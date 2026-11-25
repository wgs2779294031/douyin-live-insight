"""Standard-library bundle verification and read-only installation inspection.

The explicitly supplied manifest digest is an integrity anchor, not a publisher
signature. Management code runs outside the product runtime.
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import uuid

BLOCK = 1024 * 1024
MAX_MANIFEST = 8 * BLOCK
MAX_FILES, MAX_DIRS = 20000, 5000
MAX_FILE, MAX_TOTAL = 600 * BLOCK, 4 * 1024 * BLOCK
RESERVED = {'CON', 'PRN', 'AUX', 'NUL', 'CONIN$', 'CONOUT$',
    *(f'COM{i}' for i in '123456789¹²³'), *(f'LPT{i}' for i in '123456789¹²³')}
COMPONENTS = {'product', 'manager', 'entry', 'bootstrap', 'guide'}
VERSION_B1 = '0.1.0-local.11B1'
VERSION_B2 = '0.1.0-local.11B2'
UPGRADE_MANAGER = {'manager/code/customer_upgrade.py', 'manager/code/customer_upgrade_gate.py'}
HEX = re.compile(r'[0-9a-f]{64}\Z')
DOCTOR_TEMPLATE = 'app/baseline/installation-layout.example.json'
DOCTOR_KEY_FILES = frozenset({
    DOCTOR_TEMPLATE, 'entry/CustomerLauncher.exe', 'entry/CustomerLauncherW.exe',
    'app/scripts/local_app.py', 'app/scripts/local_mcp.py', 'app/scripts/local_doctor.py',
    'app/scripts/local_installation.py', 'app/web/dist/index.html',
    'runtime/python/python.exe', 'runtime/python/pythonw.exe', 'runtime/python/python314.dll',
    'runtime/python/python314.zip', 'runtime/python/python314._pth',
    'runtime/python/Lib/site-packages/sitecustomize.py',
    'runtime/node/node.exe', 'runtime/ffmpeg/ffmpeg.exe',
    *(f'assets/models/faster-whisper-small/{name}' for name in
      ('config.json', 'model.bin', 'tokenizer.json', 'vocabulary.txt')),
})


class BundleError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def require(value, code):
    if not value:
        raise BundleError(code)


def relative(value):
    require(isinstance(value, str) and 0 < len(value) <= 1024, 'bundle_path_invalid')
    parts = value.split('/')
    require(len(parts) <= 32 and all(p and p not in ('.', '..') and len(p) <= 255
        and not p.endswith((' ', '.')) and p.split('.')[0].rstrip(' ').upper() not in RESERVED
        and not re.search(r'[<>:"\\|?*\x00-\x1f\x7f]', p) for p in parts), 'bundle_path_invalid')
    return value


def program_path(value):
    """Keep the accepted assembler's program/state separation at both boundaries."""
    name = relative(value)
    require(not any(part.casefold() in {'.git','data','captures','browser-profile','.venv','node_modules','anchor_profiles'}
        for part in name.split('/')) and name.casefold() not in {'config.json','anchors.json','products.json','.env.watch'},
        'bundle_program_path_forbidden')
    return name


def absolute(value):
    return _absolute(value, allow_volume_root=False)


def _absolute(value, *, allow_volume_root):
    require(isinstance(value, (str, os.PathLike)), 'install_path_invalid')
    text = os.fspath(value)
    require(text and not text.startswith(('\\\\', '//')) and '://' not in text
        and not re.search(r'[\x00-\x1f\x7f]', text), 'install_path_invalid')
    path = Path(text)
    require(path.is_absolute() and re.fullmatch('[A-Za-z]:', path.drive), 'install_path_invalid')
    # Validate raw spelling before abspath can erase a traversal or alias.
    tail = text[2:].replace('\\', '/')
    if allow_volume_root and tail == '/':
        return Path(path.drive + '\\')
    require(tail.startswith('/') and all(p and p not in ('.', '..') for p in tail[1:].split('/')),
        'install_path_invalid')
    relative(tail[1:])
    return Path(os.path.abspath(text))


