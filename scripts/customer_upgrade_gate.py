"""External Windows lease and drain protocol for the installed application."""
from __future__ import annotations

from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import math
import os
import re
import time
import uuid

try:
    from . import customer_bundle as bundle
except ImportError:
    import customer_bundle as bundle


READ, WRITE = 0x80000000, 0x40000000
SHARE_READ_WRITE, SHARE_ALL = 3, 7
OPEN_EXISTING, OPEN_ALWAYS = 3, 4
INVALID = ctypes.c_void_p(-1).value
BUSY = {32, 33}
MISSING_PROCESS = {87, 1168}
MODES = {'start', 'stop', 'status', 'run', 'mcp', 'doctor-imports'}
ACTIVE_PHASES = {'reserved', 'creating', 'running', 'unknown'}
TERMINAL_PHASES = {'child_exited', 'not_created'}
MCP_ISOLATION = 'single_process_job_v1'
AMBIGUOUS_IMAGES = {'python.exe', 'pythonw.exe', 'node.exe', 'ffmpeg.exe',
                    'customerlauncher.exe', 'customerlauncherw.exe'}
CONTEXT_PATHS = {'root','config','data','roster','dist','python','profile'}


class _FileInfo(ctypes.Structure):
    _fields_ = [(name, wintypes.DWORD) for name in (
        'attributes', 'creation_low', 'creation_high', 'access_low', 'access_high',
        'write_low', 'write_high', 'volume', 'size_high', 'size_low', 'links', 'index_high', 'index_low')]


class _Overlapped(ctypes.Structure):
    _fields_ = [('internal', ctypes.c_size_t), ('internal_high', ctypes.c_size_t),
                ('offset', wintypes.DWORD), ('offset_high', wintypes.DWORD), ('event', wintypes.HANDLE)]


class _ProcessEntry(ctypes.Structure):
    _fields_ = [('size', wintypes.DWORD), ('usage', wintypes.DWORD), ('pid', wintypes.DWORD),
                ('heap', ctypes.c_size_t), ('module', wintypes.DWORD), ('threads', wintypes.DWORD),
                ('parent', wintypes.DWORD), ('priority', wintypes.LONG), ('flags', wintypes.DWORD),
                ('basename', wintypes.WCHAR * 260)]


def _api():
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    ptr = ctypes.c_void_p
    handle, number, boolean = wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL
    const_str, buffer = wintypes.LPCWSTR, wintypes.LPWSTR
    signatures = {
        'CreateFileW': (handle, (const_str, number, number, ptr, number, number, handle)),
        'CloseHandle': (boolean, (handle,)),
        'GetFileInformationByHandle': (boolean, (handle, ctypes.POINTER(_FileInfo))),
        'GetFinalPathNameByHandleW': (number, (handle, buffer, number, number)),
        'LockFileEx': (boolean, (handle, number, number, number, number, ctypes.POINTER(_Overlapped))),
        'MoveFileExW': (boolean, (const_str, const_str, number)),
        'OpenProcess': (handle, (number, boolean, number)),
        'GetExitCodeProcess': (boolean, (handle, ctypes.POINTER(number))),
        'GetProcessTimes': (boolean, (handle,) + (ctypes.POINTER(ctypes.c_ulonglong),) * 4),
        'QueryFullProcessImageNameW': (boolean, (handle, number, buffer, ctypes.POINTER(number))),
        'CreateToolhelp32Snapshot': (handle, (number, number)),
        'Process32FirstW': (boolean, (handle, ctypes.POINTER(_ProcessEntry))),
        'Process32NextW': (boolean, (handle, ctypes.POINTER(_ProcessEntry))),
    }
    for name, (result, arguments) in signatures.items():
        method = getattr(api, name)
        method.restype, method.argtypes = result, arguments
    return api


def _uuid(value):
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except ValueError:
        return False


def _process_identity(value):
    bundle.require(isinstance(value, dict) and set(value) == {'pid', 'created_filetime'}
        and type(value['pid']) is int and 0 < value['pid'] <= 0xFFFFFFFF
        and isinstance(value['created_filetime'], str)
        and re.fullmatch(r'[1-9][0-9]{0,19}', value['created_filetime'])
        and int(value['created_filetime']) <= 0xFFFFFFFFFFFFFFFF, 'upgrade_gate_unconfirmed')


