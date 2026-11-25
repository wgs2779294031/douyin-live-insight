"""External standard-library first installer."""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import uuid

try:
    from . import customer_bundle as bundle
except ImportError:
    import customer_bundle as bundle


def copy_verified(source, target, row):
    with bundle.guard_path(Path(source).parent), bundle.guard_path(Path(target).parent):
        before = bundle.no_link(source)
        digest, count = hashlib.sha256(), 0
        with Path(source).open('rb') as incoming, Path(target).open('xb') as outgoing:
            bundle.require(bundle.signature(os.fstat(incoming.fileno())) == bundle.signature(before), 'bundle_input_changed')
            while chunk := incoming.read(bundle.BLOCK):
                count += len(chunk)
                bundle.require(count <= row['bytes'], 'bundle_input_changed')
                outgoing.write(chunk)
                digest.update(chunk)
            bundle.require(bundle.signature(os.fstat(incoming.fileno())) == bundle.signature(before), 'bundle_input_changed')
            outgoing.flush()
            os.fsync(outgoing.fileno())
        bundle.require(bundle.signature(bundle.no_link(source)) == bundle.signature(before)
            and count == row['bytes'] and digest.hexdigest() == row['sha256'], 'bundle_integrity_mismatch')
        bundle.check_file(target, row, deep=True)


def _mkdir(root, names):
    bundle.make_directories(root, names)


def _intent_write(path, value):
    # Only this invocation's already-owned intention is replaced. Temp is new.
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    bundle.write_new(temporary, bundle.encode(value))
    os.replace(temporary, path)
    bundle.require(bundle.read_json(path, 65536) == value, 'install_intent_unconfirmed')


def _context(layout):
    paths = {name:str(layout[name]) for name in ('root','config','data','roster','dist','python','profile')}
    identity = {name:os.path.normcase(paths[name]) for name in ('root','data','config')}
    instance = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('ascii')).hexdigest()
    return dict(paths, schema=1, instance_id=instance, port=layout['port'], cdp_port=layout['cdp_port'])


