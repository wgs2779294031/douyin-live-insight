"""External manager for the B1 to B2 transition, rollback and recovery."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import hashlib
import os
from pathlib import Path
import shutil
import sys
import uuid

try:
    from . import customer_bundle as bundle, customer_upgrade_gate as barrier
    from .customer_install import copy_verified, _context, _native_path_preflight
except ImportError:
    import customer_bundle as bundle
    import customer_upgrade_gate as barrier
    from customer_install import copy_verified, _context, _native_path_preflight


B1, B2 = bundle.VERSION_B1, bundle.VERSION_B2
PRODUCT = ('app', 'runtime', 'assets', 'notices', 'payload-manifest.json', 'installation.json',
           'installed.manifest.json', 'owner.json')
STABLE = ('entry', 'AGENT_INSTALL.md')
DIRECTORIES = {'app', 'runtime', 'assets', 'notices', 'entry', 'manager-cache'}
OWNER_KEYS = {'schema','kind','install_id','target','target_identity','version','channel',
              'bundle_manifest_sha256','manager_cache','manager_manifest_sha256'}
JOURNAL_KEYS = {'schema','kind','target','target_identity','install_id','id','token','request_sha256','action',
                'phase','cursor','workspace','old','new','previous_id','migrate','plan'}
JOURNAL_LIMIT = 131072


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def _uuid(value):
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except ValueError:
        return False


def _kernel():
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    api.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    api.CreateFileW.restype = wintypes.HANDLE
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    api.MoveFileExW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
    api.MoveFileExW.restype = wintypes.BOOL
    return api


def _move(source, dest, *, replace=False):
    with bundle.guard_path(source.parent), bundle.guard_path(dest.parent):
        if not replace:
            bundle.absent(dest)
        if not _kernel().MoveFileExW(str(source), str(dest), 8 | int(replace)):
            code = ctypes.get_last_error()
            if code in (5, 32, 33):
                raise bundle.BundleError('upgrade_files_busy')
            raise bundle.BundleError('upgrade_move_unconfirmed')


def _journal_save(path, value, *, initial=False):
    raw = bundle.encode(value)
    bundle.require(len(raw) <= JOURNAL_LIMIT, 'upgrade_journal_invalid')
    if not initial:
        bundle.no_link(path)
    temporary = path.with_name('u-'+uuid.uuid4().hex+'.tmp')
    bundle.write_new(temporary, raw)
    _move(temporary, path, replace=not initial)
    bundle.require(bundle.read_bytes(path, JOURNAL_LIMIT) == raw, 'upgrade_journal_unconfirmed')


@contextmanager
def _transaction_lock(gate):
    path = gate/'update.lock'
    if os.path.lexists(path):
        bundle.no_link(path)
    api = _kernel()
    handle = api.CreateFileW(str(path), 0xC0000000, 0, None, 4, 0x00200000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise bundle.BundleError('upgrade_transaction_busy' if ctypes.get_last_error() in (32,33)
                                 else 'upgrade_lock_unconfirmed')
    try:
        bundle.no_link(path)
        yield
    finally:
        api.CloseHandle(handle)


def _owner(target, owner, identity):
    bundle.require(isinstance(owner, dict) and set(owner) == OWNER_KEYS and type(owner['schema']) is int
        and owner['schema'] == 1 and owner['kind'] == 'customer_installation_owner'
        and _uuid(owner['install_id']) and owner['target'] == str(target) and owner['target_identity'] == identity
        and owner['version'] in (B1, B2) and owner['channel'] == 'local-candidate'
        and all(isinstance(owner[key], str) and bundle.HEX.fullmatch(owner[key])
                for key in ('bundle_manifest_sha256','manager_manifest_sha256')), 'upgrade_owner_unconfirmed')
    return owner


def _workspace(target, identifier):
    bundle.require(_uuid(identifier), 'upgrade_journal_invalid')
    return target.parent/f'.{target.name}.upgrade-{identifier}'


def _paths(target, install_id):
    legacy = target.parent/f'.{target.name}.manager-{install_id}'
    return legacy, legacy.with_name(legacy.name+'-b2')


def _select_manifest(path, sha, version):
    manifest, raw, _, files, directories = bundle.load_manifest(path, sha)
    bundle.require(manifest['version'] == version and manifest['channel'] == 'local-candidate',
                   'upgrade_version_unsupported')
    return manifest, raw, files, directories


def _initial_digest(target, journal):
    identity = journal['target_identity']
    owner = {'target':str(target), 'install_id':journal['install_id']}
    intent, cache = bundle.initial_install_fact(target, owner, identity)
    bundle.require(intent.get('manager_cache') == str(cache), 'installation_completion_unconfirmed')
    initial = intent['bundle_manifest_sha256']
    _select_manifest(cache/'bundle.manifest.json', initial, B1)
    return initial


def _check_binding(target, identity, journal, requested_sha):
    bundle.require(isinstance(journal, dict) and set(journal) == JOURNAL_KEYS
        and type(journal['schema']) is int and journal['schema'] == 1
        and journal['kind'] == 'customer_upgrade_journal' and journal['target'] == str(target)
        and journal['target_identity'] == identity and _uuid(journal['id']) and _uuid(journal['install_id'])
        and _uuid(journal['token']) and journal['token'] != journal['id']
        and journal['request_sha256'] == requested_sha and journal['action'] in ('upgrade','rollback')
        and journal['phase'] in ('preparing','ready','committed','done','aborted')
        and type(journal['cursor']) is int and journal['cursor'] >= 0 and type(journal['migrate']) is bool,
        'upgrade_journal_invalid')
    old, new = journal['old'], journal['new']
    for owner in (old,new):
        _owner(target, owner, identity)
        bundle.require(owner['install_id'] == journal['install_id'], 'upgrade_journal_invalid')
    legacy, stable = _paths(target, old['install_id'])
    initial = _initial_digest(target, journal)
    bundle.require((old['version'], new['version']) == ((B1,B2) if journal['action'] == 'upgrade' else (B2,B1))
        and journal['migrate'] == (old['manager_cache'] == str(legacy))
        and (journal['previous_id'] is None if journal['action'] == 'upgrade' else _uuid(journal['previous_id'])),
        'upgrade_journal_invalid')
    for owner in (old,new):
        observed = initial if owner['version'] == B1 else requested_sha
        legacy_owner = owner is old and journal['migrate']
        bundle.require(owner['bundle_manifest_sha256'] == observed
            and owner['manager_cache'] == str(legacy if legacy_owner else stable)
            and owner['manager_manifest_sha256'] == (initial if legacy_owner else requested_sha), 'upgrade_journal_invalid')
    work = _workspace(target, journal['id'])
    bundle.require(journal['workspace'] == str(work) and (not journal['migrate'] or journal['action'] == 'upgrade'),
                   'upgrade_journal_invalid')
    return work, stable


def _selection(rows, dirs, name):
    return ({key:row for key,row in rows.items() if key.startswith(name+'/')},
            {key for key in dirs if key.startswith(name+'/')})


def _shape(manifest):
    rows, dirs = manifest
    return ({name.removeprefix('payload/') if name.startswith('payload/') else name:row for name,row in rows.items()
             if row['component'] in {'product','entry','guide'}},
            {name.removeprefix('payload/') if name.startswith('payload/') else name for name in dirs
             if name.startswith('payload/') or name == 'entry'})


def _reference(path, directory, *, with_digest=False):
    info = bundle.no_link(path, directory)
    if directory:
        with bundle.guard_path(path) as identity:
            return identity
    with bundle._guard_file_record(path) as record:
        volume, identifier = record['identity']
        out = {'volume_serial':volume, 'file_id':identifier, 'bytes':info.st_size}
        if with_digest:
            out['sha256'] = digest(bundle.read_bytes(path))
        return out


def _units(journal):
    stable = STABLE if journal['migrate'] else ()
    return PRODUCT + stable, PRODUCT + stable + (('manager-cache',) if journal['migrate'] else ())


def _sequence(target, work, cache, journal):
    old_units, new_units = _units(journal)
    return [(('old',name), target/name, work/'previous'/name) for name in old_units] + [
        (('new',name), work/'stage'/name, cache if name == 'manager-cache' else target/name)
        for name in new_units]


def _positions(steps, cursor):
    return {key:dest if index < cursor else source for index,(key,source,dest) in enumerate(steps)}


def _root_names(root, *, skip=()):
    names = set()
    with bundle.guard_path(root):
        with os.scandir(root) as entries:
            for entry in entries:
                bundle.relative(entry.name)
                bundle.no_link(entry.path, entry.is_dir(follow_symlinks=False))
                if entry.name not in skip:
                    names.add(entry.name)
    return names


def _placement(target, work, cache, journal, steps, cursor):
    positions = _positions(steps, cursor)
    expected_paths = set(positions.values())
    vacant = ({source for _,source,_ in steps} | {dest for _,_,dest in steps}) - expected_paths
    if any(os.path.lexists(path) for path in vacant):
        return None
    stay = set() if journal['migrate'] else set(STABLE)
    parents = ((target, ('state','.gate'), stay), (work/'stage', (), set()), (work/'previous', (), set()))
    for root, skip, retained in parents:
        if _root_names(root, skip=skip) != retained | {path.name for path in expected_paths if path.parent == root}:
            return None
    for (side,name), path in positions.items():
        if not os.path.lexists(path):
            return None
        reference = journal['plan'][side][name]
        if _reference(path, name in DIRECTORIES) != {key:value for key,value in reference.items() if key != 'sha256'}:
            return None
    return positions


def _tree_check(path, name, shape):
    rows, dirs = shape
    subrows, subdirs = _selection(rows, dirs, name)
    observed_files, observed_dirs = bundle.scan_tree(path)
    prefix = name + '/'
    bundle.require(observed_files == {item.removeprefix(prefix) for item in subrows}
        and observed_dirs == {item.removeprefix(prefix) for item in subdirs}, 'upgrade_inventory_mismatch')
    for key,row in subrows.items():
        bundle.check_file(path/key.removeprefix(prefix), row, deep=True)


def _verify_contents(positions, shapes, journal):
    for (side,name), path in positions.items():
        if name == 'manager-cache':
            bundle.manager_inventory(path, journal['new'], deep=True)
        elif name in DIRECTORIES:
            _tree_check(path, name, shapes[side])
        else:
            row = journal['plan'][side][name]
            bundle.check_file(path, row, deep=True)
            if name in ('owner.json','installed.manifest.json'):
                owner = journal[side]
                if name == 'installed.manifest.json':
                    bundle.require(row['sha256'] == owner['bundle_manifest_sha256'], 'upgrade_journal_invalid')
                else:
                    bundle.require(bundle.read_json(path, 65536) == owner, 'upgrade_journal_invalid')
            elif name not in ('installation.json',):
                listed = shapes[side][0][name]
                bundle.require((row['bytes'],row['sha256']) == (listed['bytes'],listed['sha256']), 'upgrade_journal_invalid')
    for side in ('old','new'):
        descriptor = bundle.read_json(positions[(side,'installation.json')], 65536)
        template = bundle.read_json(positions[(side,'app')]/'baseline/installation-layout.example.json', 65536)
        bundle.require({k:v for k,v in descriptor.items() if k not in {'ports','browser'}} ==
                       {k:v for k,v in template.items() if k not in {'ports','browser'}}, 'upgrade_descriptor_unconfirmed')


def _plan_positions(target, work, cache, journal, *, deep=True):
    plan = journal['plan']
    old_units, new_units = _units(journal)
    bundle.require(isinstance(plan, dict) and set(plan) == {'work','stage','previous','old','new'}
        and set(plan['old']) == set(old_units) and set(plan['new']) == set(new_units),
        'upgrade_journal_invalid')
    for key, path in (('work',work),('stage',work/'stage'),('previous',work/'previous')):
        bundle.require(_reference(path, True) == plan[key], 'upgrade_workspace_changed')
    steps = _sequence(target, work, cache, journal)
    cursor = journal['cursor']
    bundle.require(cursor <= len(steps) and (journal['phase'] == 'ready' or cursor == len(steps)),
                   'upgrade_journal_invalid')
    positions = _placement(target, work, cache, journal, steps, cursor)
    if positions is None and journal['phase'] == 'ready' and cursor < len(steps):
        cursor += 1
        positions = _placement(target, work, cache, journal, steps, cursor)
    bundle.require(positions is not None, 'upgrade_placement_unconfirmed')
    shapes = {}
    for side in ('old','new'):
        owner = journal[side]
        _, _, rows, dirs = _select_manifest(positions[(side,'installed.manifest.json')],
                                           owner['bundle_manifest_sha256'], owner['version'])
        shapes[side] = _shape((rows, dirs))
    choices = [bundle.read_json(positions[(side,'installation.json')], 65536) for side in ('old','new')]
    for key in ('ports','browser'):
        bundle.require(choices[0][key] == choices[1][key], 'upgrade_settings_changed')
    if deep:
        _verify_contents(positions, shapes, journal)
    return steps, cursor, choices[0]


def _stage(work, target, original, candidate, selected, journal):
    """Copy only the selected program and its immutable sidecars."""
    stage = work/'stage'
    shape = _shape((selected['files'], selected['directories']))
    _, new_units = _units(journal)
    selected_names = set(new_units)
    bundle.make_directories(stage, {name for name in shape[1] if name.split('/')[0] in selected_names})
    for name,row in sorted(shape[0].items()):
        if name == 'installation.json' or name.split('/')[0] not in selected_names:
            continue
        path = row['path'] if journal['action'] == 'upgrade' else name
        copy_verified(selected['root']/path, stage/name, row)
    descriptor = bundle.read_json(stage/'app/baseline/installation-layout.example.json', 65536)
    if journal['action'] == 'upgrade':
        template_row = selected['files']['payload/installation.json']
        raw_template = bundle.read_bytes(selected['root']/'payload/installation.json', 65536)
        bundle.require(len(raw_template) == template_row['bytes'] and digest(raw_template) == template_row['sha256']
            and bundle.parse(raw_template) == descriptor, 'upgrade_descriptor_unconfirmed')
    descriptor.update(ports=original['ports'],browser=original['browser'])
    bundle.descriptor_layout(target, descriptor, journal['new'])
    for name,raw in (('owner.json',bundle.encode(journal['new'])), ('installation.json',bundle.encode(descriptor)),
                     ('installed.manifest.json',selected['raw'])):
        bundle.write_new(stage/name, raw)
    if journal['migrate']:
        cache = stage/'manager-cache'
        bundle.make_directories(stage, {'manager-cache'} | {'manager-cache/'+name for name in candidate['directories']
            if name == 'manager' or name.startswith('manager/')})
        for name,row in sorted(candidate['files'].items()):
            if row['component'] == 'manager':
                copy_verified(candidate['root']/name, cache/name, row)
        owner = journal['new']
        bundle.write_new(cache/'bundle.manifest.json', candidate['raw'])
        bundle.write_new(cache/'cache-owner.json', bundle.encode({'schema':1,'kind':'customer_manager_cache_owner',
            'install_id':owner['install_id'],'target':owner['target'],'target_identity':owner['target_identity'],
            'bundle_manifest_sha256':owner['manager_manifest_sha256']}))


def _previous_record(target, identity, sha):
    pointer_path = target/'.gate/previous.json'
    bundle.require(os.path.lexists(pointer_path), 'upgrade_previous_unavailable')
    pointer = bundle.read_json(pointer_path, 65536)
    bundle.require(isinstance(pointer, dict) and set(pointer) == {'id','commit_sha256'}
        and _uuid(pointer['id']), 'upgrade_previous_unconfirmed')
    work = _workspace(target, pointer['id'])
    raw = bundle.read_bytes(work/'commit.json', JOURNAL_LIMIT)
    bundle.require(digest(raw) == pointer['commit_sha256'], 'upgrade_previous_unconfirmed')
    journal = bundle.parse(raw)
    _check_binding(target, identity, journal, sha)
    bundle.require(journal['phase'] == 'committed' and journal['action'] == 'upgrade'
        and journal['id'] == pointer['id'] and _reference(work,True) == journal['plan']['work']
        and _reference(work/'previous',True) == journal['plan']['previous'], 'upgrade_previous_unconfirmed')
    return work, journal


def _get_previous(target, identity, sha):
    work, journal = _previous_record(target,identity,sha)
    root = work/'previous'
    names = _units(journal)[0]
    bundle.require(_root_names(root) == set(names), 'upgrade_previous_unconfirmed')
    for name in names:
        bundle.require(_reference(root/name, name in DIRECTORIES, with_digest=True) == journal['plan']['old'][name],
                       'upgrade_previous_unconfirmed')
    manifest, raw, files, dirs = _select_manifest(root/'installed.manifest.json',
        journal['old']['bundle_manifest_sha256'], B1)
    positions = {('old',name):root/name for name in names}
    expected_shape = _shape((files, dirs))
    for _,name in positions:
        if name in DIRECTORIES:
            _tree_check(root/name, name, expected_shape)
    return dict(manifest=manifest,raw=raw,files=files,directories=dirs,root=root,id=journal['id'])


def _prepare(action, source, target, identity, sha, path, marker, checkpoint):
    current = bundle.inspect_install(target, deep=True)
    old = _owner(target, current['owner'], identity)
    install_id = old['install_id']
    marker['install_id'] = install_id
    maintenance = barrier.maintenance_status(target, install_id)
    bundle.require(not maintenance['active'], 'upgrade_maintenance_owned_elsewhere')
    if action == 'upgrade' and old['version'] == B2:
        bundle.require(old['bundle_manifest_sha256'] == sha, 'upgrade_version_conflict')
        return None, 'already_upgraded'
    if action == 'rollback' and old['version'] == B1:
        _, stable = _paths(target, install_id)
        bundle.require(old['manager_cache'] == str(stable) and old['manager_manifest_sha256'] == sha,
                       'upgrade_previous_unavailable')
        return None, 'already_rolled_back'
    legacy, cache = _paths(target, install_id)
    migrate = old['manager_cache'] == str(legacy)
    bundle.require((action == 'upgrade' and old['version'] == B1) or
                   (action == 'rollback' and old['version'] == B2 and not migrate), 'upgrade_previous_unavailable')
    bundle.require(migrate or old['manager_manifest_sha256'] == sha, 'upgrade_version_conflict')
    if migrate:
        bundle.absent(target/'.gate/previous.json')
    elif action == 'upgrade':
        _previous_record(target,identity,sha)
    candidate = bundle.verify_bundle(source, sha, deep=False)
    candidate['root'] = source
    bundle.require(candidate['manifest']['version'] == B2 and candidate['manifest']['channel'] == 'local-candidate',
                   'upgrade_version_unsupported')
    selected = candidate if action == 'upgrade' else _get_previous(target, identity, sha)
    transaction_id = str(uuid.uuid4())
    work = _workspace(target, transaction_id)
    bundle.disjoint(source,target,work,cache)
    bundle.absent(work)
    if migrate:
        bundle.absent(cache)
    original = bundle.read_json(target/'installation.json', 65536)
    next_owner = dict(old, version=selected['manifest']['version'], bundle_manifest_sha256=digest(selected['raw']),
                      manager_cache=str(cache), manager_manifest_sha256=sha)
    browser = original['browser'].get('path')
    _native_path_preflight(target, cache, candidate, browser)
    needed = sum(row['bytes'] for row in selected['files'].values() if row['component'] == 'product')
    if migrate:
        needed += sum(row['bytes'] for row in candidate['files'].values() if row['component'] in {'entry','guide','manager'})
    bundle.require(shutil.disk_usage(target.parent).free >= needed + 32*bundle.BLOCK, 'upgrade_insufficient_space')
    journal = dict(schema=1, kind='customer_upgrade_journal', target=str(target), target_identity=identity,
        install_id=install_id,id=transaction_id,token=str(uuid.uuid4()),request_sha256=sha,action=action,
        phase='preparing',cursor=0,
        workspace=str(work),old=old,new=next_owner,previous_id=selected.get('id'),migrate=migrate,plan=None)
    _check_binding(target, identity, journal, sha)
    _journal_save(path, journal, initial=not os.path.lexists(path))
    marker.update(transaction_id=transaction_id, phase='preparing')
    checkpoint('after_preparing')
    work.mkdir()
    bundle.make_directories(work, {'stage','previous'})
    _stage(work,target,original,candidate,selected,journal)
    old_units, new_units = _units(journal)
    journal['plan'] = {'work':_reference(work,True),'stage':_reference(work/'stage',True),
        'previous':_reference(work/'previous',True),
        'old':{name:_reference(target/name,name in DIRECTORIES,with_digest=True) for name in old_units},
        'new':{name:_reference(work/'stage'/name,name in DIRECTORIES,with_digest=True) for name in new_units}}
    journal['phase'] = 'ready'
    # The old namespace is unchanged while the staged files are verified.
    _plan_positions(target,work,cache,journal,deep=False)
    _journal_save(path,journal)
    marker['phase'] = 'ready'
    checkpoint('after_prepared')
    return journal,None


def _record_commit(target, work, journal):
    record = work/'commit.json'
    raw = bundle.encode(journal)
    if not os.path.lexists(record):
        _journal_save(record, journal, initial=True)
    bundle.require(bundle.read_bytes(record, JOURNAL_LIMIT) == raw, 'upgrade_commit_unconfirmed')
    if journal['action'] == 'upgrade':
        path = target/'.gate/previous.json'
        pointer = dict(id=journal['id'],commit_sha256=digest(raw))
        if not os.path.lexists(path):
            _journal_save(path,pointer,initial=True)
        elif bundle.read_json(path,65536) != pointer:
            _previous_record(target,journal['target_identity'],journal['request_sha256'])
            _journal_save(path,pointer)


def _finish(target, work, path, journal, gate, checkpoint):
    checked = bundle.inspect_install(target,deep=True)
    bundle.require(checked['owner'] == journal['new'], 'upgrade_commit_unconfirmed')
    if journal['phase'] != 'committed':
        journal['phase'] = 'committed'
        _journal_save(path,journal)
        checkpoint('after_commit')
    _record_commit(target,work,journal)
    checkpoint('before_clear')
    if gate is not None:
        gate.clear()
    checkpoint('after_clear')
    journal['phase'] = 'done'
    _journal_save(path,journal)


def _resume(target, identity, sha, path, journal, marker, checkpoint):
    work, cache = _check_binding(target,identity,journal,sha)
    marker.update(transaction_id=journal['id'],phase=journal['phase'],install_id=journal['install_id'])
    token, epoch = journal['token'], journal['id']
    status = barrier.maintenance_status(target,journal['install_id'])
    if journal['phase'] == 'preparing':
        bundle.require(journal['cursor'] == 0 and journal['plan'] is None and not status['active'],
                       'upgrade_journal_invalid')
        current = bundle.inspect_install(target,deep=True)
        bundle.require(current['owner'] == journal['old'], 'upgrade_commit_unconfirmed')
        journal['phase'] = 'aborted'
        _journal_save(path,journal)
        marker['phase'] = 'aborted'
        return 'staging_aborted_original_kept'
    bundle.require(journal['phase'] in ('ready','committed'), 'upgrade_journal_invalid')
    steps, cursor, old_descriptor = _plan_positions(target,work,cache,journal)
    if cursor and not (journal['phase']=='committed' and not status['active']):
        bundle.require(status['active'] and status['token'] == token and status['generation'] == epoch,
                       'upgrade_maintenance_unconfirmed')
    if journal['phase']=='committed' and not status['active']:
        bundle.require(status['generation'] == epoch,'upgrade_maintenance_unconfirmed')
        _finish(target,work,path,journal,None,checkpoint)
    else:
        context = _context(bundle.descriptor_layout(target,old_descriptor,journal['old']))
        with barrier.acquire(target,journal['install_id'],token,expected_context=context) as gate:
            gate.fence(epoch)
            checkpoint('after_fence')
            if cursor != journal['cursor']:
                journal['cursor'] = cursor
                _journal_save(path,journal)
            for index in range(cursor,len(steps)):
                (side,name), origin, dest = steps[index]
                expected = journal['plan'][side][name]
                bundle.require(_reference(origin,name in DIRECTORIES) ==
                    {key:value for key,value in expected.items() if key != 'sha256'}, 'upgrade_placement_unconfirmed')
                checkpoint(f'before_move:{index}')
                _move(origin,dest)
                bundle.require(_reference(dest,name in DIRECTORIES) ==
                    {key:value for key,value in expected.items() if key != 'sha256'}, 'upgrade_placement_unconfirmed')
                checkpoint(f'after_move:{index}')
                journal['cursor'] = index+1
                _journal_save(path,journal)
                checkpoint(f'after_cursor:{index}')
            _finish(target,work,path,journal,gate,checkpoint)
    marker['phase'] = 'done'
    return 'upgraded' if journal['action'] == 'upgrade' else 'rolled_back'


def _result(code, marker, *, exit_code=0, next_steps=()):
    return {'ok':exit_code == 0, 'code':code, 'exit':exit_code, **marker, 'next_steps':list(next_steps)}


def _status(target, install_id):
    status = barrier.maintenance_status(target,install_id)
    return {key:value for key,value in status.items() if key != 'token'}


def transact(action, source, target, sha, *, checkpoint=None):
    """All persisted paths are derived from the target and ids; no journal paths are executed."""
    checkpoint = checkpoint or (lambda _: None)
    marker = {'phase':None,'transaction_id':None,'install_id':None}
    try:
        bundle.require(action in ('upgrade','rollback','recover','upgrade-status'), 'upgrade_arguments_invalid')
        source, target = bundle.absolute(source), bundle.absolute(target)
        bundle.disjoint(source,target)
        _select_manifest(source/'bundle.manifest.json', sha, B2)
        gate = target/'.gate'
        path = gate/'update.json'
        with bundle.guard_path(target) as identity, bundle.guard_path(gate):
            if action == 'upgrade-status':
                # Status reads an atomically replaced fact, not partially moved metadata.
                if not os.path.lexists(path):
                    owner = bundle.read_json(target/'owner.json',65536)
                    _owner(target,owner,identity)
                    marker['install_id'] = owner['install_id']
                    marker['maintenance'] = _status(target,owner['install_id'])
                    marker['version'] = owner['version']
                    return _result('no_upgrade_transaction',marker)
                journal = bundle.read_json(path,JOURNAL_LIMIT)
                _check_binding(target,identity,journal,sha)
                marker.update(transaction_id=journal['id'], phase=journal['phase'],cursor=journal['cursor'],
                    install_id=journal['install_id'], maintenance=_status(target,journal['install_id']))
                marker.update(from_version=journal['old']['version'], to_version=journal['new']['version'],
                    version=(journal['new']['version'] if journal['phase']=='done' else
                             journal['old']['version'] if journal['phase'] in ('preparing','aborted') else None))
                return _result('upgrade_transaction_'+journal['phase'],marker,
                    next_steps=('Run recover with this same trusted bundle to continue.',)
                    if journal['phase'] not in ('done','aborted') else ())
            with _transaction_lock(gate):
                journal = bundle.read_json(path,JOURNAL_LIMIT) if os.path.lexists(path) else None
                if journal is not None:
                    _check_binding(target,identity,journal,sha)
                    marker.update(transaction_id=journal['id'],phase=journal['phase'],install_id=journal['install_id'])
                    if journal['phase'] not in ('done','aborted'):
                        bundle.require(action in ('recover',journal['action']), 'upgrade_recovery_required')
                        if journal['phase']=='preparing':
                            bundle.require(action=='recover','upgrade_recovery_required')
                        return _result(_resume(target,identity,sha,path,journal,marker,checkpoint),marker)
                if action=='recover':
                    return _result('no_pending_upgrade',marker)
                new, code = _prepare(action,source,target,identity,sha,path,marker,checkpoint)
                if code is None:
                    code = _resume(target,identity,sha,path,new,marker,checkpoint)
                return _result(code,marker)
    except (OSError,ValueError,TypeError,KeyError,OverflowError) as error:
        code = error.code if isinstance(error,bundle.BundleError) else 'upgrade_unconfirmed'
        busy = code.endswith('_busy') or code in ('upgrade_recovery_required','upgrade_maintenance_owned_elsewhere',
            'upgrade_holders_unconfirmed','upgrade_target_process_unconfirmed','upgrade_files_busy')
        if code in ('upgrade_holders_busy','upgrade_code_lease_busy','upgrade_target_process_busy'):
            steps = ('Disconnect the target MCP client or close the identified installation program using its normal controls.',
                     'Retry recover using the same trusted bundle and target.')
        elif code=='upgrade_insufficient_space':
            steps = ('Make sufficient space on the target drive, then retry. Keep the transaction evidence.',)
        elif code=='upgrade_transaction_busy':
            steps = ('Wait for the current management command to finish and run upgrade-status.',)
        else:
            steps = ('Keep the target and its transaction workspace. Use upgrade-status, then recover with the same trusted bundle.',
                     'If recovery reports unconfirmed evidence, provide the code and transaction id to support.')
        return _result(code,marker,exit_code=75 if busy else 78,next_steps=steps)


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise bundle.BundleError('upgrade_arguments_invalid')


def main(argv=None):
    parser = _Parser(description=__doc__)
    parser.add_argument('action', choices=('upgrade','rollback','recover','upgrade-status'))
    for name in ('bundle','target','manifest-sha256'):
        parser.add_argument('--'+name,required=True)
    try:
        args = parser.parse_args(argv)
        result = transact(args.action,args.bundle,args.target,args.manifest_sha256)
    except bundle.BundleError as error:
        result = _result(error.code,{},exit_code=78)
    sys.stdout.buffer.write(bundle.encode(result))
    return result['exit']


if __name__ == '__main__':
    raise SystemExit(main())