@contextmanager
def _open(api, path, *, access=READ | WRITE, sharing=0, opening=OPEN_EXISTING,
          directory=False, single_link=True, busy='upgrade_gate_unconfirmed'):
    flags = 0x00200000 | (0x02000000 if directory else 0x80)  # OPEN_REPARSE_POINT
    handle = api.CreateFileW(str(path), access, sharing, None, opening, flags, None)
    if handle == INVALID:
        error = ctypes.get_last_error()
        if error in (2, 3) and opening == OPEN_EXISTING:
            raise FileNotFoundError(error, 'gate_file_unavailable')
        raise bundle.BundleError(busy if error in BUSY else 'upgrade_gate_unconfirmed')
    try:
        info = _FileInfo()
        bundle.require(api.GetFileInformationByHandle(handle, ctypes.byref(info))
            and not info.attributes & 0x400 and bool(info.attributes & 0x10) == directory
            and (directory or not single_link or info.links == 1), 'upgrade_gate_unconfirmed')
        yield handle
    finally:
        api.CloseHandle(handle)


@contextmanager
def _byte_lock(api, path, *, create=False):
    """Match FileLock's msvcrt first-byte lock without importing target code."""
    with _open(api, path, sharing=SHARE_READ_WRITE, opening=OPEN_ALWAYS if create else OPEN_EXISTING,
               busy='upgrade_holders_busy') as handle:
        offset = _Overlapped()
        if not api.LockFileEx(handle, 3, 0, 1, 0, ctypes.byref(offset)):
            error = ctypes.get_last_error()
            raise bundle.BundleError('upgrade_holders_busy' if error in BUSY else 'upgrade_holders_unconfirmed')
        yield
        # Closing this handle releases its byte lock; the path is never removed.


def _replace(api, path, value, *, allow_missing=False):
    raw = bundle.encode(value)
    bundle.require(len(raw) <= 65536, 'upgrade_gate_unconfirmed')
    try:
        bundle.no_link(path)
    except FileNotFoundError:
        if not allow_missing:
            raise
    temp = path.with_name(f'{path.name}.{uuid.uuid4().hex}.tmp')
    bundle.write_new(temp, raw)
    if not api.MoveFileExW(str(temp), str(path), 1 | 8):  # REPLACE_EXISTING | WRITE_THROUGH
        raise bundle.BundleError('upgrade_gate_write_unconfirmed')
    bundle.require(bundle.read_bytes(path, 65536) == raw, 'upgrade_gate_write_unconfirmed')


def _handle_creation(api, handle):
    stamps = [ctypes.c_ulonglong() for _ in range(4)]
    if api.GetProcessTimes(handle, *(ctypes.byref(value) for value in stamps)):
        return stamps[0].value
    return None


def _app_creation(stamp):
    # This is the original local_app process receipt's Windows conversion.
    return stamp / 1e7 - 11644473600.0


def _state_of(api, identity):
    """Classify the exact Windows process identity without adopting a reused PID."""
    handle = api.OpenProcess(0x1000, False, identity['pid'])
    if not handle:
        return 'ended' if ctypes.get_last_error() in MISSING_PROCESS else 'unknown'
    try:
        created, code = _handle_creation(api, handle), wintypes.DWORD()
        if created is None or not api.GetExitCodeProcess(handle, ctypes.byref(code)):
            return 'unknown'
        return 'alive' if str(created) == identity['created_filetime'] and code.value == 259 else 'ended'
    finally:
        api.CloseHandle(handle)


def _image_name(api, handle):
    name = ctypes.create_unicode_buffer(32768)
    count = wintypes.DWORD(len(name))
    if api.QueryFullProcessImageNameW(handle, 0, name, ctypes.byref(count)):
        return name.value
    return None


def _final_image(api, path, *, directory=False):
    # A process can be mapped via a substitute drive or a short name. Compare
    # the actual volume path by handle instead of trusting the original spelling.
    with _open(api, path, access=0x80, sharing=SHARE_ALL, directory=directory, single_link=False) as handle:
        name = ctypes.create_unicode_buffer(32768)
        size = api.GetFinalPathNameByHandleW(handle, name, len(name), 1)  # VOLUME_NAME_GUID
        bundle.require(0 < size < len(name), 'upgrade_target_process_unconfirmed')
        return os.path.normcase(name.value)


