"""Wrap a trusted bundle, freshly bound desktop helper and guide in a new ZIP."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path
import zipfile

from bundle_inputs import (BLOCK, PackagingError, ValidatedBundle, digest_file, disjoint, fresh_path,
                           json_bytes, ordinary, read_file, read_json_bytes, require)

ROOT = Path(__file__).absolute().parent
PREFIX = "安装文件/"
GUIDE_NAME = "先读我-安装指南.html"
CLIENT_NAME = "安装直播监控.exe"
MANIFEST_TOKEN = "__BUNDLE_MANIFEST_SHA256__"


def package(bundle, bundle_sha256, guide, client, receipt, output, *, output_receipt=None):
    bundle_path, guide_path, client_path, receipt_path = map(ordinary, (bundle, guide, client, receipt))
    output = fresh_path(output)
    require(output.suffix.lower() == ".zip", "zip_output_required")
    output_receipt = fresh_path(output_receipt or output.with_suffix(".verification.json"))
    disjoint(output, output_receipt)
    disjoint(ROOT, output)
    disjoint(ROOT, output_receipt)
    for source in (bundle_path, guide_path, client_path, receipt_path):
        disjoint(source, output)
        disjoint(source, output_receipt)
    guide_raw, client_raw = read_file(guide_path), read_file(client_path, limit=64 * BLOCK)
    require(client_raw.startswith(b"MZ"), "client_not_windows_executable")
    build_receipt = read_json_bytes(read_file(receipt_path))
    require(isinstance(build_receipt, dict) and build_receipt.get("schema") == 1
            and build_receipt.get("kind") == "desktop_client_build"
            and build_receipt.get("ok") is True, "client_receipt_invalid")
    client_facts = build_receipt.get("client")
    require(isinstance(client_facts, dict) and client_facts.get("filename") == CLIENT_NAME
            and client_facts.get("bytes") == len(client_raw)
            and client_facts.get("sha256") == hashlib.sha256(client_raw).hexdigest(), "client_receipt_mismatch")
    harness = build_receipt.get("harness")
    require(isinstance(harness, dict) and harness.get("requested") is True and harness.get("ok") is True
            and isinstance(harness.get("cases"), list) and len(harness["cases"]) > 0
            and harness.get("genuine_install") is False, "client_harness_not_passed")
    try:
        template = guide_raw.decode("utf-8-sig")
    except UnicodeError as error:
        raise PackagingError("guide_encoding_invalid") from error
    require(template.count(MANIFEST_TOKEN) == 1 and '<html lang="zh-CN">' in template,
            "guide_template_invalid")
    with ValidatedBundle(bundle_path, bundle_sha256) as archive:
        require(build_receipt.get("bundle_sha256") == bundle_sha256
                and build_receipt.get("bundle_manifest_sha256") == archive.manifest_sha256
                and build_receipt.get("setup_sha256") == archive.setup_sha256
                and build_receipt.get("bundle_version") == archive.manifest["version"]
                and build_receipt.get("bundle_channel") == archive.manifest["channel"], "client_bundle_mismatch")
        rendered = template.replace(MANIFEST_TOKEN, archive.manifest_sha256).encode("utf-8")
        extras = {GUIDE_NAME: rendered, CLIENT_NAME: client_raw}
        expected = {PREFIX + name: row for name, row in archive.expected_files.items()}
        for name, raw in extras.items():
            expected[name] = {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
        wanted_dirs = {PREFIX} | {PREFIX + name + "/" for name in archive.directories}
        # Exclusive creation deliberately leaves a partial artifact for inspection
        # on failure. Existing output files or receipts are never replaced.
        with output.open("xb") as output_handle:
            with zipfile.ZipFile(output_handle, "w", zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as outgoing:
                for name, raw in extras.items():
                    outgoing.writestr(name, raw)
                outgoing.writestr(PREFIX, b"")
                for original in archive.infos:
                    new_entry = copy.copy(original)
                    new_entry.filename = PREFIX + original.filename
                    new_entry.orig_filename = new_entry.filename
                    if original.is_dir():
                        outgoing.writestr(new_entry, b"")
                        continue
                    checksum = hashlib.sha256()
                    count = 0
                    with archive.zip.open(original) as incoming, outgoing.open(new_entry, "w", force_zip64=True) as member:
                        for block in iter(lambda: incoming.read(BLOCK), b""):
                            count += len(block)
                            checksum.update(block)
                            member.write(block)
                    row = archive.expected_files[original.filename]
                    require(count == row["bytes"] and checksum.hexdigest() == row["sha256"], "source_member_changed")
        archive.stable()
        with zipfile.ZipFile(output) as final:
            infos = final.infolist()
            names = [item.filename for item in infos]
            wanted = set(expected) | wanted_dirs
            require(len(names) == len(set(name.casefold() for name in names)) == len(wanted)
                    and set(names) == wanted, "distribution_inventory_mismatch")
            for name, row in expected.items():
                require(final.getinfo(name).file_size == row["bytes"], "distribution_size_mismatch")
                checksum = hashlib.sha256()
                with final.open(name) as member:
                    for block in iter(lambda: member.read(BLOCK), b""):
                        checksum.update(block)
                require(checksum.hexdigest() == row["sha256"], "distribution_sha256_mismatch")
            for original in archive.infos:
                copied = final.getinfo(PREFIX + original.filename)
                require(copied.CRC == original.CRC and copied.external_attr == original.external_attr,
                        "distribution_metadata_mismatch")
        result = {"schema": 1, "kind": "guided_distribution", "ok": True,
                  "bundle_sha256": bundle_sha256, "bundle_manifest_sha256": archive.manifest_sha256,
                  "bundle_version": archive.manifest["version"], "bundle_channel": archive.manifest["channel"],
                  "client_sha256": client_facts["sha256"], "guide_sha256": hashlib.sha256(rendered).hexdigest(),
                  "desktop_harness_cases": harness["cases"],
                  "distribution": output.name, "distribution_bytes": output.stat().st_size,
                  "distribution_sha256": digest_file(output),
                  "installation_files_verified": len(archive.expected_files),
                  "original_directories_preserved": len(archive.directories),
                  "all_file_sha256_verified": True, "all_crc_and_attributes_preserved": True,
                  "new_installation_performed": False, "model_or_platform_called": False}
    with output_receipt.open("xb") as stream:
        stream.write(json_bytes(result))
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("bundle", "bundle-sha256", "guide", "client", "receipt", "output"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--output-receipt", help="Optional new verification JSON, defaults beside the output ZIP")
    args = parser.parse_args(argv)
    try:
        result = package(**vars(args))
    except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile) as error:
        result = {"ok": False, "code": str(error) if isinstance(error, PackagingError) else "distribution_build_failed"}
    print(json.dumps(result, ensure_ascii=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