def same(left, right):
    return os.path.normcase(str(left)) == os.path.normcase(str(right))


def disjoint(*paths):
    # Resolve only already guarded, non-reparse components. An uncreated path
    # is represented by its nearest existing physical ancestor plus its tail.
    # Keep every handle pinned until all comparisons have completed.
    with ExitStack() as guards:
        values = [_physical_path(absolute(path), guards) for path in paths]
        for index, left in enumerate(values):
            for right in values[index + 1:]:
                aliases = (_prefix(left['parts'], right['parts']) or _prefix(right['parts'], left['parts'])
                    or (left['exists'] and left['identity'] in right['ancestors'])
                    or (right['exists'] and right['identity'] in left['ancestors'])
                    or (left['identity'] == right['identity'] and
                        (_prefix(left['missing'], right['missing']) or _prefix(right['missing'], left['missing']))))
                require(not aliases, 'install_path_overlap')


def _prefix(parent, child):
    return len(parent) <= len(child) and child[:len(parent)] == parent


def _final_path(kernel, handle):
    kernel.GetFinalPathNameByHandleW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
    kernel.GetFinalPathNameByHandleW.restype = wintypes.DWORD
    length = kernel.GetFinalPathNameByHandleW(handle, None, 0, 0)
    require(0 < length < 32768, 'install_physical_path_unconfirmed')
    buffer = ctypes.create_unicode_buffer(length + 1)
    written = kernel.GetFinalPathNameByHandleW(handle, buffer, len(buffer), 0)
    require(0 < written < len(buffer), 'install_physical_path_unconfirmed')
    value = buffer.value
    # Accept only the DOS local-volume spelling returned for this held handle;
    # never normalize an unchecked UNC/device/reparse path supplied by a user.
    require(value.startswith('\\\\?\\') and re.fullmatch('[A-Za-z]:', value[4:6]), 'install_physical_path_unconfirmed')
    return _absolute(value[4:], allow_volume_root=True)


@contextmanager
def _guard_file_record(path):
    no_link(path)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.GetFileInformationByHandleEx.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    handle = kernel.CreateFileW(str(path), 0x80, 1 | 2, None, 3, 0x00200000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise BundleError('install_physical_path_unconfirmed')
    try:
        no_link(path)
        buffer = ctypes.create_string_buffer(24)
        require(kernel.GetFileInformationByHandleEx(handle, 18, buffer, 24), 'install_identity_unconfirmed')
        raw = buffer.raw
        yield {'identity':(f'{int.from_bytes(raw[:8], "little"):016x}', raw[8:24].hex()),
            'final_path':lambda: _final_path(kernel, handle)}
    finally:
        kernel.CloseHandle(handle)


def _physical_path(path, guards):
    current = Path(path.anchor)
    record = {}
    identity = guards.enter_context(guard_directory(current, _record=record))
    ancestors = [(identity['volume_serial'], identity['file_id'])]
    parts = path.parts[1:]
    missing = ()
    for index, part in enumerate(parts):
        candidate = current / part
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            missing = tuple(os.path.normcase(value) for value in parts[index:])
            break
        require(not stat.S_ISLNK(info.st_mode) and not getattr(info, 'st_file_attributes', 0) & 0x400, 'bundle_linked_path')
        if stat.S_ISDIR(info.st_mode):
            record = {}
            identity = guards.enter_context(guard_directory(candidate, _record=record))
            ancestors.append((identity['volume_serial'], identity['file_id']))
        else:
            require(index == len(parts)-1, 'bundle_file_kind_invalid')
            record = guards.enter_context(_guard_file_record(candidate))
            ancestors.append(record['identity'])
        current = candidate
    final = record['final_path']().joinpath(*missing)
    return {'parts':tuple(os.path.normcase(part) for part in final.parts), 'identity':ancestors[-1],
        'ancestors':tuple(ancestors), 'missing':missing, 'exists':not missing}


def no_link(path, directory=False):
    info = Path(path).lstat()
    require(not stat.S_ISLNK(info.st_mode) and not getattr(info, 'st_file_attributes', 0) & 0x400,
        'bundle_linked_path')
    require(stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode) and info.st_nlink == 1,
        'bundle_file_kind_invalid')
    return info


