"""Synthetic first-install files only; no application, native EXE or model runs."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import customer_bundle as cb
from scripts import customer_install as ci
from scripts import build_customer_bundle as builder

OWNED = Path(os.environ.get("DLI_TEST_ROOT", str(Path(__file__).resolve().parents[1] / ".test-state")))


@pytest.fixture(autouse=True)
def owned_new_root(tmp_path):
    # Even an accidentally broad pytest invocation may not exercise install on
    # a generic temp root. The runner must explicitly supply this task basetemp.
    assert tmp_path.resolve().is_relative_to(OWNED.resolve())
    assert not any(tmp_path.iterdir())
    (tmp_path/'fixture-owner.json').write_text(json.dumps({'kind':'11B1_synthetic_test','root':str(tmp_path)}), encoding='utf-8')


def write(path, raw):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)


def row(path, name):
    raw = path.read_bytes()
    return {'path':name,'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}


def jsonwrite(path, value):
    write(path, cb.encode(value))


@pytest.fixture
def toy(tmp_path):
    payload = tmp_path/'fresh-payload'
    descriptor = {'schema':1,'kind':'customer_installation','app':'app','state':'state',
        'python':'runtime/python/python.exe','node':'runtime/node/node.exe','ffmpeg':'runtime/ffmpeg/ffmpeg.exe',
        'model':'assets/models/faster-whisper-small','cache':'state/cache','browser':{'kind':'system-edge'},
        'dll_directories':['runtime/python'],'ports':{'board':8765,'cdp':29222}}
    config = {'rooms':['public-example'],'cookie':'','llm':{'enabled':True,'providers':{'toy':{'api_key':''}}},
        'feishu':{'enabled':True},'anchor':{'enabled':True}}
    jsonwrite(payload/'app/config.example.json', config)
    jsonwrite(payload/'app/baseline/installation-layout.example.json', descriptor)
    jsonwrite(payload/'installation.json', descriptor)
    for name in ('runtime/python/python.exe','runtime/python/pythonw.exe','runtime/node/node.exe',
            'runtime/ffmpeg/ffmpeg.exe','assets/models/faster-whisper-small/config.json',
            'app/web/dist/index.html','notices/LICENSE.txt'):
        write(payload/name, ('synthetic:'+name).encode())
    paths = sorted(path for path in payload.rglob('*') if path.is_file())
    dirs = sorted(path.relative_to(payload).as_posix() for path in payload.rglob('*') if path.is_dir())
    jsonwrite(payload/'payload-manifest.json', {'schema':1,'kind':'customer_payload_sample','sample_state':False,
        'files':[row(path,path.relative_to(payload).as_posix()) for path in paths],'directories':dirs})
    embed = tmp_path/'python-3.14.10-embeddable-amd64.zip'
    with zipfile.ZipFile(embed, 'x') as archive:
        for name, raw in {'python.exe':b'synthetic-exe','pythonw.exe':b'synthetic-exe-w',
                'python314.zip':b'synthetic-stdlib','python314.dll':b'synthetic-dll',
                'python314._pth':b'python314.zip\n.\n#import site\n','LICENSE.txt':b'synthetic-notice'}.items():
            archive.writestr(name,raw)
    lock = tmp_path/'runtime-lock.json'
    jsonwrite(lock, {'schema':1,'python':{'version':'3.14.10','embeddable':row(embed,embed.name) | {'filename':embed.name}}})
    write(tmp_path/'console.exe', b'synthetic-console-not-executed')
    write(tmp_path/'window.exe', b'synthetic-window-not-executed')
    write(tmp_path/'guide.md', b'Explicit trusted SHA; synthetic test only.\n')
    output = tmp_path/'bundle'
    result = builder.build(payload,embed,lock,tmp_path/'console.exe',tmp_path/'window.exe',output,
        zip_output=tmp_path/'bundle.zip',guide=tmp_path/'guide.md')
    return {'root':tmp_path,'payload':payload,'bundle':output,'sha':result['bundle_manifest_sha256'],
        'zip':tmp_path/'bundle.zip','target':tmp_path/'installed 客户','build':result}


def install(toy, **choices):
    return ci.install(toy['bundle'],toy['target'],toy['sha'],**choices)


def test_clean_bundle_zip_and_manager_no_site(toy):
    checked = cb.verify_bundle(toy['bundle'],toy['sha'],deep=True)
    assert checked['manifest']['product_metadata'] == ['payload/payload-manifest.json']
    assert not any('/state/' in name for name in checked['files'])
    assert (toy['bundle']/'manager/python/python314._pth').read_bytes() == b'python314.zip\n.\n../code\n'
    with zipfile.ZipFile(toy['zip']) as archive:
        assert set(archive.namelist()) == set(checked['files']) | {'bundle.manifest.json'} | {x+'/' for x in checked['directories']}
        for name in checked['files']:
            assert archive.read(name) == (toy['bundle']/name).read_bytes()


def test_first_install_seed_owner_external_manager_and_gate(toy):
    result = install(toy,port=41001,cdp_port=41002)
    assert result['ok'], result
    inspected = cb.inspect_install(toy['target'],deep=True)
    owner, layout = inspected['owner'], inspected['layout']
    assert owner['target_identity']['volume_serial'] and len(owner['target_identity']['file_id']) == 32
    assert layout['manager_cache'].parent == toy['target'].parent
    assert not layout['manager_cache'].is_relative_to(toy['target'])
    assert (layout['manager_cache']/'manager/code/customer_install.py').read_bytes() == (ROOT/'scripts/customer_install.py').read_bytes()
    config = cb.read_json(layout['config'])
    assert config['rooms'] == [] and not config['llm']['enabled'] and not config['feishu']['enabled'] and not config['anchor']['enabled']
    assert cb.read_json(layout['roster'])['anchors'] == []
    context = cb.read_json(layout['context_file'])
    assert set(context) == {'schema','root','data','config','roster','dist','python','profile','port','cdp_port','instance_id'}
    assert context['port'] == 41001 and context['cdp_port'] == 41002
    assert context['root'] == str(toy['target']/'app')
    assert cb.read_json(layout['gate']/'maintenance.json')['active'] is False
    assert cb.read_json(layout['gate']/'spawn-ledger.json')['active'] == []
    assert cb.read_json(layout['gate']/'spawn-ledger.json')['recent'] == []
    assert not (toy['target']/'CustomerSetup.exe').exists()


def test_same_version_is_readonly_and_preserves_customer_choices(toy,monkeypatch):
    assert install(toy,port=41101,cdp_port=41102)['ok']
    target = toy['target']
    write(target/'state/config.json',b'{"synthetic_customer_key":"preserve"}\n')
    write(target/'state/anchors.json',b'{"synthetic_roster":["preserve"]}\n')
    write(target/'state/data/.local_app/browser-profile/user.bin',b'new-owned-profile')
    before = {p.relative_to(target).as_posix():p.read_bytes() for p in target.rglob('*') if p.is_file()}
    monkeypatch.setattr(cb,'write_new',lambda *a,**k:pytest.fail('idempotent write'))
    monkeypatch.setattr(cb,'exclusive_new',lambda *a,**k:pytest.fail('idempotent lock claim'))
    result = install(toy,port=42201,cdp_port=42202)
    assert result['ok'] and result['code'] == 'already_installed' and result['existing_settings_preserved']
    assert result['ports'] == {'board':41101,'cdp':41102}
    assert before == {p.relative_to(target).as_posix():p.read_bytes() for p in target.rglob('*') if p.is_file()}


def test_existing_empty_target_retained_and_identity_bound(toy):
    toy['target'].mkdir()
    with cb.guard_path(toy['target']) as previous:
        original = dict(previous)
    result = install(toy)
    assert result['ok'], result
    retained = Path(result['retained_empty'])
    assert retained.is_dir() and not any(retained.iterdir())
    with cb.guard_path(retained) as observed:
        assert observed == original
    intent = cb.read_json(toy['target'].parent/f'.{toy["target"].name}.install-intent.json')
    assert intent['empty_identity'] == original
    assert intent['target_identity'] != original


def test_unknown_nonempty_target_not_touched(toy):
    write(toy['target']/'unknown.keep',b'never change or delete')
    with pytest.raises(cb.BundleError):
        install(toy)
    assert (toy['target']/'unknown.keep').read_bytes() == b'never change or delete'
    assert not (toy['root']/f'.{toy["target"].name}.install.lock').exists()


def test_wrong_anchor_and_unlisted_input_fail_before_install_copy(toy):
    bad = ci.install(toy['bundle'],toy['target'],'0'*64)
    assert not bad['ok'] and bad['code'] == 'bundle_manifest_digest_mismatch'
    assert not toy['target'].exists()
    assert Path(bad['retained']['intent']).is_file() and Path(bad['retained']['lock']).is_file()
    assert not Path(bad['retained']['stage']).exists()
    write(toy['bundle']/'unknown.keep',b'unlisted')
    with pytest.raises(cb.BundleError,match='bundle_unlisted_path'):
        cb.verify_bundle(toy['bundle'],toy['sha'])


def test_source_hash_failure_retains_partial_owned_stage(toy):
    source = toy['bundle']/'payload/app/web/dist/index.html'
    source.write_bytes(b'X'*source.stat().st_size)
    result = install(toy)
    assert not result['ok'] and result['code'] == 'bundle_integrity_mismatch'
    assert not toy['target'].exists()
    assert Path(result['retained']['stage']).is_dir()
    assert cb.read_json(Path(result['retained']['intent']))['phase'] == 'unconfirmed'
    with pytest.raises(cb.BundleError):
        install(toy)
    assert not toy['target'].exists()


def test_insufficient_space_is_preflight_not_partial_success(toy,monkeypatch):
    class Disk:
        free = 0
    monkeypatch.setattr(ci.shutil,'disk_usage',lambda _:Disk())
    result = install(toy)
    assert not result['ok'] and result['code'] == 'install_insufficient_space'
    assert not toy['target'].exists() and not Path(result['retained']['stage']).exists()
    assert cb.read_json(Path(result['retained']['intent']))['code'] == 'install_insufficient_space'


def test_interrupted_copy_is_not_recovered_or_claimed_complete(toy,monkeypatch):
    original = ci.copy_verified
    calls = []
    def interrupted(*args):
        original(*args)
        calls.append(args[1])
        raise SystemExit('synthetic interruption')
    monkeypatch.setattr(ci,'copy_verified',interrupted)
    with pytest.raises(SystemExit):
        install(toy)
    assert len(calls) == 1 and not toy['target'].exists()
    intent = cb.read_json(toy['root']/f'.{toy["target"].name}.install-intent.json')
    assert intent['phase'] == 'copying' and Path(intent['stage']).exists()
    with pytest.raises(cb.BundleError):
        install(toy)


def test_bad_completion_intent_or_owner_is_not_idempotent(toy):
    assert install(toy)['ok']
    intent_path = toy['root']/f'.{toy["target"].name}.install-intent.json'
    intent = cb.read_json(intent_path)
    intent['phase'] = 'ready'
    intent_path.write_bytes(cb.encode(intent))
    with pytest.raises(cb.BundleError,match='installation_completion_unconfirmed'):
        cb.inspect_install(toy['target'])
    with pytest.raises(cb.BundleError):
        install(toy)
    assert not (toy['target']/'state/config.json').read_bytes() == b''


@pytest.mark.parametrize('name',['../escape','payload/a:ads','payload/CON.txt','payload/CONIN$','payload/COM¹.log',
    'payload/LPT².any','payload/CON .txt','payload/a.','payload/a ','payload//a','\\\\server\\share','/absolute'])
def test_forbidden_manifest_paths(name):
    with pytest.raises(cb.BundleError):
        cb.relative(name)


def test_manifest_case_alias_and_real_hardlink_refused(toy):
    manifest = cb.read_json(toy['bundle']/'bundle.manifest.json')
    first = dict(manifest['files'][0])
    first['path'] = first['path'].swapcase()
    manifest['files'].append(first)
    with pytest.raises(cb.BundleError):
        cb.inventory(manifest)
    source = toy['bundle']/'payload/app/web/dist/index.html'
    alias = toy['root']/'retained-hardlink'
    os.link(source,alias)
    with pytest.raises(cb.BundleError,match='bundle_file_kind_invalid'):
        cb.verify_bundle(toy['bundle'],toy['sha'])
    assert alias.read_bytes() == source.read_bytes()


def test_product_build_refuses_stateful_payload_before_output(toy):
    meta = cb.read_json(toy['payload']/'payload-manifest.json')
    meta['sample_state'] = True
    (toy['payload']/'payload-manifest.json').write_bytes(cb.encode(meta))
    with pytest.raises(cb.BundleError,match='bundle_stateful_payload_refused'):
        builder._payload_inventory(toy['payload'])


def test_parent_pinned_during_directory_creation(tmp_path,monkeypatch):
    parent = tmp_path/'new-parent'
    parent.mkdir()
    original = Path.mkdir
    observed = []
    def create(path,*args,**kwargs):
        if path == parent/'child':
            with pytest.raises(PermissionError):
                os.rename(parent,tmp_path/'retained-parent')
            observed.append('rename-denied')
        return original(path,*args,**kwargs)
    monkeypatch.setattr(Path,'mkdir',create)
    cb.make_directories(parent,{'child'})
    assert observed == ['rename-denied'] and (parent/'child').is_dir()
    assert not (tmp_path/'retained-parent').exists()


def test_completion_published_last_and_missing_gate_refused(toy,monkeypatch):
    def stop_before_publish(path,raw):
        assert path.name == 'owner.json'
        assert not path.exists()
        assert cb.read_json(path.parent.parent/f'.{path.parent.name}.install-intent.json')['phase'] == 'installed'
        raise OSError('synthetic publication failure')
    original = cb.publish_new
    monkeypatch.setattr(cb,'publish_new',stop_before_publish)
    result = install(toy)
    assert not result['ok'] and not (toy['target']/'owner.json').exists()
    assert toy['target'].is_dir() and (toy['target']/'state/config.json').is_file()
    with pytest.raises(cb.BundleError,match='installation_not_ready'):
        cb.inspect_install(toy['target'])
    # This isolated fixture may finish the same explicitly known owner bytes;
    # production B1 deliberately exposes no automatic recovery operation.
    intent = cb.read_json(Path(result['retained']['intent']))
    manifest = cb.read_json(toy['target']/'installed.manifest.json')
    owner = {'schema':1,'kind':'customer_installation_owner','install_id':intent['install_id'],
        'target':str(toy['target']),'target_identity':intent['target_identity'],'version':manifest['version'],
        'channel':manifest['channel'],'bundle_manifest_sha256':toy['sha'],'manager_cache':intent['manager_cache'],
        'manager_manifest_sha256':toy['sha']}
    original(toy['target']/'owner.json',cb.encode(owner))
    assert cb.inspect_install(toy['target'])['owner'] == owner
    gate = toy['target']/'.gate/code.lease'
    gate.rename(gate.with_name('code.lease.retained'))
    with pytest.raises(cb.BundleError,match='installation_not_ready'):
        cb.inspect_install(toy['target'])


def test_new_fact_publish_never_replaces_existing_bytes(tmp_path):
    path = tmp_path/'existing-owner.json'
    path.write_bytes(b'original-complete-fact\n')
    with pytest.raises(cb.BundleError,match='install_output_exists'):
        cb.publish_new(path,b'new-complete-fact\n')
    assert path.read_bytes() == b'original-complete-fact\n'
    leftovers = list(tmp_path.glob('existing-owner.json.*.tmp'))
    assert len(leftovers) == 1 and leftovers[0].read_bytes() == b'new-complete-fact\n'


@pytest.mark.parametrize('fact',['descriptor','owner','intent','cache_owner'])
def test_boolean_schema_cannot_authorize_installation(toy,fact,monkeypatch):
    assert install(toy)['ok']
    target = toy['target']
    inspected = cb.inspect_install(target)
    paths = {'descriptor':target/'installation.json', 'owner':target/'owner.json',
        'intent':target.parent/f'.{target.name}.install-intent.json',
        'cache_owner':inspected['layout']['manager_cache']/'cache-owner.json'}
    path = paths[fact]
    value = cb.read_json(path)
    value['schema'] = True
    path.write_bytes(cb.encode(value))
    before = {p.relative_to(target/'state').as_posix():p.read_bytes() for p in (target/'state').rglob('*') if p.is_file()}
    with pytest.raises(cb.BundleError):
        cb.inspect_install(target)
    from scripts import local_doctor
    monkeypatch.setattr(local_doctor,'_state_probe',lambda *a,**k:pytest.fail('bad schema authorized state probe'))
    outcome = local_doctor.doctor(target,inspect_install=cb.inspect_install)
    assert not outcome['ok']
    assert before == {p.relative_to(target/'state').as_posix():p.read_bytes() for p in (target/'state').rglob('*') if p.is_file()}
    if fact == 'descriptor':
        # Exercise the actual product validator, not a duplicate schema helper.
        from scripts.local_installation import load_installation, InstallationError
        with pytest.raises(InstallationError):
            load_installation(target/'app')


def test_boolean_manifest_schema_and_state_compatibility_refused(toy):
    manifest = cb.read_json(toy['bundle']/'bundle.manifest.json')
    for key in ('schema','state_compatibility'):
        invalid = dict(manifest)
        invalid[key] = True
        with pytest.raises(cb.BundleError,match='bundle_manifest_invalid'):
            cb.inventory(invalid)
    meta = cb.read_json(toy['payload']/'payload-manifest.json')
    meta['schema'] = True
    (toy['payload']/'payload-manifest.json').write_bytes(cb.encode(meta))
    with pytest.raises(cb.BundleError,match='bundle_stateful_payload_refused'):
        builder._payload_inventory(toy['payload'])


@pytest.mark.parametrize('name',['app/.git/HEAD','app/config.json'])
def test_listed_program_state_or_git_is_refused(toy,name):
    payload = toy['payload']
    write(payload/name,b'synthetic forbidden input; preserve evidence\n')
    metadata = cb.read_json(payload/'payload-manifest.json')
    metadata['files'].append(row(payload/name,name))
    metadata['directories'] = sorted(path.relative_to(payload).as_posix() for path in payload.rglob('*') if path.is_dir())
    (payload/'payload-manifest.json').write_bytes(cb.encode(metadata))
    with pytest.raises(cb.BundleError,match='bundle_program_path_forbidden'):
        builder._payload_inventory(payload)
    manifest = cb.read_json(toy['bundle']/'bundle.manifest.json')
    manifest['files'].append(row(payload/name,'payload/'+name) | {'component':'product'})
    manifest['directories'] = sorted(set(manifest['directories']) | {'payload/'+x for x in metadata['directories']})
    with pytest.raises(cb.BundleError,match='bundle_program_path_forbidden'):
        cb.inventory(manifest)
    assert (payload/name).read_bytes() == b'synthetic forbidden input; preserve evidence\n'


def test_cache_path_limit(toy,monkeypatch):
    parent = toy['root']
    units = lambda path:len(str(path).encode('utf-16-le'))//2
    # The target itself is accepted by the native parser, while its sibling
    # cache plus manager entry no longer fits. Emoji exercise UTF-16 counting.
    count = max(1,(180-units(parent)-1)//2)
    target = parent/('😀'*count)
    assert units(target) < 240
    derived = target.parent/f'.{target.name}.manager-00000000-0000-0000-0000-000000000000'
    assert units(derived/'manager/code/customer_install.py') >= 240
    calls = []
    monkeypatch.setattr(ci,'copy_verified',lambda *a,**k:calls.append(a))
    result = ci.install(toy['bundle'],target,toy['sha'])
    assert not result['ok'] and result['code'] == 'install_native_path_too_long'
    assert calls == [] and not target.exists()
    assert not Path(result['retained']['stage']).exists()
    assert not Path(result['retained']['manager_cache']).exists()
    assert Path(result['retained']['lock']).is_file()
    assert cb.read_json(Path(result['retained']['intent']))['code'] == 'install_native_path_too_long'
    assert 'short absolute target' in result['next_steps'][0]


# Revision 1 uses its own tiny bundle: no sibling doctor's changing source,
# embedded interpreter, real runtime, native launcher or model is executed.
@pytest.fixture
def r1_bundle(tmp_path):
    owned = OWNED/'revision-1/install'
    assert tmp_path.resolve().is_relative_to(owned.resolve())
    root = tmp_path/'long-source-bundle-for-real-short-name-boundary'
    descriptor = {'schema':1,'kind':'customer_installation','app':'app','state':'state',
        'python':'runtime/python/python.exe','node':'runtime/node/node.exe','ffmpeg':'runtime/ffmpeg/ffmpeg.exe',
        'model':'assets/models/faster-whisper-small','cache':'state/cache','browser':{'kind':'system-edge'},
        'dll_directories':[], 'ports':{'board':8765,'cdp':29222}}
    config = {'rooms':[],'cookie':'','llm':{'enabled':False,'providers':{}},
        'feishu':{'enabled':False},'anchor':{'enabled':False}}
    stub = b'MZ-generated-fixture-not-executed'
    contents = {
        'payload/installation.json':cb.encode(descriptor),
        'payload/payload-manifest.json':cb.encode({'synthetic':True,'no_real_runtime':True}),
        'payload/app/config.example.json':cb.encode(config),
        'payload/app/baseline/installation-layout.example.json':cb.encode(descriptor),
        'manager/python/python.exe':stub,
        'manager/python/python314._pth':b'python314.zip\n.\n../code\n',
        'manager/code/customer_bundle.py':b'# generated fixture; not imported\n',
        'manager/code/customer_install.py':b'# generated fixture; not imported\n',
        'manager/code/local_doctor.py':b'# generated fixture; not imported\n',
        'entry/CustomerLauncher.exe':stub,'entry/CustomerLauncherW.exe':stub,
        'CustomerSetup.exe':stub,'AGENT_INSTALL.md':b'Generated protocol fixture only.\n'}
    rows, dirs = [], set()
    for name, raw in contents.items():
        write(root/name, raw)
        parts = name.split('/')
        dirs.update('/'.join(parts[:index]) for index in range(1,len(parts)))
        component = ('product' if name.startswith('payload/') else 'manager' if name.startswith('manager/')
            else 'entry' if name.startswith('entry/') else 'bootstrap' if name == 'CustomerSetup.exe' else 'guide')
        rows.append(row(root/name,name) | {'component':component})
    raw = cb.encode({'schema':1,'kind':'customer_bundle','version':'0.1.0-generated-r1','channel':'local-review',
        'state_compatibility':1,'files':rows,'directories':sorted(dirs),'ready_to_export':False})
    write(root/'bundle.manifest.json',raw)
    digest = hashlib.sha256(raw).hexdigest()
    cb.verify_bundle(root,digest,deep=True)
    return root,digest


def _r1_tree(root):
    # This function visits only this test's newly generated tiny fixture.
    result = {}
    for path in [root,*sorted(root.rglob('*'))]:
        info = path.lstat()
        name = '.' if path == root else path.relative_to(root).as_posix()
        item = {'dev':info.st_dev,'ino':info.st_ino,'nlink':info.st_nlink,'mtime_ns':info.st_mtime_ns}
        if path.is_dir():
            item['kind'] = 'directory'
        else:
            raw = path.read_bytes()
            item.update(kind='file',bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest())
        result[name] = item
    return result


def _r1_cli(root,digest,target,**options):
    import subprocess
    environment = dict(os.environ)
    for key in ('PYTHONHOME','PYTHONPATH'):
        environment.pop(key,None)
    environment.update(PYTHONDONTWRITEBYTECODE='1',PYTHONUTF8='1')
    argv = [sys.executable,'-B','-X','utf8',str(ROOT/'scripts/customer_install.py'),'install',
        '--bundle',str(root),'--target',str(target),'--manifest-sha256',digest]
    for key,value in options.items():
        argv.extend(['--'+key.replace('_','-'),str(value)])
    process = subprocess.run(argv,cwd=root.parent,env=environment,capture_output=True,timeout=120)
    assert not process.stderr,process.stderr.decode('utf-8',errors='replace')
    return process,json.loads(process.stdout)


def _r1_short(path):
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.GetShortPathNameW.argtypes = [wintypes.LPCWSTR,wintypes.LPWSTR,wintypes.DWORD]
    kernel.GetShortPathNameW.restype = wintypes.DWORD
    buffer = ctypes.create_unicode_buffer(32768)
    length = kernel.GetShortPathNameW(str(path),buffer,len(buffer))
    assert 0 < length < len(buffer)
    alias = Path(buffer.value)
    assert path.samefile(alias) and not cb.same(path,alias)
    assert path.name.lower() != alias.name.lower(), 'This case requires an actual leaf 8.3 alias.'
    return alias


def _r1_save(tmp_path,name,value,processes=()):
    for index,process in enumerate(processes):
        write(tmp_path/f'{name}-{index}.stdout.txt',process.stdout)
        write(tmp_path/f'{name}-{index}.stderr.txt',process.stderr)
    jsonwrite(tmp_path/f'{name}.json',value)


def test_r1_cli(r1_bundle,tmp_path):
    source,digest = r1_bundle
    before = _r1_tree(source)
    target = tmp_path/'app'
    first,result = _r1_cli(source,digest,target,port=42001,cdp_port=42002)
    assert first.returncode == 0 and result['ok'] and result['code'] == 'installed',result
    # Actual CLI and original copy_verified/write_new call real fsync. No
    # monkeypatch or fixture omission is involved in either subprocess.
    inspected = cb.inspect_install(target,deep=True)
    assert inspected['owner']['bundle_manifest_sha256'] == digest
    assert inspected['layout']['port'] == 42001 and inspected['layout']['cdp_port'] == 42002
    write(target/'state/config.json',b'{"synthetic_customer_marker":"preserve"}\n')
    write(target/'state/anchors.json',b'{"synthetic_customer_roster":"preserve"}\n')
    write(target/'state/data/.local_app/browser-profile/owned.bin',b'generated profile marker')
    installed_before = _r1_tree(target)
    second,reused = _r1_cli(source,digest,target,port=43001,cdp_port=43002)
    assert second.returncode == 0 and reused['ok'] and reused['code'] == 'already_installed',reused
    assert reused['existing_settings_preserved'] and reused['ports'] == {'board':42001,'cdp':42002}
    assert _r1_tree(target) == installed_before
    assert _r1_tree(source) == before
    _r1_save(tmp_path,'cli',{'first':result,'second':reused,'source_before':before,'source_after':_r1_tree(source),
        'installed_before':installed_before,'installed_after':_r1_tree(target),
        'scope':'actual CLI, real copy/fsync, synthetic tiny bundle; no native launch'},(first,second))


@pytest.mark.parametrize('case',['same','child','missing_tail','ancestor'])
def test_r1_alias(r1_bundle,tmp_path,case):
    source,digest = r1_bundle
    alias = _r1_short(source)
    target = {'same':alias,'child':alias/'new-target','missing_tail':alias/'missing'/'tail'/'new-target',
        'ancestor':alias.parent}[case]
    before_source,before_all = _r1_tree(source),_r1_tree(tmp_path)
    process,result = _r1_cli(source,digest,target)
    assert process.returncode == 78 and not result['ok'],result
    assert _r1_tree(source) == before_source
    assert _r1_tree(tmp_path) == before_all, 'The CLI must reject before lock/intent/stage writes.'
    with pytest.raises(cb.BundleError,match='^install_path_overlap$'):
        cb.disjoint(source,target)
    _r1_save(tmp_path,'alias',{'case':case,'source':str(source),'alias':str(alias),'target':str(target),
        'actual_short_name_samefile':True,'result':result,'source_before':before_source,
        'source_after':_r1_tree(source),'all_fixture_entries_unchanged':True},(process,))


def test_r1_root(r1_bundle,tmp_path,monkeypatch):
    import uuid
    source,digest = r1_bundle
    volume = Path(tmp_path.anchor)
    with cb.guard_path(volume) as identity:
        assert len(identity['volume_serial']) == 16 and len(identity['file_id']) == 32
    with pytest.raises(cb.BundleError,match='^install_path_invalid$'):
        cb.absolute(volume)
    class BeforeWrite(BaseException):
        pass
    calls = []
    def block_claim(path):
        calls.append(str(path))
        raise BeforeWrite()
    target = volume/('r1-never-created-'+uuid.uuid4().hex)
    monkeypatch.setattr(cb,'exclusive_new',block_claim)
    with pytest.raises(BeforeWrite):
        ci.install(source,target,digest)
    assert calls == [str(volume/f'.{target.name}.install.lock')]
    assert not target.exists() and not Path(calls[0]).exists()
    assert not (volume/f'.{target.name}.install-intent.json').exists()
    _r1_save(tmp_path,'root',{'volume_guard_identity':identity,'public_volume_target_rejected':True,
        'root_child_reached_claim_but_intercepted_before_any_write':True,'intercepted':calls})


def test_r1_ziproot(r1_bundle,tmp_path,monkeypatch):
    import uuid
    source,_ = r1_bundle
    target = Path(tmp_path.anchor)/('r1-never-created-'+uuid.uuid4().hex+'.zip')
    class BeforeWrite(BaseException):
        pass
    calls = []
    def block_zip(path,*args,**kwargs):
        calls.append(str(path))
        raise BeforeWrite()
    monkeypatch.setattr(builder.zipfile,'ZipFile',block_zip)
    with pytest.raises(BeforeWrite):
        builder.create_zip(source,target,{},set())
    assert calls == [str(target)] and not target.exists()
    _r1_save(tmp_path,'ziproot',{'volume_parent_accepted':True,'before_create_intercepted':calls,'no_volume_output':True})


def test_r1_files(tmp_path):
    path = tmp_path/'existing-regular-file-with-native-short-name.zip'
    write(path,b'owned ordinary file; not a real ZIP')
    alias = _r1_short(path)
    before = _r1_tree(tmp_path)
    with pytest.raises(cb.BundleError,match='^install_path_overlap$'):
        cb.disjoint(path,alias)
    with pytest.raises(cb.BundleError,match='^bundle_file_kind_invalid$'):
        cb.disjoint(path,path/'new-child')
    cb.disjoint(path,tmp_path/'new-sibling.zip')
    cb.disjoint(tmp_path/'missing'/'left',tmp_path/'missing'/'right')
    cb.disjoint(tmp_path/'missing-left'/'child',tmp_path/'missing-right'/'child')
    assert _r1_tree(tmp_path) == before
    _r1_save(tmp_path,'files',{'regular_alias_rejected':True,'regular_ancestor_rejected':True,
        'regular_and_missing_sibling_accepted':True,'two_missing_siblings_accepted':True,'before':before,
        'after':_r1_tree(tmp_path)})


def test_r1_junction(r1_bundle,tmp_path):
    import subprocess
    source,digest = r1_bundle
    link = tmp_path/'owned-junction-retained'
    process = subprocess.run(['cmd.exe','/d','/c','mklink','/J',str(link),str(source)],
        capture_output=True,timeout=30)
    write(tmp_path/'junction-create.stdout.txt',process.stdout)
    write(tmp_path/'junction-create.stderr.txt',process.stderr)
    assert process.returncode == 0 and link.lstat().st_file_attributes & 0x400
    before_source = _r1_tree(source)
    # Never recurse through this retained junction when taking an inventory.
    before_names = sorted(path.name for path in tmp_path.iterdir())
    target = link/'new-target'
    child,result = _r1_cli(source,digest,target)
    assert child.returncode == 78 and not result['ok'],result
    with pytest.raises(cb.BundleError,match='^bundle_linked_path$'):
        cb.disjoint(source,target)
    assert _r1_tree(source) == before_source
    assert sorted(path.name for path in tmp_path.iterdir()) == before_names
    _r1_save(tmp_path,'junction',{'actual_reparse_directory':True,'result':result,'source_before':before_source,
        'source_after':_r1_tree(source),'parent_entries_unchanged':True,'junction_retained':str(link)},(child,))
