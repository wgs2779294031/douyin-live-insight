"""Synthetic ZIP tests. Set RUN_DESKTOP_HARNESS=1 on Windows for native checks."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
import uuid
import warnings
import zipfile

ROOT = Path(__file__).absolute().parents[1]
sys.path.insert(0, str(ROOT))
from bundle_inputs import PackagingError, ValidatedBundle, digest_file, json_bytes
from package_with_guide import package


def fixture_bundle(path, *, extra=None, duplicate=False, symlink=False, wrong_content=False, mutate_rows=None):
    payload = {"CustomerSetup.exe": b"MZ synthetic setup; never executed", "payload/app/readme.txt": b"synthetic payload"}
    if extra:
        payload.update(extra)
    def component(name):
        return ("bootstrap" if name == "CustomerSetup.exe" else "guide" if name == "AGENT_INSTALL.md"
                else "manager" if name.startswith("manager/") else "entry" if name.startswith("entry/")
                else "product")
    rows = [{"path": name, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(), "component": component(name)}
            for name, raw in payload.items()]
    if mutate_rows is not None:
        mutate_rows(rows)
    directories = sorted({"/".join(name.split("/")[:index]) for name in payload
                          for index in range(1, len(name.split("/")))})
    manifest = {"schema": 1, "kind": "customer_bundle", "version": "customer-local-11B2",
                "channel": "local-candidate", "state_compatibility": 1,
                "files": rows, "directories": directories, "product_metadata": []}
    raw_manifest = json_bytes(manifest)
    with zipfile.ZipFile(path, "x", zipfile.ZIP_DEFLATED) as archive:
        for directory in directories:
            archive.writestr(directory + "/", b"")
        for name, raw in payload.items():
            archive.writestr(name, b"tampered payload" if wrong_content and name.endswith("readme.txt") else raw)
        archive.writestr("bundle.manifest.json", raw_manifest)
        if duplicate:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                archive.writestr("CustomerSetup.exe", payload["CustomerSetup.exe"])
        if symlink:
            info = zipfile.ZipInfo("linked")
            info.create_system = 3
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, b"payload/readme.txt")
    return {"bundle_sha256": digest_file(path), "bundle_manifest_sha256": hashlib.sha256(raw_manifest).hexdigest(),
            "setup_sha256": rows[0]["sha256"], "manifest_raw": raw_manifest,
            "bundle_version": manifest["version"], "bundle_channel": manifest["channel"]}


class PackagingTests(unittest.TestCase):
    def setUp(self):
        base = os.environ.get("PACKAGING_TEST_ROOT")
        if base:
            Path(base).mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="pkg-", dir=base)
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.bundle = self.work / "bundle.zip"
        self.facts = fixture_bundle(self.bundle)
        self.client = self.work / "client.exe"
        self.client.write_bytes(b"MZ synthetic desktop helper; never executed")
        self.receipt = self.work / "build-receipt.json"
        self.receipt_data = {"schema": 1, "kind": "desktop_client_build", "ok": True,
                             **{key: value for key, value in self.facts.items() if key != "manifest_raw"},
                             "client": {"filename": "安装直播监控.exe", "bytes": self.client.stat().st_size,
                                        "sha256": digest_file(self.client)},
                             "harness": {"requested": True, "ok": True,
                                         "cases": ["synthetic_receipt_for_archive_unit_test"], "genuine_install": False}}
        self.save_receipt()
        self.guide = ROOT / "guide.template.html"
        self.output = self.work / "guided.zip"

    def save_receipt(self):
        self.receipt.write_bytes(json_bytes(self.receipt_data))

    def call_package(self, **override):
        values = dict(bundle=self.bundle, bundle_sha256=self.facts["bundle_sha256"], guide=self.guide,
                      client=self.client, receipt=self.receipt, output=self.output)
        values.update(override)
        return package(**values)

    def reject(self, code, **override):
        with self.assertRaisesRegex(PackagingError, "^" + code + "$"):
            self.call_package(**override)
        self.assertFalse(self.output.exists())

    def test_normal_small_package_preserves_manifest_version_channel_and_contents(self):
        result = self.call_package()
        self.assertTrue(result["ok"])
        self.assertEqual(result["installation_files_verified"], 3)
        self.assertEqual(result["bundle_version"], "customer-local-11B2")
        self.assertEqual(result["bundle_channel"], "local-candidate")
        with zipfile.ZipFile(self.output) as archive:
            self.assertEqual(archive.read("安装文件/bundle.manifest.json"), self.facts["manifest_raw"])
            self.assertEqual(archive.read("安装文件/payload/app/readme.txt"), b"synthetic payload")
            guide = archive.read("先读我-安装指南.html")
            self.assertIn(self.facts["bundle_manifest_sha256"].encode("ascii"), guide)
            self.assertNotIn(b"__BUNDLE_MANIFEST_SHA256__", guide)
        self.assertFalse(result["new_installation_performed"])

    def test_wrong_trusted_bundle_sha_is_refused(self):
        self.reject("bundle_sha256_mismatch", bundle_sha256="0" * 64)

    def test_formal_component_fields_are_retained_for_all_path_groups(self):
        bundle = self.work / "all-components.zip"
        facts = fixture_bundle(bundle, extra={"manager/code/customer_bundle.py": b"# synthetic manager source\n",
                                              "entry/CustomerLauncher.exe": b"MZ synthetic entry; never executed",
                                              "AGENT_INSTALL.md": b"Synthetic guide\n"})
        with ValidatedBundle(bundle, facts["bundle_sha256"]) as checked:
            observed = {row["component"] for name, row in checked.expected_files.items()
                        if name != "bundle.manifest.json"}
            self.assertEqual(observed, {"product", "manager", "entry", "bootstrap", "guide"})

    def test_missing_component_field_is_refused(self):
        bundle = self.work / "missing-component.zip"
        facts = fixture_bundle(bundle, mutate_rows=lambda rows: rows[0].pop("component"))
        self.reject("invalid_manifest_row", bundle=bundle, bundle_sha256=facts["bundle_sha256"])

    def test_illegal_component_is_refused(self):
        bundle = self.work / "illegal-component.zip"
        facts = fixture_bundle(bundle, mutate_rows=lambda rows: rows[0].update(component="unrecognized"))
        self.reject("invalid_manifest_component", bundle=bundle, bundle_sha256=facts["bundle_sha256"])

    def test_component_for_wrong_path_is_refused(self):
        bundle = self.work / "wrong-path-component.zip"
        facts = fixture_bundle(bundle, mutate_rows=lambda rows: rows[0].update(component="manager"))
        self.reject("invalid_manifest_component", bundle=bundle, bundle_sha256=facts["bundle_sha256"])

    def test_extra_manifest_row_field_is_refused(self):
        bundle = self.work / "extra-row-field.zip"
        facts = fixture_bundle(bundle, mutate_rows=lambda rows: rows[0].update(extra="not permitted"))
        self.reject("invalid_manifest_row", bundle=bundle, bundle_sha256=facts["bundle_sha256"])

    def test_member_path_traversal_is_refused(self):
        bad = self.work / "traversal.zip"
        facts = fixture_bundle(bad, extra={"../escape.txt": b"no"})
        self.reject("unsafe_member_path", bundle=bad, bundle_sha256=facts["bundle_sha256"])

    def test_windows_ads_member_is_refused(self):
        bad = self.work / "ads.zip"
        facts = fixture_bundle(bad, extra={"payload/file:stream": b"no"})
        self.reject("unsafe_member_path", bundle=bad, bundle_sha256=facts["bundle_sha256"])

    def test_duplicate_zip_member_is_refused(self):
        bad = self.work / "duplicate.zip"
        facts = fixture_bundle(bad, duplicate=True)
        self.reject("duplicate_member", bundle=bad, bundle_sha256=facts["bundle_sha256"])

    def test_zip_symlink_is_refused(self):
        bad = self.work / "symlink.zip"
        facts = fixture_bundle(bad, symlink=True)
        self.reject("linked_or_special_member", bundle=bad, bundle_sha256=facts["bundle_sha256"])

    def test_member_bytes_must_match_manifest(self):
        bad = self.work / "tampered.zip"
        facts = fixture_bundle(bad, wrong_content=True)
        with self.assertRaisesRegex(PackagingError, "member_(size|sha256)_mismatch"):
            self.call_package(bundle=bad, bundle_sha256=facts["bundle_sha256"])
        self.assertFalse(self.output.exists())

    def test_client_receipt_mismatch_is_refused(self):
        self.receipt_data["client"]["sha256"] = "0" * 64
        self.save_receipt()
        self.reject("client_receipt_mismatch")

    def test_receipt_for_other_bundle_is_refused(self):
        self.receipt_data["bundle_manifest_sha256"] = "0" * 64
        self.save_receipt()
        self.reject("client_bundle_mismatch")

    def test_unrun_harness_is_not_reported_as_passed(self):
        self.receipt_data["harness"] = {"requested": False, "ok": False, "cases": []}
        self.save_receipt()
        self.reject("client_harness_not_passed")

    def test_existing_output_is_not_overwritten(self):
        self.output.write_bytes(b"keep existing output")
        with self.assertRaisesRegex(PackagingError, "output_already_exists"):
            self.call_package()
        self.assertEqual(self.output.read_bytes(), b"keep existing output")

    def test_existing_verification_receipt_is_not_overwritten(self):
        output_receipt = self.output.with_suffix(".verification.json")
        output_receipt.write_bytes(b"keep existing receipt")
        self.reject("output_already_exists")
        self.assertEqual(output_receipt.read_bytes(), b"keep existing receipt")

    def test_cli_failure_stdout_is_ascii_json(self):
        command = [sys.executable, "-B", str(ROOT / "package_with_guide.py"),
                   "--bundle", str(self.bundle), "--bundle-sha256", "0" * 64,
                   "--guide", str(self.guide), "--client", str(self.client),
                   "--receipt", str(self.receipt), "--output", str(self.output)]
        result = subprocess.run(command, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout.decode("ascii"))["code"], "bundle_sha256_mismatch")

    @unittest.skipUnless(os.name == "nt" and os.environ.get("RUN_DESKTOP_HARNESS") == "1",
                         "set RUN_DESKTOP_HARNESS=1 on Windows to compile and run the isolated harness")
    def test_native_desktop_build_harness_and_archive_with_synthetic_bundle(self):
        from build_desktop_client import build
        evidence = Path(os.environ.get("PACKAGING_TEST_ROOT", self.work))
        token = uuid.uuid4().hex[:8]
        output = evidence / ("desktop-" + token)
        scratch = evidence / ("proof-" + token)
        result = build(self.bundle, self.facts["bundle_sha256"], output, run_harness=True, harness_root=scratch)
        self.assertTrue(result["ok"])
        self.assertTrue(result["harness"]["ok"])
        self.assertEqual(len(result["harness"]["cases"]), 7)
        guided = package(self.bundle, self.facts["bundle_sha256"], self.guide,
                         output / result["client"]["filename"], output / "build-receipt.json", output / "guided.zip")
        self.assertTrue(guided["ok"])
        self.assertFalse(guided["new_installation_performed"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