def signature(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_nlink


def absent(path):
    try:
        Path(path).lstat()
    except FileNotFoundError:
        return
    raise BundleError('install_output_exists')


@contextmanager
def guard_directory(path, *, _record=None):
    """Pin a local directory against rename/delete; never follow a reparse point."""
    path = Path(path)
    no_link(path, True)
    require(os.name == 'nt', 'windows_required')
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.GetFileInformationByHandleEx.argtypes = [wintypes.HANDLE, ctypes.c_int,
        ctypes.c_void_p, wintypes.DWORD]
    handle = kernel.CreateFileW(str(path), 1 | 0x80, 1 | 2, None, 3, 0x02000000 | 0x00200000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise BundleError('install_directory_unconfirmed')
    try:
        no_link(path, True)
        buffer = ctypes.create_string_buffer(24)
        if not kernel.GetFileInformationByHandleEx(handle, 18, buffer, 24):
            raise BundleError('install_identity_unconfirmed')
        raw = buffer.raw
        identity = {'volume_serial': f'{int.from_bytes(raw[:8], "little"):016x}', 'file_id': raw[8:24].hex()}
        if _record is not None:
            _record['final_path'] = lambda: _final_path(kernel, handle)
        yield identity
    finally:
        kernel.CloseHandle(handle)


@contextmanager
def guard_path(path):
    path = _absolute(path, allow_volume_root=True)
    with ExitStack() as stack:
        identity = None
        for parent in reversed((path, *path.parents)):
            identity = stack.enter_context(guard_directory(parent))
        yield identity


@contextmanager
def exclusive_new(path):
    """Keep an exclusively shared CREATE_NEW lock file open for this invocation."""
    import msvcrt
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateFileW(str(path), 0x80000000 | 0x40000000, 0, None, 1, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        raise BundleError('install_lock_unconfirmed')
    try:
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDWR | os.O_BINARY)
    except BaseException:
        kernel.CloseHandle(handle)
        raise
    with os.fdopen(descriptor, 'wb') as stream:
        yield stream


def encode(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False) + '\n').encode('utf-8')


def parse(raw):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            require(key not in value, 'bundle_json_invalid')
            value[key] = item
        return value
    try:
        return json.loads(raw, object_pairs_hook=unique,
            parse_constant=lambda _: (_ for _ in ()).throw(BundleError('bundle_json_invalid')))
    except (UnicodeError, json.JSONDecodeError, RecursionError, OverflowError) as exc:
        raise BundleError('bundle_json_invalid') from exc


def read_bytes(path, limit=MAX_MANIFEST):
    path = absolute(path)
    with guard_path(path.parent):
        before = no_link(path)
        require(before.st_size <= limit, 'bundle_file_too_large')
        with path.open('rb') as stream:
            require(signature(os.fstat(stream.fileno())) == signature(before), 'bundle_input_changed')
            raw = stream.read(limit + 1)
            require(len(raw) <= limit and signature(os.fstat(stream.fileno())) == signature(before), 'bundle_input_changed')
        require(signature(no_link(path)) == signature(before), 'bundle_input_changed')
        return raw


def read_json(path, limit=MAX_MANIFEST):
    return parse(read_bytes(path, limit))


def write_new(path, raw):
    with Path(path).open('xb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    no_link(path)


def make_directories(root, names):
    """Create listed directories while their complete existing parent chain is pinned."""
    for name in sorted(names, key=lambda item:(item.count('/'),item)):
        relative(name)
        path = Path(root)/name
        with guard_path(path.parent):
            path.mkdir(exist_ok=True)
            no_link(path, True)


def publish_new(path, raw):
    """Publish a complete new fact without replacing any existing destination."""
    path = Path(path)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    write_new(temporary, raw)
    require(read_bytes(temporary, len(raw)) == raw, 'install_commit_unconfirmed')
    absent(path)
    os.rename(temporary, path)  # Windows rename never replaces a destination.
    require(read_bytes(path, len(raw)) == raw, 'install_commit_unconfirmed')


def inventory(manifest):
    require(isinstance(manifest, dict) and type(manifest.get('schema')) is int and manifest['schema'] == 1
        and manifest.get('kind') == 'customer_bundle' and type(manifest.get('state_compatibility')) is int
        and manifest['state_compatibility'] == 1,
        'bundle_manifest_invalid')
    require(isinstance(manifest.get('version'), str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,79}', manifest['version'])
        and isinstance(manifest.get('channel'), str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,39}', manifest['channel']),
        'bundle_manifest_invalid')
    rows, directories = manifest.get('files'), manifest.get('directories')
    require(isinstance(rows, list) and 0 < len(rows) <= MAX_FILES and isinstance(directories, list)
        and len(directories) <= MAX_DIRS, 'bundle_capacity')
    files, dirs, aliases, total = {}, set(), set(), 0
    for row in rows:
        require(isinstance(row, dict) and set(row) == {'path', 'bytes', 'sha256', 'component'}, 'bundle_manifest_invalid')
        name = relative(row['path'])
        require(name.casefold() not in aliases and type(row['bytes']) is int and 0 <= row['bytes'] <= MAX_FILE
            and isinstance(row['sha256'], str) and HEX.fullmatch(row['sha256'])
            and isinstance(row['component'], str) and row['component'] in COMPONENTS, 'bundle_manifest_invalid')
        component = row['component']
        valid = ((component == 'product' and name.startswith('payload/') and
                  (name.split('/')[1] in {'app', 'runtime', 'assets', 'notices'} or
                   name in {'payload/installation.json','payload/payload-manifest.json'}))
            or (component == 'manager' and (name.startswith('manager/python/') or name in {
                'manager/code/customer_bundle.py', 'manager/code/customer_install.py', 'manager/code/local_doctor.py'}
                or name in UPGRADE_MANAGER))
            or (component == 'entry' and name in {'entry/CustomerLauncher.exe', 'entry/CustomerLauncherW.exe'})
            or (component == 'bootstrap' and name == 'CustomerSetup.exe')
            or (component == 'guide' and name == 'AGENT_INSTALL.md'))
        require(valid, 'bundle_component_invalid')
        if name.startswith('payload/app/'):
            program_path(name.removeprefix('payload/app/'))
        aliases.add(name.casefold())
        files[name] = row
        total += row['bytes']
    require(total <= MAX_TOTAL, 'bundle_capacity')
    for name in directories:
        relative(name)
        if name.startswith('payload/app/'):
            program_path(name.removeprefix('payload/app/'))
        require(name.casefold() not in aliases, 'bundle_path_alias')
        require(name in {'payload','manager','manager/python','manager/code','entry'}
            or any(name == prefix or name.startswith(prefix+'/') for prefix in
                ('payload/app','payload/runtime','payload/assets','payload/notices','manager/python')), 'bundle_component_invalid')
        aliases.add(name.casefold())
        dirs.add(name)
    require(len(dirs) == len(directories), 'bundle_path_alias')
    for name in list(files) + list(dirs):
        parts = name.split('/')
        for index in range(1, len(parts)):
            require('/'.join(parts[:index]) in dirs, 'bundle_parent_missing')
    required = {'payload/installation.json', 'payload/payload-manifest.json', 'manager/python/python.exe',
        'manager/python/python314._pth', 'manager/code/customer_bundle.py', 'manager/code/customer_install.py',
        'manager/code/local_doctor.py', 'entry/CustomerLauncher.exe', 'entry/CustomerLauncherW.exe', 'CustomerSetup.exe', 'AGENT_INSTALL.md'}
    if manifest['version'] == VERSION_B2:
        required |= UPGRADE_MANAGER
    require(required <= files.keys(), 'bundle_component_missing')
    require(files['CustomerSetup.exe']['sha256'] == files['entry/CustomerLauncher.exe']['sha256'], 'bundle_bootstrap_mismatch')
    return files, dirs


def scan_tree(root, *, skip=()):
    root = absolute(root)
    files, directories, aliases = set(), set(), set()
    def visit(directory):
        with guard_path(directory):
            with os.scandir(directory) as entries:
                for item in entries:
                    name = Path(item.path).relative_to(root).as_posix()
                    relative(name)
                    require(name.casefold() not in aliases, 'bundle_path_alias')
                    aliases.add(name.casefold())
                    if name in skip:
                        no_link(item.path, item.is_dir(follow_symlinks=False))
                        continue
                    info = item.stat(follow_symlinks=False)
                    if stat.S_ISDIR(info.st_mode):
                        no_link(item.path, True)
                        directories.add(name)
                        require(len(directories) <= MAX_DIRS, 'bundle_capacity')
                        visit(Path(item.path))
                    else:
                        no_link(item.path)
                        files.add(name)
                        require(len(files) <= MAX_FILES, 'bundle_capacity')
    visit(root)
    return files, directories


def check_file(path, row, *, deep):
    with guard_path(Path(path).parent):
        info = no_link(path)
        require(info.st_size == row['bytes'], 'bundle_integrity_mismatch')
        if deep:
            digest, count = hashlib.sha256(), 0
            with Path(path).open('rb') as stream:
                require(signature(os.fstat(stream.fileno())) == signature(info), 'bundle_input_changed')
                while chunk := stream.read(BLOCK):
                    count += len(chunk)
                    require(count <= row['bytes'], 'bundle_input_changed')
                    digest.update(chunk)
                require(signature(os.fstat(stream.fileno())) == signature(info), 'bundle_input_changed')
            require(signature(no_link(path)) == signature(info) and count == row['bytes']
                and digest.hexdigest() == row['sha256'], 'bundle_integrity_mismatch')


def load_manifest(path, expected_sha256=None):
    raw = read_bytes(path)
    digest = hashlib.sha256(raw).hexdigest()
    if expected_sha256 is not None:
        require(isinstance(expected_sha256, str) and HEX.fullmatch(expected_sha256)
            and digest == expected_sha256, 'bundle_manifest_digest_mismatch')
    value = parse(raw)
    files, directories = inventory(value)
    return value, raw, digest, files, directories


def verify_bundle(bundle, manifest_sha256, *, deep=True):
    bundle = absolute(bundle)
    with guard_path(bundle):
        value, raw, digest, files, directories = load_manifest(bundle / 'bundle.manifest.json', manifest_sha256)
        actual_files, actual_dirs = scan_tree(bundle)
        require(actual_files == set(files) | {'bundle.manifest.json'} and actual_dirs == directories, 'bundle_unlisted_path')
        for name, row in files.items():
            check_file(bundle / name, row, deep=deep)
        require(read_bytes(bundle / 'manager/python/python314._pth') == b'python314.zip\n.\n../code\n', 'bundle_manager_python_invalid')
        require(not any(name.casefold().endswith(('/sitecustomize.py', '/usercustomize.py'))
            or '/site-packages/' in name.casefold() for name in files if name.startswith('manager/')), 'bundle_manager_python_invalid')
        return {'manifest': value, 'raw': raw, 'sha256': digest, 'files': files, 'directories': directories}


def descriptor_layout(target, descriptor, owner):
    fields = {'schema', 'kind', 'app', 'state', 'python', 'node', 'ffmpeg', 'model', 'cache', 'browser', 'dll_directories', 'ports'}
    require(isinstance(descriptor, dict) and set(descriptor) == fields and type(descriptor['schema']) is int and descriptor['schema'] == 1
        and descriptor['kind'] == 'customer_installation', 'installation_descriptor_invalid')
    constants = {'app':'app', 'state':'state', 'python':'runtime/python/python.exe', 'node':'runtime/node/node.exe',
        'ffmpeg':'runtime/ffmpeg/ffmpeg.exe', 'model':'assets/models/faster-whisper-small', 'cache':'state/cache'}
    require(all(descriptor.get(key) == value for key, value in constants.items()), 'installation_descriptor_invalid')
    ports, browser = descriptor['ports'], descriptor['browser']
    require(isinstance(ports, dict) and set(ports) == {'board', 'cdp'}
        and all(type(p) is int and 1 <= p <= 65535 for p in ports.values()) and ports['board'] != ports['cdp'], 'installation_descriptor_invalid')
    require(isinstance(browser, dict) and (browser == {'kind':'system-edge'}
        or (set(browser) == {'kind','path'} and browser['kind'] == 'explicit')), 'installation_descriptor_invalid')
    browser_executable = None
    if browser['kind'] == 'explicit':
        browser_executable = absolute(browser['path'])
    else:
        for base in ('C:/Program Files (x86)', 'C:/Program Files'):
            candidate = Path(base) / 'Microsoft/Edge/Application/msedge.exe'
            try:
                no_link(candidate)
                browser_executable = candidate
                break
            except (OSError, BundleError):
                pass
    dlls = descriptor['dll_directories']
    require(isinstance(dlls, list) and len(dlls) <= 32 and len(set(dlls)) == len(dlls), 'installation_descriptor_invalid')
    for name in dlls:
        relative(name)
        require(name == 'runtime/python' or name.startswith('runtime/python/'), 'installation_descriptor_invalid')
    state, root = target / 'state', target / 'app'
    result = {key: target / value for key, value in constants.items()}
    result.update(target=target, install=target, root=root, state=state, pythonw=target/'runtime/python/pythonw.exe',
        data=state/'data', config=state/'config.json', roster=state/'anchors.json', dist=root/'web/dist',
        profile=state/'data/.local_app/browser-profile', context_file=state/'data/.local_app/context.json',
        port=ports['board'], cdp_port=ports['cdp'], browser=dict(browser), browser_executable=browser_executable,
        entry=target/'entry', gate=target/'.gate', manager_cache=absolute(owner['manager_cache']),
        dll_directories=tuple(target / name for name in dlls))
    return result


def _identity_shape(value):
    return isinstance(value, dict) and set(value) == {'volume_serial','file_id'} and all(
        isinstance(value.get(key), str) and re.fullmatch(f'[0-9a-f]{{{length}}}', value[key])
        for key, length in (('volume_serial',16),('file_id',32)))


def initial_install_fact(target, owner, identity):
    intent = read_json(target.parent/f'.{target.name}.install-intent.json', 65536)
    require(isinstance(intent, dict) and type(intent.get('schema')) is int and intent['schema'] == 1
        and intent.get('kind') == 'customer_first_install_intent' and intent.get('phase') == 'installed'
        and intent.get('install_id') == owner['install_id'] and intent.get('target') == owner['target']
        and intent.get('target_identity') == identity and intent.get('stage_identity') == identity
        and isinstance(intent.get('bundle_manifest_sha256'), str)
        and HEX.fullmatch(intent['bundle_manifest_sha256']) and intent.get('code') is None,
        'installation_completion_unconfirmed')
    legacy_cache = target.parent/f'.{target.name}.manager-{owner["install_id"]}'
    return intent, legacy_cache


def manager_inventory(cache, owner, *, deep):
    digest = owner['manager_manifest_sha256']
    with guard_path(cache):
        expected_owner = {'schema':1,'kind':'customer_manager_cache_owner','install_id':owner['install_id'],
            'target':owner['target'],'target_identity':owner['target_identity'],'bundle_manifest_sha256':digest}
        cached = read_json(cache/'cache-owner.json', 65536)
        require(isinstance(cached, dict) and type(cached.get('schema')) is int and cached == expected_owner,
            'installation_cache_invalid')
        manifest, raw, _, files, directories = load_manifest(cache/'bundle.manifest.json', digest)
        manager_files = {name:row for name,row in files.items() if row['component'] == 'manager'}
        manager_dirs = {name for name in directories if name == 'manager' or name.startswith('manager/')}
        seen_files, seen_dirs = scan_tree(cache)
        require(seen_files == set(manager_files) | {'cache-owner.json','bundle.manifest.json'}
            and seen_dirs == manager_dirs, 'installation_cache_invalid')
        for name, row in manager_files.items():
            check_file(cache/name, row, deep=deep)
    return manifest, raw, files, directories, manager_files


def _doctor_component_missing(name, *, manifest=False):
    # Only this fixed key set can produce a public component diagnosis.
    require(name in DOCTOR_KEY_FILES, 'installation_unconfirmed')
    component = {
        DOCTOR_TEMPLATE:'installation_template', 'app/web/dist/index.html':'board_dist',
        'runtime/python/python.exe':'python', 'runtime/python/pythonw.exe':'pythonw',
        'runtime/node/node.exe':'node', 'runtime/ffmpeg/ffmpeg.exe':'ffmpeg',
    }.get(name)
    if component is None:
        component = ('model_files' if name.startswith('assets/models/') else
            'launcher' if name.startswith('entry/') else
            'application_code' if name.startswith('app/scripts/') else 'private_python_dependencies')
    error = BundleError('installation_component_manifest_missing' if manifest else 'installation_component_missing')
    error.doctor_component = component
    return error


def inspect_install(target, deep=False):
    """Inspect owned immutable files; never create state or import target code."""
    return _inspect_install(target, deep=deep)


def inspect_install_quick(target):
    """Doctor-only scope: full cache hashes and fixed target component metadata."""
    return _inspect_install(target, doctor_quick=True)


def _inspect_install(target, deep=False, *, doctor_quick=False):
    target = absolute(target)
    try:
        with guard_path(target) as identity:
            owner = read_json(target/'owner.json', 65536)
            expected = {'schema','kind','install_id','target','target_identity','version','channel',
                'bundle_manifest_sha256','manager_cache','manager_manifest_sha256'}
            require(isinstance(owner, dict) and set(owner) == expected and type(owner.get('schema')) is int and owner['schema'] == 1
                and owner.get('kind') == 'customer_installation_owner', 'installation_owner_invalid')
            require(isinstance(owner['install_id'], str) and str(uuid.UUID(owner['install_id'])) == owner['install_id']
                and same(absolute(owner['target']), target)
                and _identity_shape(owner['target_identity']) and identity == owner['target_identity'], 'installation_owner_invalid')
            manifest, raw, digest, files, directories = load_manifest(target/'installed.manifest.json', owner['bundle_manifest_sha256'])
            require(owner['version'] == manifest['version'] and owner['channel'] == manifest['channel'],
                'installation_owner_invalid')
            intent, legacy_cache = initial_install_fact(target, owner, identity)
            cache = absolute(owner['manager_cache'])
            migrated = same(cache, legacy_cache.with_name(legacy_cache.name+'-b2'))
            require(migrated or (same(cache, legacy_cache) and owner['manager_manifest_sha256'] == digest
                and intent['bundle_manifest_sha256'] == digest), 'installation_cache_invalid')
            disjoint(target, cache)
            manager_manifest, manager_raw, manager_rows, manager_dirs, manager_files = manager_inventory(
                cache, owner, deep=deep or doctor_quick)
            if migrated:
                require(manager_manifest['version'] == VERSION_B2 and manager_manifest['channel'] == owner['channel'] == 'local-candidate'
                    and intent.get('manager_cache') == str(legacy_cache)
                    and ((manifest['version'] == VERSION_B1 and digest == intent['bundle_manifest_sha256'])
                        or (manifest['version'] == VERSION_B2 and raw == manager_raw)),
                    'installation_cache_invalid')
                # The first-install record remains immutable across program switches.
                initial, _, _, _, _ = load_manifest(legacy_cache/'bundle.manifest.json', intent['bundle_manifest_sha256'])
                require(initial['version'] == VERSION_B1 and initial['channel'] == 'local-candidate',
                    'installation_completion_unconfirmed')
            else:
                require(manager_raw == raw, 'installation_cache_invalid')
            expected_files, expected_dirs = {}, set()
            for name, row in files.items():
                if row['component'] == 'product':
                    expected_files[name.removeprefix('payload/')] = row
            expected_files.update({name:row for name,row in manager_rows.items() if row['component'] in {'entry','guide'}})
            for name in directories:
                if name.startswith('payload/'):
                    expected_dirs.add(name.removeprefix('payload/'))
            expected_dirs.update(name for name in manager_dirs if name == 'entry' or name.startswith('entry/'))
            if doctor_quick:
                missing = DOCTOR_KEY_FILES - expected_files.keys()
                if missing:
                    raise _doctor_component_missing(sorted(missing)[0], manifest=True)
                for name in sorted(DOCTOR_KEY_FILES - {DOCTOR_TEMPLATE}):
                    try:
                        check_file(target/name, expected_files[name], deep=False)
                    except FileNotFoundError as exc:
                        raise _doctor_component_missing(name) from exc
            else:
                seen_files, seen_dirs = scan_tree(target, skip=('state','.gate'))
                require(seen_files == set(expected_files) | {'owner.json','installed.manifest.json'}
                    and seen_dirs == expected_dirs, 'installation_unlisted_path')
                for name, row in expected_files.items():
                    if name != 'installation.json':
                        check_file(target/name, row, deep=deep)
            descriptor = read_json(target/'installation.json', 65536)
            if doctor_quick:
                # This immutable template authorizes mutable descriptor choices.
                # Bind the parsed bytes to the manifest in this same read.
                try:
                    template = read_bytes(target/DOCTOR_TEMPLATE, 65536)
                except FileNotFoundError as exc:
                    raise _doctor_component_missing(DOCTOR_TEMPLATE) from exc
                template_row = expected_files[DOCTOR_TEMPLATE]
                require(len(template) == template_row['bytes']
                    and hashlib.sha256(template).hexdigest() == template_row['sha256'], 'bundle_integrity_mismatch')
                original = parse(template)
            else:
                original = read_json(target/DOCTOR_TEMPLATE, 65536)
            require({k:v for k,v in descriptor.items() if k not in {'ports','browser'}} ==
                {k:v for k,v in original.items() if k not in {'ports','browser'}}, 'installation_descriptor_invalid')
            layout = descriptor_layout(target, descriptor, owner)
            # Existing mutable roots are required, but their customer contents are not enumerated.
            no_link(layout['state'], True)
            no_link(layout['data'], True)
            no_link(layout['gate'], True)
            for name in ('admission.lock','code.lease','maintenance.json','spawn-ledger.json'):
                no_link(layout['gate']/name)
            if doctor_quick:
                for directory in layout['dll_directories']:
                    with guard_path(directory):
                        pass
                return {'owner':owner, 'manifest':manifest, 'layout':layout,
                    'integrity':{'scope':'doctor_key_metadata_and_complete_cache_hashes', 'deep':False,
                        'files_checked':len(DOCTOR_KEY_FILES)+len(manager_files),
                        'target_files_checked':len(DOCTOR_KEY_FILES), 'cache_files_checked':len(manager_files),
                        'target_inventory_checked':False, 'target_hashes_checked':False,
                        'cache_inventory_checked':True, 'cache_hashes_checked':True,
                        'template_hash_checked':True, 'state_contents_checked':False}}
            return {'owner':owner, 'manifest':manifest, 'layout':layout,
                'integrity':{'scope':'immutable_manifest_files','deep':bool(deep),
                    'files_checked':len(expected_files)+len(manager_files), 'state_contents_checked':False}}
    except BundleError:
        raise
    except FileNotFoundError as exc:
        raise BundleError('installation_not_ready') from exc
    except (OSError, ValueError, TypeError, KeyError, OverflowError) as exc:
        raise BundleError('installation_unconfirmed') from exc
