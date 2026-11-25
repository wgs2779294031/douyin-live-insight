"""Offline CPython embeddable + complete hash-locked wheel preparation.

Build tool only. Does not download, install globally, run postinstall hooks,
modify source/state, or implement install/upgrade transactions.
"""
from __future__ import annotations
import argparse
import configparser
import email.parser
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import time
import zipfile

PTH = ['python314.zip', '.', 'Lib/site-packages', '../../app', '../../app/scripts', 'import site']
DLL_DIRS = ['runtime/python', 'runtime/python/Lib/site-packages/pywin32_system32',
 'runtime/python/Lib/site-packages/numpy.libs', 'runtime/python/Lib/site-packages/curl_cffi.libs',
 'runtime/python/Lib/site-packages/ctranslate2', 'runtime/python/Lib/site-packages/av.libs',
 'runtime/python/Lib/site-packages/onnxruntime/capi', 'runtime/python/Lib/site-packages/py_mini_racer']
def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1048576), b''): h.update(chunk)
    return h.hexdigest()
def require(condition, reason):
    if not condition: raise ValueError(reason)
def entry_exists(path):
    """Unlike exists(), lstat also observes a dangling link leaf."""
    try:
        os.lstat(path)
    except FileNotFoundError:
        return False
    return True
def write_exclusive(path, content):
    """Never follow or replace a leaf created after the preflight check."""
    with Path(path).open('xb') as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
def regular(path):
    path = Path(path)
    for item in [path, *path.parents]:
        require(not item.is_symlink() and not item.is_junction(), 'linked_input_or_output')
    s = path.stat()
    require(stat.S_ISREG(s.st_mode) and s.st_nlink == 1, 'input_not_single_regular_file')
    return path.resolve(strict=True)
def read_json(path, limit=4*1024*1024):
    path = regular(path); require(path.stat().st_size <= limit, 'input_json_capacity')
    return json.loads(path.read_bytes())
