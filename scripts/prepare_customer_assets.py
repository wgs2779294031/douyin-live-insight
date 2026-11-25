"""Prepare locked runtime assets from an explicit offline cache, using stdlib only.

No downloads, asset execution, installation, update state, or global mutation.
An output with manifest.json is complete; failed partial output is retained.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import zipfile

LOCK_LIMIT = 2 * 1024 * 1024
BLOCK = 1024 * 1024
MODEL_NAMES = {'model.bin', 'config.json', 'tokenizer.json', 'vocabulary.txt'}
RESERVED = {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)),
            *(f'LPT{i}' for i in range(1, 10))}


class PreparationError(ValueError):
    pass


def require(condition, reason):
    if not condition:
        raise PreparationError(reason)


def exact(value, keys):
    require(isinstance(value, dict) and set(value) == set(keys), 'invalid_lock_shape')


def integer(value, low, high):
    return type(value) is int and low <= value <= high


def relative(value):
    require(isinstance(value, str) and 0 < len(value) <= 1024, 'invalid_relative_path')
    parts = value.split('/')
    require(all(p and p not in ('.', '..') and len(p) <= 255 and
                not p.endswith((' ', '.')) and
                p.split('.')[0].upper() not in RESERVED and
                not re.search(r'[<>:"\\|?*\x00-\x1f]', p) for p in parts),
            'invalid_relative_path')
    return PurePosixPath(value)


def no_link(path, *, directory=False):
    info = path.lstat()
    require(not stat.S_ISLNK(info.st_mode) and
            not (getattr(info, 'st_file_attributes', 0) & 0x400), 'linked_path')
    if directory:
        require(stat.S_ISDIR(info.st_mode), 'not_directory')
    else:
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, 'not_single_regular_file')
    return info


def directory_chain(path):
    path = Path(os.path.abspath(path))
    for component in (path, *path.parents):
        no_link(component, directory=True)
    return path


def signature(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_nlink)


def unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, 'duplicate_json_key')
        result[key] = value
    return result


def load_lock(path):
    no_link(path)
    with path.open('rb') as handle:
        raw = handle.read(LOCK_LIMIT + 1)
    require(len(raw) <= LOCK_LIMIT, 'lock_too_large')
    try:
        lock = json.loads(raw, object_pairs_hook=unique_pairs)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise PreparationError('invalid_lock_json') from exc
    exact(lock, ('schema', 'kind', 'inputs', 'node', 'ffmpeg', 'model', 'notices'))
    require(type(lock['schema']) is int and lock['schema'] == 1 and
            lock['kind'] == 'offline_runtime_assets', 'invalid_lock_schema')
    inputs = lock['inputs']
    require(isinstance(inputs, dict) and 6 <= len(inputs) <= 32, 'invalid_inputs')
    cache_paths = set()
    for key, item in inputs.items():
        require(isinstance(key, str) and re.fullmatch(r'[a-z][a-z0-9_]{0,63}', key), 'invalid_input_id')
        exact(item, ('cache_path', 'url', 'bytes', 'sha256'))
        relative(item['cache_path'])
        require(item['cache_path'].casefold() not in cache_paths, 'duplicate_cache_path')
        cache_paths.add(item['cache_path'].casefold())
        require(integer(item['bytes'], 1, 512 * BLOCK), 'invalid_input_size')
        require(isinstance(item['sha256'], str) and re.fullmatch(r'[0-9a-f]{64}', item['sha256']), 'invalid_input_digest')
        # Provenance only: no URL is ever requested by this program.
        from urllib.parse import urlsplit
        require(isinstance(item['url'], str), 'invalid_public_source_url')
        try:
            url = urlsplit(item['url'])
            valid_url = (url.scheme == 'https' and bool(url.hostname) and
                         url.port in (None, 443) and not url.username and not url.password and
                         not url.query and not url.fragment)
        except (ValueError, TypeError):
            valid_url = False
        require(valid_url, 'invalid_public_source_url')
    used = set()
    for kind, cap, count in (('node', 100 * BLOCK, 20000), ('ffmpeg', 256 * BLOCK, 10000)):
        item = lock[kind]
        exact(item, ('input', 'version', 'zip_prefix', 'max_entries', 'max_uncompressed_bytes'))
        require(isinstance(item['input'], str) and item['input'] in inputs, 'unknown_archive_input')
        require(inputs[item['input']]['bytes'] <= cap, 'archive_input_too_large')
        require(isinstance(item['version'], str) and re.fullmatch(r'[A-Za-z0-9_.+-]{1,100}', item['version']), 'invalid_version')
        require(len(relative(item['zip_prefix']).parts) == 1, 'invalid_zip_prefix')
        require(integer(item['max_entries'], 1, count) and
                integer(item['max_uncompressed_bytes'], 1, 1024 * BLOCK), 'invalid_archive_cap')
        used.add(item['input'])
    exact(lock['model'], ('revision', 'files'))
    require(isinstance(lock['model']['revision'], str) and re.fullmatch(r'[0-9a-f]{40}', lock['model']['revision']), 'invalid_model_revision')
    require(isinstance(lock['model']['files'], dict) and set(lock['model']['files']) == MODEL_NAMES, 'invalid_model_files')
    for name, key in lock['model']['files'].items():
        require(isinstance(key, str) and key in inputs, 'unknown_model_input')
        used.add(key)
    require(len(used) == 6, 'duplicate_asset_input')
    require(isinstance(lock['notices'], list) and 1 <= len(lock['notices']) <= 32, 'invalid_notices')
    targets = set()
    for item in lock['notices']:
        exact(item, ('source', 'target', 'bytes', 'sha256'))
        relative(item['target'])
        require(item['target'].casefold() not in targets, 'duplicate_notice_target')
        targets.add(item['target'].casefold())
        require(integer(item['bytes'], 1, 16 * BLOCK) and isinstance(item['sha256'], str) and
                re.fullmatch(r'[0-9a-f]{64}', item['sha256']), 'invalid_notice_identity')
        source = item['source']
        require(isinstance(source, dict), 'invalid_notice_source')
        if set(source) == {'input'}:
            require(isinstance(source['input'], str) and source['input'] in inputs and inputs[source['input']]['bytes'] <= 16 * BLOCK,
                    'unknown_notice_input')
            require((item['bytes'], item['sha256']) ==
                    (inputs[source['input']]['bytes'], inputs[source['input']]['sha256']), 'notice_input_conflict')
            used.add(source['input'])
        else:
            exact(source, ('archive', 'path'))
            require(source['archive'] in ('node', 'ffmpeg'), 'unknown_notice_archive')
            relative(source['path'])
    require(used == set(inputs), 'unused_or_unknown_input')
    return lock, hashlib.sha256(raw).hexdigest()


class CachedInput:
    def __init__(self, root, item, stack):
        self.item = item
        self.path = root.joinpath(*relative(item['cache_path']).parts)
        directory_chain(self.path.parent)
        before = no_link(self.path)
        require(before.st_size == item['bytes'], 'input_size_mismatch')
        self.handle = stack.enter_context(self.path.open('rb'))
        self.initial = signature(os.fstat(self.handle.fileno()))
        require(self.initial == signature(before), 'input_identity_changed')
        self.verify()

    def stable(self):
        require(signature(no_link(self.path)) == self.initial ==
                signature(os.fstat(self.handle.fileno())), 'input_identity_changed')

    def verify(self):
        self.handle.seek(0)
        digest = hashlib.file_digest(self.handle, 'sha256').hexdigest()
        require(digest == self.item['sha256'], 'input_digest_mismatch')
        self.stable()

    def bytes(self, cap):
        require(self.item['bytes'] <= cap, 'input_cap_exceeded')
        self.handle.seek(0)
        raw = self.handle.read(cap + 1)
        require(len(raw) == self.item['bytes'] and hashlib.sha256(raw).hexdigest() == self.item['sha256'],
                'input_changed')
        self.stable()
        return raw


def prepare(lock_path, cache_dir, output):
    lock, lock_digest = load_lock(Path(lock_path))
    cache = directory_chain(Path(cache_dir))
    output = Path(os.path.abspath(output))
    directory_chain(output.parent)
    try:
        output.lstat()
    except FileNotFoundError:
        pass
    else:
        raise PreparationError('output_already_exists')
    manifest = {}
    directories = {'node', 'ffmpeg', 'model', 'notices'}

    def make_directory(name):
        current = output
        no_link(current, directory=True)
        for component in relative(name).parts:
            current = current / component
            try:
                current.mkdir()
            except FileExistsError:
                pass
            no_link(current, directory=True)
            directories.add(current.relative_to(output).as_posix())
        return current

    def target(name):
        parts = relative(name).parts
        parent = make_directory('/'.join(parts[:-1])) if len(parts) > 1 else output
        no_link(parent, directory=True)
        return parent / parts[-1]

    def write_stream(name, source, size, expected=None):
        require(name.casefold() not in manifest, 'duplicate_output_path')
        dest = target(name)
        count = 0
        digest = hashlib.sha256()
        with dest.open('xb') as out:
            while block := source.read(BLOCK):
                count += len(block)
                require(count <= size, 'output_size_exceeded')
                out.write(block)
                digest.update(block)
        require(count == size, 'output_size_mismatch')
        value = digest.hexdigest()
        require(expected is None or value == expected, 'output_digest_mismatch')
        no_link(dest)
        manifest[name.casefold()] = {'path': name, 'bytes': count, 'sha256': value}

    def write_bytes(name, raw, expected=None):
        write_stream(name, io.BytesIO(raw), len(raw), expected)

    def unpack(kind, cached):
        spec = lock[kind]
        cap = (100 if kind == 'node' else 256) * BLOCK
        raw = cached.bytes(cap)  # Immutable, hash-verified ZIP bytes; <=256MiB peak archive input.
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            entries = archive.infolist()
            require(len(entries) <= spec['max_entries'], 'archive_entry_cap')
            total = 0
            planned = []
            planned_directories = set()
            member_names = set()
            file_targets = set()
            directory_targets = set()
            for entry in entries:
                raw_name = entry.filename[:-1] if entry.is_dir() else entry.filename
                parts = relative(raw_name).parts
                require(parts[0] == spec['zip_prefix'], 'unexpected_zip_prefix')
                require(raw_name.casefold() not in member_names, 'duplicate_archive_member')
                member_names.add(raw_name.casefold())
                mode = entry.external_attr >> 16
                require(stat.S_IFMT(mode) in (0, stat.S_IFREG, stat.S_IFDIR) and
                        not (entry.flag_bits & 1), 'special_or_encrypted_archive_member')
                total += entry.file_size
                require(total <= spec['max_uncompressed_bytes'], 'archive_size_cap')
                tail = parts[1:]
                if not tail:
                    require(entry.is_dir(), 'invalid_archive_root')
                    continue
                # Full Node root; FFmpeg bin files move to its root, other contents retain layout.
                if kind == 'ffmpeg' and tail[0] == 'bin':
                    if len(tail) == 1:
                        require(entry.is_dir(), 'invalid_ffmpeg_bin')
                        continue
                    require(len(tail) == 2 and not entry.is_dir(), 'nested_ffmpeg_bin')
                    tail = tail[1:]
                name = '/'.join((kind, *tail))
                folded = name.casefold()
                for parent in PurePosixPath(name).parents:
                    require(parent.as_posix().casefold() not in file_targets, 'file_directory_conflict')
                    directory_targets.add(parent.as_posix().casefold())
                if entry.is_dir():
                    require(folded not in file_targets, 'file_directory_conflict')
                    directory_targets.add(folded)
                    planned_directories.add(name)
                    continue
                require(folded not in file_targets and folded not in directory_targets, 'output_alias_conflict')
                file_targets.add(folded)
                planned.append((entry, name))
            for name in sorted(planned_directories):
                make_directory(name)
            for entry, name in planned:
                with archive.open(entry) as stream:
                    write_stream(name, stream, entry.file_size)
        cached.stable()

    with contextlib.ExitStack() as stack:
        # No output is created until all required cache entries verify.
        inputs = {key: CachedInput(cache, item, stack) for key, item in lock['inputs'].items()}
        output.mkdir()
        for name in ('node', 'ffmpeg', 'model', 'notices'):
            (output / name).mkdir()
        for kind in ('node', 'ffmpeg'):
            unpack(kind, inputs[lock[kind]['input']])
        for name, key in sorted(lock['model']['files'].items()):
            cached = inputs[key]
            cached.handle.seek(0)
            write_stream('model/' + name, cached.handle, cached.item['bytes'], cached.item['sha256'])
            cached.stable()
        for notice in lock['notices']:
            source = notice['source']
            if 'input' in source:
                raw = inputs[source['input']].bytes(16 * BLOCK)
            else:
                name = source['archive'] + '/' + source['path']
                known = manifest.get(name.casefold())
                require(known is not None and (known['bytes'], known['sha256']) ==
                        (notice['bytes'], notice['sha256']), 'archive_notice_conflict')
                path = output.joinpath(*relative(name).parts)
                no_link(path)
                raw = path.read_bytes()
            require(len(raw) == notice['bytes'], 'notice_size_mismatch')
            write_bytes('notices/' + notice['target'], raw, notice['sha256'])
        for cached in inputs.values():
            cached.stable()
        origin = {'schema': 1, 'kind': 'offline_asset_origins', 'lock_sha256': lock_digest,
                  'inputs': lock['inputs'], 'layouts': {'node': lock['node'], 'ffmpeg': lock['ffmpeg'],
                  'model': lock['model']}, 'notices': lock['notices']}
        write_bytes('origin.json', (json.dumps(origin, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode('utf-8'))
        result = {'schema': 1, 'kind': 'prepared_runtime_assets', 'lock_sha256': lock_digest,
                  'directories': sorted(directories),
                  'files': sorted(manifest.values(), key=lambda item: item['path'])}
        # The manifest excludes itself to avoid a circular digest. It is written last.
        raw = (json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode('utf-8')
        with (output / 'manifest.json').open('xb') as handle:
            handle.write(raw)
        return {'files': len(result['files']), 'directories': len(result['directories']),
                'bytes': sum(item['bytes'] for item in result['files']),
                'manifest_sha256': hashlib.sha256(raw).hexdigest(), 'lock_sha256': lock_digest}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--lock', required=True, type=Path)
    parser.add_argument('--cache-dir', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        result = prepare(args.lock, args.cache_dir, args.output)
    except (PreparationError, OSError, zipfile.BadZipFile) as exc:
        reason = str(exc) if isinstance(exc, PreparationError) else type(exc).__name__
        print(json.dumps({'ok': False, 'reason': reason}), file=sys.stderr)
        return 1
    print(json.dumps({'ok': True, **result}, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