def _process_entries(api):
    snapshot = api.CreateToolhelp32Snapshot(2, 0)
    bundle.require(snapshot != INVALID, 'upgrade_target_process_unconfirmed')
    try:
        row = _ProcessEntry(size=ctypes.sizeof(_ProcessEntry))
        exists = api.Process32FirstW(snapshot, ctypes.byref(row))
        count = 0
        while exists:
            count += 1
            bundle.require(count <= 32768, 'upgrade_target_process_unconfirmed')
            if row.pid:
                yield row.pid, row.basename.casefold()
            exists = api.Process32NextW(snapshot, ctypes.byref(row))
        bundle.require(ctypes.get_last_error() == 18, 'upgrade_target_process_unconfirmed')  # NO_MORE_FILES
    finally:
        api.CloseHandle(snapshot)


def _scan_target_processes(api, target):
    physical = _final_image(api, target, directory=True).rstrip('\\') + '\\'
    lexical = os.path.normcase(str(target)).rstrip('\\') + '\\'
    deadline = time.monotonic() + 5.0
    for pid, basename in _process_entries(api):
        bundle.require(time.monotonic() < deadline, 'upgrade_target_process_unconfirmed')
        handle = api.OpenProcess(0x1000, False, pid)
        if not handle:
            missing = ctypes.get_last_error() in MISSING_PROCESS
            bundle.require(missing or basename not in AMBIGUOUS_IMAGES, 'upgrade_target_process_unconfirmed')
            continue
        try:
            path = _image_name(api, handle)
            if path is None:
                code = wintypes.DWORD()
                ended = api.GetExitCodeProcess(handle, ctypes.byref(code)) and code.value != 259
                bundle.require(ended or basename not in AMBIGUOUS_IMAGES, 'upgrade_target_process_unconfirmed')
                continue
            if os.path.normcase(path).startswith(lexical):
                bundle.require(_handle_creation(api, handle), 'upgrade_target_process_unconfirmed')
                raise bundle.BundleError('upgrade_target_process_busy')
            try:
                final = _final_image(api, path)
            except (OSError, bundle.BundleError):
                code = wintypes.DWORD()
                ended = api.GetExitCodeProcess(handle, ctypes.byref(code)) and code.value != 259
                bundle.require(ended or basename not in AMBIGUOUS_IMAGES, 'upgrade_target_process_unconfirmed')
                continue
            if final.startswith(physical):
                bundle.require(_handle_creation(api, handle), 'upgrade_target_process_unconfirmed')
                raise bundle.BundleError('upgrade_target_process_busy')
        finally:
            api.CloseHandle(handle)