def safe_name(value):
    require(isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', value), 'unsafe_filename')
    return value
def check_file(path, row):
    path = regular(path)
    require(path.name == row['filename'], 'filename_mismatch')
    require(path.stat().st_size == row['bytes'] and digest(path) == row['sha256'], 'input_integrity_mismatch')
    return path
def encoded(value): return json.dumps(value, ensure_ascii=False, indent=2).encode('utf-8') + b'\n'
def wheel_info(file):
    with zipfile.ZipFile(file) as z:
        names = [n for n in z.namelist() if n.endswith('.dist-info/METADATA')]
        require(len(names) == 1, 'wheel_metadata_unconfirmed')
        return email.parser.BytesParser().parsebytes(z.read(names[0]))
def run(args):
    runtime = read_json(args.runtime_lock)
    require(runtime['schema'] == 1 and runtime['python']['version'] == '3.14.10'
      and runtime['python']['abi'] == 'cp314' and runtime['python']['platform'] == 'win_amd64'
      and runtime['python']['free_threaded'] is False, 'unsupported_runtime')
    require(runtime['pth_lines'] == PTH and runtime['dll_directories'] == DLL_DIRS, 'path_policy_mismatch')
    wheel_lock_path = regular(args.wheel_lock); requirements = regular(args.requirements)
    shim = regular(args.sitecustomize); builder = regular(args.builder_python)
    require(digest(wheel_lock_path) == runtime['wheel_lock_sha256'], 'wheel_lock_mismatch')
    require(digest(requirements) == runtime['requirements_sha256'], 'requirements_mismatch')
    require(digest(shim) == runtime['sitecustomize_sha256'], 'sitecustomize_mismatch')
    require(digest(builder) == runtime['builder_python_exe_sha256'], 'builder_python_mismatch')
    wheels = read_json(wheel_lock_path)['wheels']
    require(len(wheels) == 63 and len({w['filename'] for w in wheels}) == len(wheels), 'wheel_set_unconfirmed')
    expected = ''.join(w['name']+'=='+w['version']+' --hash=sha256:'+w['sha256']+'\n' for w in wheels)
    require(requirements.read_text(encoding='utf-8') == expected, 'requirements_not_exact_wheel_closure')
    require(not any(re.sub('[-_.]+','-', w['name']).lower() in ('pip','pytest','pyexecjs') for w in wheels), 'dev_or_unsupported_distribution')
    archive_row = runtime['python']['embeddable']; safe_name(archive_row['filename'])
    archive = check_file(Path(args.cache)/archive_row['filename'], archive_row)
    for wheel in wheels:
        safe_name(wheel['filename']); path = check_file(Path(args.wheelhouse)/wheel['filename'], wheel)
        info = wheel_info(path)
        require(re.sub('[-_.]+','-',info['Name']).lower() == re.sub('[-_.]+','-',wheel['name']).lower()
          and info['Version'] == wheel['version'], 'wheel_metadata_mismatch')
    env = os.environ.copy()
    for key in list(env):
        if key.upper().startswith('PYTHON') or key.upper().startswith('PIP_') or key.upper() in ('NODE_OPTIONS','NODE_PATH'):
            env.pop(key)
    # --isolated still reads GLOBAL/SITE pip configuration. The explicit null
    # config is pip's documented all-configuration opt-out, including find-links.
    env['PIP_CONFIG_FILE'] = os.devnull
    env['PATH'] = os.environ['SystemRoot']+'\\System32;'+os.environ['SystemRoot']
    probe = subprocess.run([str(builder),'-I','-B','-c',
      'import json,struct,sys,sysconfig;print(json.dumps([list(sys.version_info[:3]),struct.calcsize("P"),sysconfig.get_config_var("Py_GIL_DISABLED")]))'],
      env=env,capture_output=True,text=True,timeout=30,check=True)
    version, pointer, free = json.loads(probe.stdout)
    require(version == [3,14,10] and pointer == 8 and not free, 'builder_abi_mismatch')
    raw_output = Path(args.output).absolute()
    require(not entry_exists(raw_output), 'output_must_be_new')
    parent = raw_output.parent.resolve(strict=True)
    for item in [raw_output.parent, *raw_output.parent.parents]:
        require(not item.is_symlink() and not item.is_junction(), 'linked_output_parent')
    output = parent/raw_output.name
    for suffix in ('.prepare.json','.pip-report.json','.pip.stdout','.pip.stderr','.build-temp'):
        require(not entry_exists(output.with_name(output.name+suffix)), 'evidence_already_exists')
    start = time.monotonic(); output.mkdir()
    status = {'schema':1, 'completed':False, 'output_layout':'private Python directory; final installation uses runtime/python',
      'inputs': {'runtime_lock_sha256':digest(args.runtime_lock), 'wheel_lock_sha256':digest(wheel_lock_path),
                 'requirements_sha256':digest(requirements), 'sitecustomize_sha256':digest(shim),
                 'builder_python_exe_sha256':digest(builder)}, 'python':runtime['python'],
      'dll_directories':DLL_DIRS, 'network':'offline; no-index, hash-required, wheel-only', 'ready_to_export':False}
    try:
        build_temp = output.with_name(output.name+'.build-temp')
        build_temp.mkdir()
        env['TEMP'] = env['TMP'] = str(build_temp)
        status['build_temp'] = {'path':build_temp.name,'scope':'adjacent_owned_retained_not_payload'}
        with zipfile.ZipFile(archive) as z:
            seen = set(); total = 0
            for item in z.infolist():
                p = PurePosixPath(item.filename); total += item.file_size
                require(not p.is_absolute() and '..' not in p.parts and ':' not in item.filename and '\\' not in item.filename
                  and item.filename not in seen and ((item.external_attr>>16)&0o170000) != 0o120000, 'unsafe_embed_archive')
                seen.add(item.filename)
            require(len(seen) <= 256 and total <= 128*1024*1024, 'embed_archive_capacity')
            z.extractall(output)
        package_root = output/'Lib/site-packages'
        command = [str(builder),'-I','-X','utf8','-B','-m','pip','--isolated','install','--disable-pip-version-check','--no-cache-dir',
          '--no-index','--only-binary=:all:','--require-hashes','--no-compile','--find-links',str(Path(args.wheelhouse).resolve()),
          '--target',str(package_root),'-r',str(requirements)]
        installed = subprocess.run(command,env=env,capture_output=True,text=True,encoding='utf-8',timeout=180)
        write_exclusive(output.with_name(output.name+'.pip.stdout'),installed.stdout.encode('utf-8'))
        write_exclusive(output.with_name(output.name+'.pip.stderr'),installed.stderr.encode('utf-8'))
        status['pip_exit_code'] = installed.returncode
        require(installed.returncode == 0, 'offline_wheel_install_failed')
        require(not list(package_root.rglob('direct_url.json')), 'unexpected_direct_url_metadata')
        observed_pth = {str(f.relative_to(output)).replace('\\','/'):digest(f) for f in package_root.glob('*.pth')}
        require(observed_pth == runtime['allowed_pth_sha256'], 'unreviewed_pth_hook')
        for relative, expected_sha in runtime['reviewed_hook_sha256'].items():
            require(digest(regular(output/relative)) == expected_sha, 'unreviewed_bootstrap_hook')
        (output/'python314._pth').write_text('\n'.join(PTH)+'\n',encoding='utf-8')
        shutil.copyfile(shim,package_root/'sitecustomize.py')
        require(digest(package_root/'sitecustomize.py') == runtime['sitecustomize_sha256'], 'shim_copy_mismatch')
        entrypoints = {}
        for f in package_root.glob('*.dist-info/entry_points.txt'):
            cp = configparser.ConfigParser(); cp.optionxform = str; cp.read(f,encoding='utf-8')
            for section in ('console_scripts','gui_scripts'):
                if cp.has_section(section):
                    for name,value in cp.items(section):entrypoints[name] = {'distribution':f.parent.name,'section':section,'entrypoint':value}
        excludes = []
        for f in sorted((package_root/'bin').glob('*.exe')):
            require(f.stem in entrypoints, 'unknown_bin_executable')
            data = f.read_bytes()
            require(b'#!' in data and str(builder).encode('utf-8') in data, 'bin_not_confirmed_pip_wrapper')
            excludes.append({'path':str(f.relative_to(output)).replace('\\','/'),'bytes':len(data),'sha256':digest(f),
              'reason':'pip_generated_builder_shebang_not_product_entrypoint','source':entrypoints[f.stem]})
        status['final_copy_excludes'] = excludes
        status['prepared_wrappers_retained'] = True
        status['upstream_modules_data_libs_distinfo_pth_preserved'] = True
        status['postinstall_scripts_executed'] = False
        status.update(manifest_tree(output))
        status['completed'] = True
    except BaseException as exc:
        status['error_type'] = type(exc).__name__
        status['error'] = str(exc) if isinstance(exc,ValueError) else 'preparation_interrupted_or_failed'
        raise
    finally:
        status['elapsed_seconds'] = time.monotonic()-start
        write_exclusive(output.with_name(output.name+'.prepare.json'),encoded(status))
    return {'completed':True,'files':status['file_count'],'bytes':status['bytes'],'excluded_generated_wrappers':len(excludes)}
def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('runtime-lock','wheel-lock','requirements','cache','wheelhouse','builder-python','sitecustomize','output'):
        parser.add_argument('--'+name,required=True)
    result = run(parser.parse_args()); print(json.dumps(result))
def manifest_tree(output):
    """Recheck every file and directory; preserve empty directory entries."""
    output = Path(output)
    files = []; directories = []
    for f in sorted(output.rglob('*')):
        require(not f.is_symlink() and not f.is_junction(), 'prepared_link')
        relative = str(f.relative_to(output)).replace('\\','/')
        if f.is_dir():
            directories.append(relative)
        elif f.is_file():
            regular(f); files.append({'path':relative,'bytes':f.stat().st_size,'sha256':digest(f)})
        else:
            raise ValueError('unknown_prepared_entry')
    return {'files':files,'directories':directories,'file_count':len(files),
            'directory_count':len(directories),'bytes':sum(v['bytes'] for v in files)}
if __name__ == '__main__': main()
