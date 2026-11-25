"""Build the small stable .NET4 launcher using the existing Windows compiler.

No global installation, NuGet restore, download, product execution or registry
configuration. Binary output must be a fresh directory outside the source tree.
"""
from __future__ import annotations
import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
RESERVED={'CON','PRN','AUX','NUL','CONIN$','CONOUT$',*(f'COM{i}' for i in range(1,10)),*(f'LPT{i}' for i in range(1,10))}

def regular_path(value):
    text=os.fspath(value)
    if (not re.match(r'^[A-Za-z]:[\\/]',text) or text.startswith(('\\\\','//')) or
            any(p in ('','..','.') or p.endswith(('.', ' ')) or p.split('.')[0].rstrip(' ').upper() in RESERVED
                or re.search(r'[<>:"|?*\x00-\x1f]',p) for p in text[3:].replace('/','\\').split('\\'))):
        raise ValueError('ordinary_absolute_path_required')
    return Path(os.path.abspath(text))

def no_links(path, *, file=False):
    for part in (path,*path.parents):
        info=part.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info,'st_file_attributes',0)&0x400:
            raise ValueError('linked_build_path')
    if file and (not path.is_file() or path.stat().st_nlink!=1):
        raise ValueError('invalid_source_file')

def sha(path):
    with path.open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()

def file_version(path):
    api=ctypes.WinDLL('version',use_last_error=True)
    api.GetFileVersionInfoSizeW.argtypes=[ctypes.c_wchar_p,ctypes.POINTER(ctypes.c_uint32)]
    api.GetFileVersionInfoSizeW.restype=ctypes.c_uint32
    unused=ctypes.c_uint32();size=api.GetFileVersionInfoSizeW(str(path),ctypes.byref(unused))
    if not size:return None
    buffer=ctypes.create_string_buffer(size)
    api.GetFileVersionInfoW.argtypes=[ctypes.c_wchar_p,ctypes.c_uint32,ctypes.c_uint32,ctypes.c_void_p]
    api.GetFileVersionInfoW.restype=ctypes.c_int
    if not api.GetFileVersionInfoW(str(path),0,size,buffer):return None
    value=ctypes.c_void_p();length=ctypes.c_uint32()
    api.VerQueryValueW.argtypes=[ctypes.c_void_p,ctypes.c_wchar_p,ctypes.POINTER(ctypes.c_void_p),ctypes.POINTER(ctypes.c_uint32)]
    api.VerQueryValueW.restype=ctypes.c_int
    if not api.VerQueryValueW(buffer,'\\',ctypes.byref(value),ctypes.byref(length)) or length.value<52:return None
    words=ctypes.cast(value,ctypes.POINTER(ctypes.c_uint32))
    return '.'.join(map(str,(words[2]>>16,words[2]&65535,words[3]>>16,words[3]&65535)))

def build(output):
    if os.name!='nt':raise ValueError('windows_required')
    output=regular_path(output)
    no_links(output.parent)
    if output.is_relative_to(ROOT) or ROOT.is_relative_to(output):raise ValueError('binary_output_must_be_external')
    try:output.lstat()
    except FileNotFoundError:pass
    else:raise ValueError('output_already_exists')
    system=Path(os.environ.get('SystemRoot','C:/Windows'))
    compiler=system/'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
    if not compiler.is_file():raise ValueError('existing_framework_compiler_unavailable')
    no_links(compiler)
    source=ROOT/'launcher/CustomerLauncher.cs';no_links(source,file=True)
    source_hash=sha(source)
    output.mkdir()
    env={key:value for key,value in os.environ.items() if key.upper() in {'SYSTEMROOT','WINDIR','COMSPEC','TEMP','TMP'}}
    env['PATH']=str(system/'System32')
    references=[compiler.parent/name for name in ('mscorlib.dll','System.dll','System.Core.dll')]
    result={'schema':1,'kind':'customer_launcher_build','source':str(source),'source_sha256':source_hash,
      'compiler':str(compiler),'compiler_sha256':sha(compiler),'compiler_file_version':file_version(compiler),
      'framework_installed_by_builder':False,'architecture':'x64','runs':[]}
    try:
        for target,name in [('exe','CustomerLauncher.exe'),('winexe','CustomerLauncherW.exe')]:
            executable=output/name
            argv=[str(compiler),'/nologo','/noconfig','/nostdlib+','/optimize+','/debug-',
                  '/platform:x64','/target:'+target,'/out:'+str(executable),
                  *('/reference:'+str(path) for path in references),str(source)]
            run=subprocess.run(argv,cwd=output,env=env,capture_output=True,timeout=60,creationflags=0x08000000)
            (output/(name+'.stdout.txt')).write_bytes(run.stdout)
            (output/(name+'.stderr.txt')).write_bytes(run.stderr)
            result['runs'].append({'argv':argv,'returncode':run.returncode})
            if run.returncode:raise ValueError('launcher_compile_failed')
            result.setdefault('binaries',{})[name]={'bytes':executable.stat().st_size,'sha256':sha(executable)}
        if sha(source)!=source_hash:raise ValueError('source_changed_during_build')
        result['ok']=True
    except BaseException:
        result['ok']=False
        raise
    finally:
        result['source_sha256_after']=sha(source)
        (output/'build.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    return result

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir',required=True,type=Path)
    args=parser.parse_args(argv)
    try:
        result=build(args.output_dir)
    except (ValueError,OSError,subprocess.SubprocessError):
        print(json.dumps({'ok':False,'code':'launcher_build_failed_artifacts_retained'}),file=sys.stderr)
        return 1
    print(json.dumps({'ok':True,'source_sha256':result['source_sha256'],'binaries':result['binaries']}))
    return 0

if __name__=='__main__':raise SystemExit(main())
