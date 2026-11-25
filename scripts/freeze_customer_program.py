"""Create an explicit vendor-reviewed program file lock, never a tree archive."""
from __future__ import annotations
import argparse
import ast
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess

ROOT = Path(__file__).resolve().parents[1]
ENTRY_FILES = {
    'main.py', 'scripts/local_app.py', 'scripts/local_mcp.py', 'scripts/run_watchdog.py',
    'scripts/watch_anchor_live.py', 'scripts/stream_recorder.py', 'scripts/stream_dns_worker.py', 'scripts/asr_worker.py',
    'scripts/transcribe_backfill.py', 'scripts/portrait_backfill.py', 'scripts/local_installation.py',
    'scripts/local_doctor.py',
}
EXPLICIT_RESOURCES = {
    'config.example.json', 'products.example.json', 'runtime-bootstrap/sitecustomize.py',
    'baseline/installation-layout.example.json', 'baseline/model-baseline.json',
    'baseline/python-runtime-lock.json', 'baseline/python-wheel-lock.json',
    'baseline/requirements-runtime.lock.txt', 'baseline/customer-assets.lock.json',
    'baseline/RUNTIME_INSTALLATION.md', 'AGENT_INSTALL.md', 'LICENSE', 'THIRD_PARTY_NOTICES.md',
}
EXTERNAL_DISTRIBUTION_INPUTS = {'scripts/customer_install.py', 'scripts/customer_upgrade.py',
    'scripts/customer_upgrade_gate.py', 'launcher/CustomerLauncher.cs'}


def _output_path(output):
    text = os.fspath(output)
    if (not text or text.startswith(('\\', '/')) or '://' in text
            or any(ord(char) < 32 for char in text)):
        raise ValueError('invalid_output_path')
    path = Path(text)
    drive = path.drive
    if drive and (len(drive) != 2 or drive[0] not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ'
                  or drive[1] != ':' or text[2:3] not in ('/', '\\')):
        raise ValueError('invalid_output_path')
    tail = text[len(drive):]
    if any(char in ':<>"|?*' for char in tail):
        raise ValueError('invalid_output_path')
    parts = tail.replace('\\', '/').split('/')
    reserved = {'CON', 'PRN', 'AUX', 'NUL', 'CONIN$', 'CONOUT$',
                *(f'{prefix}{number}' for prefix in ('COM', 'LPT') for number in '123456789¹²³')}
    if (parts[-1] in ('', '.', '..')
            or any(part not in ('', '.', '..') and (part.endswith((' ', '.'))
                   or part.split('.')[0].rstrip(' ').upper() in reserved) for part in parts)):
        raise ValueError('invalid_output_path')
    return path.absolute()


def freeze(output, source_manifest=None):
    output = _output_path(output)
    if source_manifest is None:
        raw = subprocess.check_output(['git', '-C', str(ROOT), 'ls-files', '--cached', '--others', '--exclude-standard', '-z'])
        names = {name for name in raw.decode('utf-8').split('\0') if name and (ROOT / name).is_file()}
    else:
        manifest = json.loads(Path(source_manifest).read_text(encoding='utf-8'))
        names = set()
        for row in manifest['files']:
            name = row['path']
            parts = name.replace('\\', '/').split('/')
            if (not name or '\\' in name or name.startswith('/') or ':' in name
                    or any(part in ('', '.', '..', '.git') for part in parts) or name in names):
                raise ValueError('invalid_source_manifest_path')
            path = ROOT / name
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != row['sha256']:
                raise ValueError('source_manifest_content_changed')
            names.add(name)
    modules, canonical = {}, {}
    for name in sorted(names):
        if name.endswith('.py') and not name.startswith(('tests/', 'web/', 'runtime-bootstrap/')):
            module = name[:-3].replace('/', '.')
            if module.endswith('.__init__'):
                module = module[:-9]
            modules[module], canonical[name] = name, module
            if name.startswith('scripts/'):
                modules.setdefault(Path(name).stem, name)
    selected, todo = set(), list(ENTRY_FILES)
    while todo:
        name = todo.pop()
        if name in selected:
            continue
        if name not in names:
            raise ValueError('missing_program_entry')
        selected.add(name)
        module = canonical[name]
        package = module if name.endswith('/__init__.py') else module.rpartition('.')[0]
        tree = ast.parse((ROOT / name).read_text(encoding='utf-8-sig'))
        imports = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(item.name for item in node.names)
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ''
                if node.level:
                    base = importlib.util.resolve_name('.' * node.level + base, package)
                imports.add(base)
                imports.update(base + '.' + item.name for item in node.names)
        for imported in imports:
            parts = imported.split('.')
            for length in range(1, len(parts) + 1):
                target = modules.get('.'.join(parts[:length]))
                if target and target not in selected:
                    todo.append(target)
    release = json.loads((ROOT / 'baseline/release-scope.json').read_text(encoding='utf-8'))
    resources = {name for name in release['candidate_files'] if name.endswith(('.js', '.mjs', '.html', '.css', '.proto'))
                 or name.startswith('anchor_profiles.example/')}
    selected |= resources | EXPLICIT_RESOURCES | set(release.get('license_files', []))
    if not selected <= names:
        raise ValueError('missing_explicit_program_resource')
    files = []
    for name in sorted(selected):
        path = ROOT / name
        for parent in (path, *path.parents):
            info = parent.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise ValueError('linked_program_input')
        if not path.is_file() or path.stat().st_nlink != 1:
            raise ValueError('invalid_program_input')
        data = path.read_bytes()
        files.append({'path': name, 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()})
    value = {'schema': 1, 'kind': 'customer_program_allowlist',
             'entry_files': sorted(ENTRY_FILES), 'files': files,
             'rule': 'Explicit entries plus conservative static local imports and named dynamic resources; builder never copies the worktree or Git history.',
             'state_not_program': ['config.json', 'anchors.json', 'data', 'browser-profile', 'captures', 'anchor_profiles'],
             'external_distribution_source_inputs_not_app': sorted(EXTERNAL_DISTRIBUTION_INPUTS & names),
             'unsupported_legacy_cli_not_shipped': sorted(set(release['candidate_files']) - selected - EXTERNAL_DISTRIBUTION_INPUTS),
             'ready_to_export': False}
    for parent in (output.parent, *output.parent.parents):
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('linked_output_parent')
    try:
        output.lstat()
    except FileNotFoundError:
        pass
    else:
        raise ValueError('program_lock_output_exists')
    with output.open('x', encoding='utf-8', newline='\n') as stream:
        stream.write(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'program_files': len(files), 'bytes': sum(row['bytes'] for row in files),
                      'sha256': hashlib.sha256(output.read_bytes()).hexdigest()}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', default=str(ROOT / 'baseline/customer-program-files.json'))
    parser.add_argument('--source-manifest', help='Reviewed source list for an export without Git metadata')
    args = parser.parse_args()
    freeze(args.output, args.source_manifest)