def _native_path_preflight(target, cache, checked, browser):
    """Match the fixed .NET 4 launcher's <240 UTF-16 path contract.

    Only paths read/mapped by that entry are included. Deep product resources
    (for example vendored Node modules) are not subjected to this entry limit.
    """
    fixed = ('owner.json','installed.manifest.json','installation.json',
        'entry','entry/CustomerLauncher.exe','entry/CustomerLauncherW.exe',
        '.gate','.gate/admission.lock','.gate/code.lease','.gate/maintenance.json','.gate/spawn-ledger.json',
        'app','app/scripts/local_app.py','app/scripts/local_mcp.py','app/scripts/local_doctor.py',
        'runtime/python/python.exe','runtime/python/pythonw.exe','runtime/node/node.exe','runtime/ffmpeg/ffmpeg.exe',
        'assets/models/faster-whisper-small','state','state/cache','state/config.json','state/anchors.json',
        'state/data','state/data/.local_app/context.json','state/data/.local_app/browser-profile','app/web/dist')
    paths = [target, cache, cache/'cache-owner.json', cache/'bundle.manifest.json']
    paths.extend(target/name for name in fixed)
    paths.extend(cache/name for name,row in checked['files'].items() if row['component'] == 'manager')
    paths.extend(cache/name for name in checked['directories'] if name == 'manager' or name.startswith('manager/'))
    if browser is not None:
        paths.append(browser)
    bundle.require(all(len(str(path).encode('utf-16-le'))//2 < 240 for path in paths), 'install_native_path_too_long')


def _seed(stage, target, descriptor, owner):
    layout = bundle.descriptor_layout(target, descriptor, owner)
    config = bundle.read_json(stage/'app/config.example.json', 1024*1024)
    bundle.require(isinstance(config, dict) and isinstance(config.get('llm'), dict)
        and isinstance(config.get('feishu'), dict) and isinstance(config.get('anchor'), dict), 'install_seed_invalid')
    # The only seed is the shipped public example, never seller/customer state.
    config.update(rooms=[], cookie='', web_rid='')
    config['llm']['enabled'] = False
    for provider in config['llm'].get('providers', {}).values():
        if isinstance(provider, dict):
            provider['api_key'] = ''
    config['feishu']['enabled'] = False
    for key in ('webhook_url','bearer_token','app_id','app_secret','alert_chat_id','app_token','table_id'):
        config['feishu'][key] = ''
    config['anchor'].update(enabled=False, profile_dir=str(target/'state/anchor_profiles'))
    _mkdir(stage, {'state','state/cache','state/data','state/data/.local_app','state/anchor_profiles','.gate'})
    bundle.write_new(stage/'state/config.json', bundle.encode(config))
    bundle.write_new(stage/'state/anchors.json', bundle.encode({'version':1,'updated_at':'','anchors':[]}))
    bundle.write_new(stage/'state/data/.local_app/context.json', bundle.encode(_context(layout)))
    bundle.write_new(stage/'.gate/admission.lock', b'')
    bundle.write_new(stage/'.gate/code.lease', b'')
    maintenance = {'schema':1,'kind':'customer_maintenance','install_id':owner['install_id'],'active':False,'token':''}
    if owner['version'] == bundle.VERSION_B2:
        maintenance.update(schema=2,generation=owner['install_id'])
    bundle.write_new(stage/'.gate/maintenance.json', bundle.encode(maintenance))
    bundle.write_new(stage/'.gate/spawn-ledger.json', bundle.encode({'schema':1,'kind':'customer_spawn_ledger',
        'install_id':owner['install_id'],'active':[],'recent':[]}))


def install(bundle_dir, target, manifest_sha256, *, port=8765, cdp_port=29222, browser=None):
    """Install only into a new/empty explicit target; preserve all owned failures."""
    target, source = bundle.absolute(target), bundle.absolute(bundle_dir)
    bundle.disjoint(source, target)
    # An existing installation has a wholly read-only idempotent path.
    try:
        bundle.no_link(target, True)
    except FileNotFoundError:
        existing = False
    else:
        existing = True
        if any(target.iterdir()):
            current = bundle.inspect_install(target, deep=True)
            bundle.require(current['owner']['bundle_manifest_sha256'] == manifest_sha256, 'install_version_conflict')
            return {'ok':True,'code':'already_installed','install_id':current['owner']['install_id'],
                'target':str(target),'version':current['owner']['version'],'state_preserved':True,
                'existing_settings_preserved':True,'ports':{'board':current['layout']['port'],'cdp':current['layout']['cdp_port']},
                'browser':current['layout']['browser']}
    install_id = str(uuid.uuid4())
    parent = target.parent
    stage = parent/f'.{target.name}.install-{install_id}'
    cache = parent/f'.{target.name}.manager-{install_id}'
    saved_empty = parent/f'.{target.name}.empty-{install_id}'
    lock_path = parent/f'.{target.name}.install.lock'
    intent_path = parent/f'.{target.name}.install-intent.json'
    bundle.disjoint(source, target, stage, cache, saved_empty)
    # A retained lock or intention is deliberately not recovered by B1.
    bundle.absent(lock_path)
    bundle.absent(intent_path)
    intent = {'schema':1,'kind':'customer_first_install_intent','install_id':install_id,
        'target':str(target),'stage':str(stage),'manager_cache':str(cache),'saved_empty':str(saved_empty),
        'bundle_manifest_sha256':manifest_sha256,'phase':'reserved','target_identity':None,
        'stage_identity':None,'empty_identity':None,'code':None}
    acquired = False
    try:
        with bundle.guard_path(parent), bundle.guard_path(source):
            # Exclusive create is the cross-process claim; keep its handle open.
            with bundle.exclusive_new(lock_path) as lock:
                acquired = True
                lock.write(bundle.encode({'schema':1,'kind':'customer_first_install_lock',
                    'install_id':install_id,'target':str(target)}))
                lock.flush()
                os.fsync(lock.fileno())
                bundle.write_new(intent_path, bundle.encode(intent))
                checked = bundle.verify_bundle(source, manifest_sha256, deep=False)
                manifest, files, dirs = checked['manifest'], checked['files'], checked['directories']
                bundle.require(type(port) is int and type(cdp_port) is int and 1 <= port <= 65535
                    and 1 <= cdp_port <= 65535 and port != cdp_port, 'install_settings_invalid')
                if browser is not None:
                    browser = bundle.absolute(browser)
                    bundle.no_link(browser)
                _native_path_preflight(target, cache, checked, browser)
                needed = sum(row['bytes'] for row in files.values() if row['component'] != 'bootstrap') + 2*bundle.BLOCK
                bundle.require(shutil.disk_usage(parent).free >= needed, 'install_insufficient_space')
                for output in (stage, cache, saved_empty):
                    bundle.absent(output)
                empty_identity = None
                if existing:
                    with bundle.guard_path(target) as empty_identity:
                        bundle.require(not any(target.iterdir()), 'install_target_not_empty')
                    intent['empty_identity'] = empty_identity
                    _intent_write(intent_path, intent)
                else:
                    bundle.absent(target)
                stage.mkdir()
                cache.mkdir()
                with bundle.guard_path(stage) as identity:
                    intent.update(phase='copying', stage_identity=identity, target_identity=identity)
                    _intent_write(intent_path, intent)
                    owner = {'schema':1,'kind':'customer_installation_owner','install_id':install_id,
                        'target':str(target),'target_identity':identity,'version':manifest['version'],'channel':manifest['channel'],
                        'bundle_manifest_sha256':checked['sha256'],'manager_cache':str(cache),
                        'manager_manifest_sha256':checked['sha256']}
                    product_dirs = {name.removeprefix('payload/') for name in dirs if name.startswith('payload/')}
                    product_dirs.update(name for name in dirs if name == 'entry' or name.startswith('entry/'))
                    manager_dirs = {name for name in dirs if name == 'manager' or name.startswith('manager/')}
                    _mkdir(stage, product_dirs)
                    _mkdir(cache, manager_dirs)
                    for name, row in sorted(files.items()):
                        if row['component'] == 'manager':
                            destination = cache/name
                        elif row['component'] == 'product':
                            destination = stage/name.removeprefix('payload/')
                        elif row['component'] in {'entry','guide'}:
                            destination = stage/name
                        else:
                            continue
                        if name == 'payload/installation.json':
                            # Verify the source before selecting customer-owned settings.
                            descriptor_raw = bundle.read_bytes(source/name, 65536)
                            bundle.require(len(descriptor_raw) == row['bytes'] and
                                hashlib.sha256(descriptor_raw).hexdigest() == row['sha256'], 'bundle_integrity_mismatch')
                            descriptor_template = bundle.parse(descriptor_raw)
                            continue
                        copy_verified(source/name, destination, row)
                    descriptor = descriptor_template
                    descriptor['ports'] = {'board':port,'cdp':cdp_port}
                    descriptor['browser'] = {'kind':'system-edge'} if browser is None else {'kind':'explicit','path':str(bundle.absolute(browser))}
                    bundle.descriptor_layout(target, descriptor, owner)
                    bundle.write_new(stage/'installation.json', bundle.encode(descriptor))
                    bundle.write_new(stage/'installed.manifest.json', checked['raw'])
                    bundle.write_new(cache/'bundle.manifest.json', checked['raw'])
                    bundle.write_new(cache/'cache-owner.json', bundle.encode({'schema':1,'kind':'customer_manager_cache_owner',
                        'install_id':install_id,'target':str(target),'target_identity':identity,'bundle_manifest_sha256':checked['sha256']}))
                    _seed(stage, target, descriptor, owner)
                    intent['phase'] = 'ready'
                    _intent_write(intent_path, intent)
                # Guard must be released for our own same-volume directory rename.
                if existing:
                    with bundle.guard_path(target) as observed:
                        bundle.require(observed == empty_identity and not any(target.iterdir()), 'install_target_changed')
                    os.rename(target, saved_empty)
                    with bundle.guard_path(saved_empty) as observed:
                        bundle.require(observed == empty_identity and not any(saved_empty.iterdir()), 'install_target_changed')
                else:
                    bundle.absent(target)
                with bundle.guard_path(stage) as observed:
                    bundle.require(observed == identity, 'install_stage_changed')
                os.rename(stage, target)
                with bundle.guard_path(target) as observed:
                    bundle.require(observed == identity, 'install_target_changed')
                    intent['phase'] = 'installed'
                    _intent_write(intent_path, intent)
                    # Final completion fact is published last, never in an incomplete stage.
                    bundle.publish_new(target/'owner.json', bundle.encode(owner))
                    current = bundle.inspect_install(target, deep=False)
                    bundle.require(current['owner'] == owner, 'install_commit_unconfirmed')
                return {'ok':True,'code':'installed','install_id':install_id,'target':str(target),
                    'version':manifest['version'],'state_preserved':False,'manager_cache':str(cache),
                    'retained_empty':str(saved_empty) if existing else None}
    except (OSError, ValueError, TypeError, KeyError, OverflowError) as exc:
        code = exc.code if isinstance(exc, bundle.BundleError) else 'install_unconfirmed'
        if acquired:
            # Existing durable phase is sufficient if recording the safe failure also fails.
            if intent['phase'] != 'installed':
                intent['code'] = code
                intent['phase'] = 'unconfirmed'
            try:
                with bundle.guard_path(parent):
                    _intent_write(intent_path, intent)
            except (OSError, ValueError):
                pass
        next_steps = ['Keep the intention, lock, staging and management cache for support inspection.',
            'B1 does not recover or remove an incomplete installation. Do not retry by deleting evidence.']
        if code == 'install_native_path_too_long':
            next_steps.insert(0, 'Choose a different new, short absolute target such as D:\\LiveMonitor; the native entry and derived sibling manager paths must each be below 240 UTF-16 code units.')
        return {'ok':False,'code':code,'install_id':install_id,'target':str(target),
            'retained':{'intent':str(intent_path),'lock':str(lock_path),'stage':str(stage),'manager_cache':str(cache),
                'saved_empty':str(saved_empty)},
            'next_steps':next_steps}


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise bundle.BundleError('install_arguments_invalid')


def main(argv=None):
    parser = _Parser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    command = sub.add_parser('install')
    for name in ('bundle','target','manifest-sha256'):
        command.add_argument('--'+name, required=True)
    command.add_argument('--port', type=int, default=8765)
    command.add_argument('--cdp-port', type=int, default=29222)
    command.add_argument('--browser', '--browser-path', dest='browser')
    try:
        args = parser.parse_args(argv)
        result = install(args.bundle,args.target,args.manifest_sha256,port=args.port,cdp_port=args.cdp_port,browser=args.browser)
    except (OSError, ValueError, TypeError, KeyError):
        result = {'ok':False,'code':'install_arguments_or_path_invalid'}
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0 if result['ok'] else 78


if __name__ == '__main__':
    raise SystemExit(main())
