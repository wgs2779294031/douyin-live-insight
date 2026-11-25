"""Offline vendor-side sample assembly, not install/upgrade transaction logic.

Accept only explicit hash inventories and a new destination. Never copies a
worktree, Git history, existing customer state, or an unlisted prepared file.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys

ROOT = Path(__file__).resolve().parents[1]
BLOCK = 1024 * 1024
RESERVED = {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))}


def require(condition, code):
    if not condition:
        raise ValueError(code)


def relative(value):
    require(isinstance(value, str) and 0 < len(value) <= 2048, 'invalid_relative_path')
    parts = value.split('/')
    require(all(p and p not in ('.', '..') and len(p) <= 255 and not p.endswith((' ', '.'))
                and p.split('.')[0].upper() not in RESERVED and not re.search(r'[<>:"\\|?*\x00-\x1f]', p)
                for p in parts), 'invalid_relative_path')
    return PurePosixPath(value)


def program_path(name):
    parts = relative(name).parts
    require(not any(part.casefold() in {'.git', 'data', 'captures', 'browser-profile', '.venv', 'node_modules', 'anchor_profiles'}
                    for part in parts) and name.casefold() not in {'config.json', 'anchors.json', 'products.json', '.env.watch'},
            'forbidden_program_path')
    return name


def no_link(path, directory=False):
    info = Path(path).lstat()
    require(not stat.S_ISLNK(info.st_mode) and not getattr(info, 'st_file_attributes', 0) & 0x400, 'linked_path')
    require(stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode) and info.st_nlink == 1,
            'invalid_file_kind')
    return info


def directory(path):
    path = local_path(path)
    for parent in (path, *path.parents):
        no_link(parent, True)
    return path


def local_path(value):
    value = os.fspath(value)
    require(bool(value) and not value.startswith(('\\\\', '//')) and '://' not in value
            and not any(ord(char) < 32 for char in value), 'invalid_local_path')
    path = Path(value)
    require(not path.drive or (len(path.drive) == 2 and path.drive[1] == ':'), 'invalid_local_path')
    require(not path.drive or path.is_absolute(), 'invalid_local_path')
    return Path(os.path.abspath(path))


def absent(path):
    try:
        path.lstat()
    except FileNotFoundError:
        return
    raise ValueError('output_must_be_new')


def signature(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_nlink


def read_json(path):
    path = local_path(path)
    directory(path.parent)
    require(no_link(path).st_size <= 8 * BLOCK, 'manifest_too_large')
    def unique(pairs):
        value = {}
        for key, item in pairs:
            require(key not in value, 'duplicate_json_key')
            value[key] = item
        return value
    return json.loads(path.read_bytes(), object_pairs_hook=unique,
        parse_constant=lambda _v: (_ for _ in ()).throw(ValueError('invalid_json_number')))


def inventory(value):
    require(isinstance(value, dict) and isinstance(value.get('files'), list) and len(value['files']) <= 30000,
            'invalid_inventory')
    files, folded = {}, set()
    for row in value['files']:
        require(isinstance(row, dict) and set(row) >= {'path', 'bytes', 'sha256'}, 'invalid_inventory_row')
        name = str(relative(row['path']))
        require(name.casefold() not in folded and type(row['bytes']) is int and row['bytes'] >= 0
                and isinstance(row['sha256'], str) and re.fullmatch('[0-9a-f]{64}', row['sha256']), 'invalid_inventory_row')
        folded.add(name.casefold())
        files[name] = row
    dirs = value.get('directories', [])
    require(isinstance(dirs, list) and len(dirs) <= 10000, 'invalid_directories')
    normalized = [str(relative(name)) for name in dirs]
    require(len(normalized) == len({name.casefold() for name in normalized}), 'directory_alias')
    require(not {name.casefold() for name in normalized} & folded, 'file_directory_alias')
    return files, set(normalized)


def verify_tree(root, files, dirs, ignore=()):
    root = directory(root)
    observed_files, observed_dirs = set(), set()
    for path in root.rglob('*'):
        name = path.relative_to(root).as_posix()
        relative(name)
        if path.is_dir():
            no_link(path, True)
            observed_dirs.add(name)
        else:
            no_link(path)
            observed_files.add(name)
    require(observed_files == set(files) | set(ignore) and observed_dirs == dirs, 'unlisted_prepared_path')
    return root


def write_json(path, value):
    with path.open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        stream.write('\n')


def build(args):
    program = read_json(args.program_lock)
    require(program.get('schema') == 1 and program.get('kind') == 'customer_program_allowlist', 'invalid_program_lock')
    program_files, _ = inventory(program)
    python_info, asset_info = read_json(args.python_manifest), read_json(args.assets_manifest)
    require(python_info.get('completed') is True, 'python_preparation_incomplete')
    require(asset_info.get('kind') == 'prepared_runtime_assets', 'assets_preparation_incomplete')
    python_files, python_dirs = inventory(python_info)
    asset_files, asset_dirs = inventory(asset_info)
    python_root = verify_tree(args.python_dir, python_files, python_dirs)
    asset_root = local_path(args.assets_dir)
    manifest_relative = local_path(args.assets_manifest).relative_to(asset_root).as_posix()
    asset_root = verify_tree(asset_root, asset_files, asset_dirs, ignore=(manifest_relative,))
    excludes = {row['path']: row for row in python_info.get('final_copy_excludes', [])}
    require(len(excludes) == 28, 'wrapper_exclusions_unconfirmed')
    for name, row in excludes.items():
        require(name.startswith('Lib/site-packages/bin/') and name.endswith('.exe')
                and name in python_files and row['sha256'] == python_files[name]['sha256']
                and row.get('reason') == 'pip_generated_builder_shebang_not_product_entrypoint', 'invalid_wrapper_exclusion')
    output = local_path(args.output)
    directory(output.parent)
    absent(output)
    output.mkdir()
    copied, copied_dirs = {}, set()

    def make_dir(name):
        current = output
        for part in relative(name).parts:
            current = current / part
            current.mkdir(exist_ok=True)
            no_link(current, True)
            copied_dirs.add(current.relative_to(output).as_posix())

    def copy(source_root, name, target_name, row):
        source = source_root.joinpath(*relative(name).parts)
        directory(source.parent)
        parent_name = PurePosixPath(target_name).parent.as_posix()
        if parent_name != '.':
            make_dir(parent_name)
        target = output.joinpath(*relative(target_name).parts)
        require(target_name.casefold() not in copied, 'duplicate_payload_target')
        before = no_link(source)
        h, count = hashlib.sha256(), 0
        with source.open('rb') as incoming, target.open('xb') as outgoing:
            require(signature(os.fstat(incoming.fileno())) == signature(before), 'input_changed')
            while chunk := incoming.read(BLOCK):
                count += len(chunk)
                require(count <= row['bytes'], 'input_size_changed')
                h.update(chunk)
                outgoing.write(chunk)
            require(signature(os.fstat(incoming.fileno())) == signature(before), 'input_changed')
        require(signature(no_link(source)) == signature(before) and count == row['bytes'] and h.hexdigest() == row['sha256'],
                'input_integrity_mismatch')
        copied[target_name.casefold()] = {'path': target_name, 'bytes': count, 'sha256': h.hexdigest()}

    for name, row in sorted(program_files.items()):
        program_path(name)
        copy(ROOT, name, 'app/' + name, row)
    for name in sorted(python_dirs):
        make_dir('runtime/python/' + name)
    for name, row in sorted(python_files.items()):
        if name not in excludes:
            copy(python_root, name, 'runtime/python/' + name, row)

    def asset_target(name):
        parts = relative(name).parts
        destinations = {'node': 'runtime/node', 'ffmpeg': 'runtime/ffmpeg',
                        'model': 'assets/models/faster-whisper-small', 'notices': 'notices'}
        if name == 'origin.json':
            return 'notices/assets-origin.json'
        require(parts[0] in destinations, 'unexpected_asset_component')
        return '/'.join((destinations[parts[0]], *parts[1:]))
    for name in sorted(asset_dirs):
        make_dir(asset_target(name))
    for name, row in sorted(asset_files.items()):
        copy(asset_root, name, asset_target(name), row)
    descriptor_name = 'baseline/installation-layout.example.json'
    copy(ROOT, descriptor_name, 'installation.json', program_files[descriptor_name])
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from scripts.local_installation import load_installation
    layout = load_installation(output / 'app')
    require(layout is not None, 'assembled_layout_unavailable')
    if args.sample_state:
        config = read_json(ROOT / 'config.example.json')
        config.update(rooms=[])
        config['llm']['enabled'] = False
        config['feishu']['enabled'] = False
        config['anchor'].update(enabled=False, profile_dir=str(layout['state'] / 'anchor_profiles'))
        make_dir('state/data/.local_app')
        write_json(layout['config'], config)
        write_json(layout['roster'], {'version': 1, 'updated_at': '', 'anchors': []})
        from scripts.local_autostart import instance_id
        context = {key: str(layout[key]) for key in ('root', 'config', 'data', 'roster', 'dist', 'python', 'profile')}
        context.update(schema=1, port=layout['port'], cdp_port=layout['cdp_port'], instance_id=instance_id(context))
        write_json(layout['context_file'], context)
        for path in (layout['config'], layout['roster'], layout['context_file']):
            raw = path.read_bytes()
            copied[path.relative_to(output).as_posix().casefold()] = {'path': path.relative_to(output).as_posix(),
                'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
    result = {'schema': 1, 'kind': 'customer_payload_sample', 'ready_to_export': False,
              'program_lock_sha256': hashlib.sha256(Path(args.program_lock).read_bytes()).hexdigest(),
              'python_manifest_sha256': hashlib.sha256(Path(args.python_manifest).read_bytes()).hexdigest(),
              'assets_manifest_sha256': hashlib.sha256(Path(args.assets_manifest).read_bytes()).hexdigest(),
              'sample_state': args.sample_state, 'directories': sorted(copied_dirs),
              'files': sorted(copied.values(), key=lambda row: row['path']),
              'excluded_generated_wrappers': sorted(excludes),
              'scope': 'New build directory only; no existing installation/state changed; no install/upgrade transaction.'}
    write_json(output / 'payload-manifest.json', result)
    return {'files': len(result['files']), 'directories': len(result['directories']),
            'bytes': sum(row['bytes'] for row in result['files']), 'sample_state': args.sample_state}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--program-lock', default=str(ROOT / 'baseline/customer-program-files.json'))
    for key in ('python-dir', 'python-manifest', 'assets-dir', 'assets-manifest', 'output'):
        parser.add_argument('--' + key, required=True)
    parser.add_argument('--sample-state', action='store_true', help='Only a new test/sample state, never existing customer state')
    args = parser.parse_args(argv)
    try:
        print(json.dumps({'ok': True, **build(args)}))
        return 0
    except (OSError, ValueError, KeyError, TypeError):
        print(json.dumps({'ok': False, 'code': 'payload_build_failed_partial_output_retained'}), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
