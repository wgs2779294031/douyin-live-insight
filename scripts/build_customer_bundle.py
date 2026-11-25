"""Build an offline directory/ZIP bundle from a fresh state-free payload.

Run build_customer_payload without --sample-state first. This tool never takes
an installed customer tree as a product input and never downloads dependencies.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import zipfile

try:
    from . import customer_bundle as bundle
    from .customer_install import copy_verified
except ImportError:
    import customer_bundle as bundle
    from customer_install import copy_verified

ROOT = Path(__file__).resolve().parents[1]


def _row(path, *, limit=bundle.MAX_FILE):
    info = bundle.no_link(path)
    bundle.require(info.st_size <= limit, 'bundle_capacity')
    digest = hashlib.sha256()
    count = 0
    with Path(path).open('rb') as stream:
        bundle.require(bundle.signature(os.fstat(stream.fileno())) == bundle.signature(info), 'bundle_input_changed')
        while chunk := stream.read(bundle.BLOCK):
            count += len(chunk)
            bundle.require(count <= info.st_size, 'bundle_input_changed')
            digest.update(chunk)
        bundle.require(bundle.signature(os.fstat(stream.fileno())) == bundle.signature(info), 'bundle_input_changed')
    bundle.require(bundle.signature(bundle.no_link(path)) == bundle.signature(info), 'bundle_input_changed')
    return {'bytes':count,'sha256':digest.hexdigest()}


def _payload_inventory(root):
    value = bundle.read_json(root/'payload-manifest.json')
    bundle.require(isinstance(value, dict) and type(value.get('schema')) is int and value['schema'] == 1
        and value.get('kind') == 'customer_payload_sample' and value.get('sample_state') is False,
        'bundle_stateful_payload_refused')
    rows, dirs = value.get('files'), value.get('directories')
    bundle.require(isinstance(rows, list) and len(rows) < bundle.MAX_FILES and isinstance(dirs, list)
        and len(dirs) < bundle.MAX_DIRS, 'bundle_capacity')
    files, aliases = {}, set()
    for row in rows:
        bundle.require(isinstance(row, dict) and set(row) == {'path','bytes','sha256'}, 'bundle_payload_invalid')
        name = bundle.relative(row['path'])
        bundle.require(name.casefold() not in aliases and type(row['bytes']) is int and 0 <= row['bytes'] <= bundle.MAX_FILE
            and isinstance(row['sha256'], str) and bundle.HEX.fullmatch(row['sha256']), 'bundle_payload_invalid')
        bundle.require(name.split('/')[0] in {'app','runtime','assets','notices','installation.json'}, 'bundle_stateful_payload_refused')
        if name.startswith('app/'):
            bundle.program_path(name.removeprefix('app/'))
        files[name] = row
        aliases.add(name.casefold())
    normalized = set()
    for name in dirs:
        bundle.relative(name)
        if name.startswith('app/'):
            bundle.program_path(name.removeprefix('app/'))
        bundle.require(name.casefold() not in aliases and name.split('/')[0] in {'app','runtime','assets','notices'},
            'bundle_payload_invalid')
        aliases.add(name.casefold())
        normalized.add(name)
    actual_files, actual_dirs = bundle.scan_tree(root)
    bundle.require(actual_files == set(files) | {'payload-manifest.json'} and actual_dirs == normalized,
        'bundle_unlisted_payload_path')
    # The root inventory is itself included as an explicit immutable metadata row.
    files['payload-manifest.json'] = {'path':'payload-manifest.json', **_row(root/'payload-manifest.json')}
    return files, normalized


def _manager_python(archive, lock, output):
    runtime = bundle.read_json(lock)
    expected = runtime.get('python', {}).get('embeddable', {})
    bundle.require(type(runtime.get('schema')) is int and runtime['schema'] == 1
        and runtime.get('python', {}).get('version') == '3.14.10'
        and expected.get('filename') == Path(archive).name, 'bundle_manager_python_invalid')
    bundle.check_file(archive, expected, deep=True)
    with zipfile.ZipFile(archive) as incoming:
        members = incoming.infolist()
        bundle.require(len(members) <= 1000, 'bundle_capacity')
        seen, total = set(), 0
        for member in members:
            name = bundle.relative(member.filename.rstrip('/'))
            bundle.require(name.casefold() not in seen and not member.flag_bits & 1
                and not stat.S_ISLNK(member.external_attr >> 16), 'bundle_embed_archive_invalid')
            seen.add(name.casefold())
            total += member.file_size
            bundle.require(member.file_size <= bundle.MAX_FILE and total <= 128 * bundle.BLOCK, 'bundle_capacity')
            destination = output/name
            if member.is_dir():
                parts = name.split('/')
                bundle.make_directories(output, {'/'.join(parts[:i]) for i in range(1,len(parts)+1)})
                continue
            parts = name.split('/')
            bundle.make_directories(output, {'/'.join(parts[:i]) for i in range(1,len(parts))})
            if name == 'python314._pth':
                continue
            with bundle.guard_path(destination.parent), incoming.open(member) as source, destination.open('xb') as target:
                count = 0
                while chunk := source.read(bundle.BLOCK):
                    count += len(chunk)
                    bundle.require(count <= member.file_size, 'bundle_embed_archive_invalid')
                    target.write(chunk)
                bundle.require(count == member.file_size, 'bundle_embed_archive_invalid')
                target.flush()
                os.fsync(target.fileno())
        bundle.write_new(output/'python314._pth', b'python314.zip\n.\n../code\n')


def create_zip(root, destination, files, directories):
    """Create only listed entries; validate each source while streaming to ZIP."""
    destination = bundle.absolute(destination)
    bundle.absent(destination)
    with bundle.guard_path(root), bundle.guard_path(destination.parent):
        with zipfile.ZipFile(destination, 'x', compression=zipfile.ZIP_DEFLATED, compresslevel=1, allowZip64=True) as archive:
            for name in sorted(directories):
                entry = zipfile.ZipInfo(name+'/', (1980,1,1,0,0,0))
                entry.external_attr = (0o40755 << 16) | 0x10
                archive.writestr(entry, b'')
            for name, row in sorted(files.items()):
                with bundle.guard_path((root/name).parent):
                    before = bundle.no_link(root/name)
                    entry = zipfile.ZipInfo(name, (1980,1,1,0,0,0))
                    entry.compress_type = zipfile.ZIP_DEFLATED
                    entry.external_attr = 0o100644 << 16
                    digest, count = hashlib.sha256(), 0
                    with (root/name).open('rb') as incoming, archive.open(entry, 'w', force_zip64=True) as outgoing:
                        bundle.require(bundle.signature(os.fstat(incoming.fileno())) == bundle.signature(before), 'bundle_input_changed')
                        while chunk := incoming.read(bundle.BLOCK):
                            count += len(chunk)
                            bundle.require(count <= row['bytes'], 'bundle_input_changed')
                            digest.update(chunk)
                            outgoing.write(chunk)
                        bundle.require(bundle.signature(os.fstat(incoming.fileno())) == bundle.signature(before), 'bundle_input_changed')
                    bundle.require(bundle.signature(bundle.no_link(root/name)) == bundle.signature(before)
                        and count == row['bytes'] and digest.hexdigest() == row['sha256'], 'bundle_integrity_mismatch')
        with destination.open('r+b') as stream:
            os.fsync(stream.fileno())
        with zipfile.ZipFile(destination) as archive:
            bundle.require(len(archive.infolist()) == len(files)+len(directories)
                and set(archive.namelist()) == set(files) | {name+'/' for name in directories}, 'bundle_zip_invalid')
        return _row(destination, limit=bundle.MAX_TOTAL)


def build(payload_dir, embed_zip, runtime_lock, entry_console, entry_windowed, output, *, zip_output=None,
          doctor_code=None, guide=None, version=bundle.VERSION_B2, channel='local-candidate'):
    payload, output = bundle.absolute(payload_dir), bundle.absolute(output)
    embed_zip, runtime_lock = bundle.absolute(embed_zip), bundle.absolute(runtime_lock)
    console, windowed = bundle.absolute(entry_console), bundle.absolute(entry_windowed)
    doctor = bundle.absolute(doctor_code or ROOT/'scripts/local_doctor.py')
    guide = bundle.absolute(guide or ROOT/'AGENT_INSTALL.md')
    for source in (payload, embed_zip, runtime_lock, console, windowed, doctor, guide, ROOT):
        bundle.disjoint(source, output)
    if zip_output is not None:
        zip_output = bundle.absolute(zip_output)
        bundle.disjoint(output, zip_output, payload)
        bundle.absent(zip_output)
    bundle.absent(output)
    with bundle.guard_path(payload), bundle.guard_path(output.parent):
        payload_files, payload_dirs = _payload_inventory(payload)
        output.mkdir()
        bundle.make_directories(output, {'payload'} | {'payload/'+name for name in payload_dirs})
        for name, row in sorted(payload_files.items()):
            copy_verified(payload/name, output/'payload'/name, row)
        bundle.make_directories(output, {'manager','manager/python','manager/code'})
        _manager_python(embed_zip, runtime_lock, output/'manager/python')
        manager_sources = {'customer_bundle.py':Path(__file__).with_name('customer_bundle.py'),
                'customer_install.py':Path(__file__).with_name('customer_install.py'), 'local_doctor.py':doctor}
        if version == bundle.VERSION_B2:
            manager_sources.update({Path(name).name:Path(__file__).with_name(Path(name).name) for name in bundle.UPGRADE_MANAGER})
        for name, source in manager_sources.items():
            copy_verified(source, output/'manager/code'/name, _row(source))
        bundle.make_directories(output, {'entry'})
        for source, name in ((console,'entry/CustomerLauncher.exe'), (windowed,'entry/CustomerLauncherW.exe'),
                (console,'CustomerSetup.exe'), (guide,'AGENT_INSTALL.md')):
            copy_verified(source, output/name, _row(source))
        paths, directories = bundle.scan_tree(output)
        rows = []
        for name in sorted(paths):
            component = ('product' if name.startswith('payload/') else 'manager' if name.startswith('manager/')
                else 'entry' if name.startswith('entry/') else 'bootstrap' if name == 'CustomerSetup.exe' else 'guide')
            rows.append({'path':name, **_row(output/name), 'component':component})
        manifest = {'schema':1,'kind':'customer_bundle','version':version,'channel':channel,'state_compatibility':1,
            'files':rows,'directories':sorted(directories),'product_metadata':['payload/payload-manifest.json'],
            'ready_to_export':False,'trust':'operator_selected_digest_integrity_not_publisher_authentication'}
        bundle.inventory(manifest)
        raw = bundle.encode(manifest)
        bundle.require(len(raw) <= bundle.MAX_MANIFEST, 'bundle_capacity')
        bundle.write_new(output/'bundle.manifest.json', raw)
        digest = hashlib.sha256(raw).hexdigest()
        bundle.verify_bundle(output, digest, deep=False)
        archive = None
        if zip_output is not None:
            zip_rows = {row['path']:row for row in rows}
            zip_rows['bundle.manifest.json'] = {'bytes':len(raw),'sha256':digest}
            archive = create_zip(output, zip_output, zip_rows, directories)
        return {'ok':True,'bundle_manifest_sha256':digest,'files':len(rows),'directories':len(directories),
            'bytes':sum(row['bytes'] for row in rows),'zip':archive,'ready_to_export':False}


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise bundle.BundleError('bundle_arguments_invalid')


def main(argv=None):
    parser = _Parser(description=__doc__)
    for name in ('payload-dir','embed-zip','runtime-lock','entry-console','entry-windowed','output'):
        parser.add_argument('--'+name, required=True)
    for name in ('zip-output','doctor-code','guide'):
        parser.add_argument('--'+name)
    try:
        args = parser.parse_args(argv)
        result = build(**vars(args))
    except (OSError, ValueError, TypeError, KeyError, zipfile.BadZipFile):
        result = {'ok':False,'code':'bundle_build_failed_partial_output_retained'}
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0 if result['ok'] else 78


if __name__ == '__main__':
    raise SystemExit(main())