class _Gate:
    def __init__(self, target, install_id, token=None, expected_context=None):
        bundle.require(_uuid(install_id) and (token is None or _uuid(token)), 'upgrade_gate_unconfirmed')
        self.api = _api()
        self.target, self.install_id, self.token = bundle.absolute(target), install_id, token
        self.directory = self.target / '.gate'
        self.app_state = self.target / 'state/data/.local_app'
        self.expected_context = expected_context
        self._exclusive = False
        self._generation = None

    @contextmanager
    def _admission(self):
        deadline = time.monotonic() + .5
        while True:
            try:
                claim = _open(self.api, self.directory/'admission.lock', busy='upgrade_admission_busy')
                claim.__enter__()
            except bundle.BundleError as error:
                if error.code != 'upgrade_admission_busy' or time.monotonic() >= deadline:
                    raise
                time.sleep(.01)
            else:
                break
        try:
            yield
        finally:
            claim.__exit__(None, None, None)

    def _facts(self):
        maintenance = bundle.read_json(self.directory/'maintenance.json', 65536)
        ledger = bundle.read_json(self.directory/'spawn-ledger.json', 65536)
        keys = {'schema', 'kind', 'install_id', 'active', 'token'}
        bundle.require(isinstance(maintenance, dict) and type(maintenance.get('schema')) is int
            and maintenance['schema'] in (1, 2), 'upgrade_gate_unconfirmed')
        if maintenance['schema'] == 2:
            keys.add('generation')
        bundle.require(set(maintenance) == keys and maintenance['kind'] == 'customer_maintenance'
            and maintenance['install_id'] == self.install_id and type(maintenance['active']) is bool
            and (_uuid(maintenance['token']) if maintenance['active'] else maintenance['token'] == '')
            and (maintenance['schema'] == 1 or _uuid(maintenance['generation'])), 'upgrade_gate_unconfirmed')
        bundle.require(isinstance(ledger, dict) and set(ledger) == {'schema','kind','install_id','active','recent'}
            and type(ledger['schema']) is int and ledger['schema'] == 1
            and ledger['kind'] == 'customer_spawn_ledger' and ledger['install_id'] == self.install_id,
            'upgrade_gate_unconfirmed')
        ids = set()
        entry_keys = {'id', 'mode', 'phase', 'launcher', 'child'}
        for kind, phases in (('active', ACTIVE_PHASES), ('recent', TERMINAL_PHASES)):
            values = ledger[kind]
            bundle.require(isinstance(values, list) and len(values) <= 32, 'upgrade_gate_unconfirmed')
            for entry in values:
                bundle.require(isinstance(entry, dict) and set(entry) in (entry_keys, entry_keys | {'isolation'})
                    and isinstance(entry['id'], str) and re.fullmatch(r'[0-9a-f]{32}', entry['id'])
                    and entry['id'] not in ids and isinstance(entry['mode'], str) and entry['mode'] in MODES
                    and isinstance(entry['phase'], str) and entry['phase'] in phases,
                    'upgrade_gate_unconfirmed')
                ids.add(entry['id'])
                _process_identity(entry['launcher'])
                if entry['child'] is not None:
                    _process_identity(entry['child'])
                else:
                    bundle.require(entry['phase'] not in ('running','child_exited'), 'upgrade_gate_unconfirmed')
                if 'isolation' in entry:
                    bundle.require(entry['isolation'] == MCP_ISOLATION and entry['mode'] == 'mcp'
                        and entry['phase'] == 'running', 'upgrade_gate_unconfirmed')
        return maintenance, ledger

    def _owned(self, maintenance):
        bundle.require(maintenance['active'] and maintenance['token'] == self.token,
                       'upgrade_maintenance_owned_elsewhere')

    def _mark(self):
        with self._admission():
            maintenance, _ = self._facts()
            if maintenance['active']:
                self._owned(maintenance)
            else:
                maintenance.update(active=True, token=self.token)
                _replace(self.api, self.directory/'maintenance.json', maintenance)

    def _reap_ended_mcp(self, ledger):
        """Release only OS-contained MCP pairs while holding admission."""
        remaining = []
        for entry in ledger['active']:
            if (entry['mode'] == 'mcp' and entry['phase'] == 'running'
                    and entry.get('isolation') == MCP_ISOLATION
                    and _state_of(self.api, entry['launcher']) == 'ended'
                    and _state_of(self.api, entry['child']) == 'ended'):
                continue
            remaining.append(entry)
        if len(remaining) != len(ledger['active']):
            ledger['active'] = remaining
            _replace(self.api, self.directory/'spawn-ledger.json', ledger)

    def _holders(self, ledger):
        run = None
        pending_run = False
        other = False
        for entry in ledger['active']:
            launcher = _state_of(self.api, entry['launcher'])
            bundle.require(launcher == 'alive' and entry['phase'] != 'unknown', 'upgrade_holders_unconfirmed')
            if entry['phase'] != 'running':
                if entry['mode'] == 'run':
                    pending_run = True
                else:
                    other = True
                continue
            child = _state_of(self.api, entry['child'])
            bundle.require(child != 'unknown', 'upgrade_holders_unconfirmed')
            if child == 'ended':
                # The still-living launcher may be publishing its terminal fact.
                # Never promote even this exact departure into settlement ourselves.
                pending_run = True
            elif entry['mode'] == 'run':
                bundle.require(run is None, 'upgrade_holders_unconfirmed')
                run = entry
            else:
                other = True
        settling = False
        for entry in ledger['recent']:
            for identity in (entry['launcher'], entry['child']):
                if identity is not None:
                    state = _state_of(self.api, identity)
                    bundle.require(state != 'unknown', 'upgrade_holders_unconfirmed')
                    settling |= state == 'alive'
        return run, pending_run, other, settling

    def _instance(self):
        try:
            value = bundle.read_json(self.app_state/'instance.json', 1024 * 1024)
        except FileNotFoundError:
            return None
        expected = self.expected_context
        bundle.require(expected is not None and isinstance(value, dict) and type(value.get('schema')) is int
            and value['schema'] == 1 and value.get('instance_id') == expected['instance_id'], 'upgrade_holders_unconfirmed')
        context = value.get('context')
        bundle.require(isinstance(context, dict) and set(context) == set(expected)
            and all(type(context[name]) is type(expected[name]) and context[name] == expected[name]
                for name in expected.keys() - CONTEXT_PATHS)
            and all(bundle.same(bundle.absolute(context[name]), bundle.absolute(expected[name]))
                for name in CONTEXT_PATHS), 'upgrade_holders_unconfirmed')
        owner = value.get('owner')
        bundle.require(isinstance(owner, dict) and set(owner) == {'state','pid','created'} and owner['state'] == 'alive'
            and type(owner['pid']) is int and 0 < owner['pid'] <= 0xFFFFFFFF
            and type(owner['created']) in (float,int) and math.isfinite(owner['created']) and owner['created'] > 0,
            'upgrade_holders_unconfirmed')
        return value

    def _verify_running_image(self, identity, choices):
        handle = self.api.OpenProcess(0x1000, False, identity['pid'])
        if not handle:
            raise bundle.BundleError('upgrade_holders_unconfirmed')
        try:
            actual = _image_name(self.api, handle)
            bundle.require(str(_handle_creation(self.api, handle)) == identity['created_filetime'] and actual,
                           'upgrade_holders_unconfirmed')
            final = _final_image(self.api, actual)
            bundle.require(any(final == _final_image(self.api, self.target/name) for name in choices),
                           'upgrade_holders_unconfirmed')
        finally:
            self.api.CloseHandle(handle)

    def _request_stop(self, entry):
        """Return false while an admitted run is still publishing its receipt."""
        with bundle.guard_path(self.app_state):
            value = self._instance()
            if value is None:
                return False
            owner = value['owner']
            child = entry['child']
            creation = _app_creation(int(child['created_filetime']))
            if owner['pid'] != child['pid'] or owner['created'] != creation:
                # A newly created child may not have replaced a prior stopped
                # receipt yet. Do not send anything without the exact new owner.
                bundle.require(value.get('state') == 'stopped', 'upgrade_holders_unconfirmed')
                return False
            if value.get('stopping') or value.get('state') == 'stopped':
                return True
            self._verify_running_image(entry['launcher'], ('entry/CustomerLauncher.exe','entry/CustomerLauncherW.exe'))
            self._verify_running_image(child, ('runtime/python/pythonw.exe',))
            with _byte_lock(self.api, self.app_state/'commands.lock', create=True):
                latest = self._instance()
                bundle.require(latest is not None and latest['owner'] == owner, 'upgrade_holders_unconfirmed')
                if latest.get('stopping') or latest.get('state') == 'stopped':
                    return True
                bundle.require(_state_of(self.api, child) == 'alive', 'upgrade_holders_unconfirmed')
                try:
                    with _byte_lock(self.api, self.app_state/'run.lock'):
                        raise bundle.BundleError('upgrade_holders_unconfirmed')
                except bundle.BundleError as error:
                    if error.code != 'upgrade_holders_busy':
                        raise
                with _byte_lock(self.api, self.app_state/'control.json.lock', create=True):
                    _replace(self.api, self.app_state/'control.json', {'schema':1,'action':'stop',
                        'instance_id':latest['instance_id'],'owner':owner,'nonce':uuid.uuid4().hex}, allow_missing=True)
            return True

    def _quiet_application(self):
        with bundle.guard_path(self.app_state):
            value = self._instance()
            have_run_lock = False
            try:
                with _byte_lock(self.api, self.app_state/'run.lock'):
                    have_run_lock = True
            except FileNotFoundError:
                pass
            if value is None:
                bundle.require(not have_run_lock, 'upgrade_holders_unconfirmed')
                return
            # The supervisor only publishes this state after RoleManager.drained,
            # server shutdown, and close of the existing browser/writer resources.
            roles = value.get('roles')
            bundle.require(have_run_lock and value.get('state') == 'stopped' and value.get('ok') is True
                and value.get('stopping') is True and value.get('board_ready') is False
                and type(value.get('manual_writers')) is int and value['manual_writers'] == 0
                and type(value.get('inflight_requests')) is int and value['inflight_requests'] == 0
                and isinstance(roles, dict) and all(isinstance(role, dict) and role.get('running') is False
                    for role in roles.values()), 'upgrade_holders_unconfirmed')
            owner = value['owner']
            handle = self.api.OpenProcess(0x1000, False, owner['pid'])
            if not handle:
                bundle.require(ctypes.get_last_error() in MISSING_PROCESS, 'upgrade_holders_unconfirmed')
                return
            try:
                created, exit_code = _handle_creation(self.api, handle), wintypes.DWORD()
                bundle.require(created is not None and self.api.GetExitCodeProcess(handle, ctypes.byref(exit_code)),
                               'upgrade_holders_unconfirmed')
                bundle.require(_app_creation(created) != owner['created'] or exit_code.value != 259,
                               'upgrade_holders_busy')
            finally:
                self.api.CloseHandle(handle)

    def _wait_drained(self):
        deadline = time.monotonic() + 12.0
        requested = None
        while True:
            try:
                with self._admission():
                    maintenance, ledger = self._facts()
                    self._owned(maintenance)
                    self._reap_ended_mcp(ledger)
            except bundle.BundleError as error:
                # The launcher may still be flushing a terminal ledger record.
                # Keep waiting within the same deadline; never replace its fact.
                if error.code != 'upgrade_admission_busy' or time.monotonic() >= deadline:
                    raise
                continue
            run, pending, other, settling = self._holders(ledger)
            if run and requested != run['id']:
                try:
                    if self._request_stop(run):
                        requested = run['id']
                except bundle.BundleError as error:
                    if error.code != 'upgrade_holders_busy' or time.monotonic() >= deadline:
                        raise
            if other:
                raise bundle.BundleError('upgrade_holders_busy')
            if not (run or pending or settling or ledger['active']):
                return
            bundle.require(time.monotonic() < deadline, 'upgrade_holders_busy')
            time.sleep(.05)

    def fence(self, generation):
        bundle.require(self._exclusive and _uuid(generation), 'upgrade_gate_unconfirmed')
        with self._admission():
            maintenance, _ = self._facts()
            self._owned(maintenance)
            next_marker = dict(maintenance, schema=2, generation=generation)
            if next_marker != maintenance:
                _replace(self.api, self.directory/'maintenance.json', next_marker)
            self._generation = generation

    def clear(self):
        bundle.require(self._exclusive and self._generation, 'upgrade_gate_unconfirmed')
        self._quiet_application()
        _scan_target_processes(self.api, self.target)
        with self._admission():
            maintenance, ledger = self._facts()
            self._owned(maintenance)
            bundle.require(maintenance.get('generation') == self._generation and not any(self._holders(ledger)),
                           'upgrade_holders_unconfirmed')
            _replace(self.api, self.directory/'maintenance.json', dict(maintenance, active=False, token=''))


def maintenance_status(target, install_id):
    """Read the transaction marker; counts do not imply that a lease is free."""
    gate = _Gate(target, install_id)
    with bundle.guard_path(gate.directory), gate._admission():
        maintenance, ledger = gate._facts()
        return dict(active=maintenance['active'], token=maintenance['token'], generation=maintenance.get('generation'),
                    active_count=len(ledger['active']), recent_count=len(ledger['recent']))


@contextmanager
def acquire(target, install_id, token, *, expected_context=None):
    """Keep maintenance across failures and hold the original code lease exclusively."""
    gate = _Gate(target, install_id, token, expected_context)
    with bundle.guard_path(gate.directory):
        gate._mark()
        gate._wait_drained()
        with _open(gate.api, gate.directory/'code.lease', busy='upgrade_code_lease_busy'):
            gate._exclusive = True
            try:
                with gate._admission():
                    maintenance, ledger = gate._facts()
                    gate._owned(maintenance)
                    bundle.require(not any(gate._holders(ledger)), 'upgrade_holders_unconfirmed')
                gate._quiet_application()
                _scan_target_processes(gate.api, gate.target)
                yield gate
            finally:
                gate._exclusive = False
