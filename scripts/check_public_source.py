"""Check the declared public source without importing application modules."""
from __future__ import annotations

import ast
import json
from pathlib import Path, PurePosixPath
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_PARTS = {'.git', '.venv', 'node_modules', 'browser-profile', 'captures', 'data', 'anchor_profiles'}
FORBIDDEN_SUFFIXES = {'.exe', '.dll', '.pyd', '.whl', '.zip', '.log', '.db', '.sqlite', '.wav', '.mp3', '.aac', '.flv', '.pyc'}


def declared_paths():
    if (ROOT / '.git').exists():
        raw = subprocess.check_output(['git', '-C', str(ROOT), 'ls-files', '-z'])
        return [name for name in raw.decode('utf-8').split('\0') if name]
    manifest = json.loads((ROOT / 'PUBLIC_SOURCE_MANIFEST.json').read_text(encoding='utf-8'))
    return [row['path'] for row in manifest['files']]


def main():
    failures, counts = [], {'files': 0, 'python': 0, 'json': 0}
    for name in declared_paths():
        parts = PurePosixPath(name).parts
        path = ROOT / name
        if (not parts or name.startswith('/') or '\\' in name or ':' in name
                or any(part in FORBIDDEN_PARTS or part in ('.', '..') for part in parts)
                or path.suffix.lower() in FORBIDDEN_SUFFIXES
                or name in {'config.json', 'anchors.json', 'products.json', '.env.watch'}):
            failures.append({'path': name, 'type': 'forbidden_public_path'})
            continue
        if not path.is_file() or path.is_symlink():
            failures.append({'path': name, 'type': 'missing_or_linked_source'})
            continue
        data = path.read_bytes()
        counts['files'] += 1
        if b'\0' in data:
            failures.append({'path': name, 'type': 'binary_content'})
            continue
        try:
            text = data.decode('utf-8-sig')
            if path.suffix == '.py':
                ast.parse(text, filename=name)
                counts['python'] += 1
            elif path.suffix == '.json':
                json.loads(text)
                counts['json'] += 1
        except (UnicodeDecodeError, SyntaxError, ValueError):
            failures.append({'path': name, 'type': 'invalid_text_or_syntax'})
    print(json.dumps({'ok': not failures, 'counts': counts, 'failures': failures}, ensure_ascii=True))
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
