"""Build a Windows desktop helper bound to one explicitly trusted bundle."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile

from bundle_inputs import (PackagingError, ValidatedBundle, digest_file, disjoint,
                           fresh_path, json_bytes, ordinary, read_file, read_json_bytes, require)

ROOT = Path(__file__).absolute().parent
SOURCE = ROOT / "desktop-client"
CLIENT_NAME = "安装直播监控.exe"
MANIFEST_TOKEN = "__BUNDLE_MANIFEST_SHA256__"
SETUP_TOKEN = "__CUSTOMER_SETUP_SHA256__"
REFERENCES = ("mscorlib.dll", "System.dll", "System.Core.dll", "System.Drawing.dll",
              "System.Windows.Forms.dll", "System.Web.Extensions.dll",
              "System.IO.Compression.dll", "System.IO.Compression.FileSystem.dll")


def build(bundle, bundle_sha256, output_dir, *, compiler=None, run_harness=False, harness_root=None):
    require(os.name == "nt", "windows_required")
    output = fresh_path(output_dir)
    disjoint(ROOT, output)
    bundle_path = ordinary(bundle)
    disjoint(bundle_path, output)
    # Windows component-store files legitimately have hard links. They are
    # read-only compiler inputs; junctions and symlinks remain forbidden.
    compiler = ordinary(compiler or Path(os.environ.get("SystemRoot", "C:/Windows")) /
                        "Microsoft.NET/Framework64/v4.0.30319/csc.exe", allow_hardlinks=True)
    references = [ordinary(compiler.parent / name, allow_hardlinks=True) for name in REFERENCES]
    raw_sources = {name: read_file(SOURCE / name) for name in
                   ("ClientBackend.cs", "DesktopClient.cs", "IsolatedHarness.cs")}
    backend = raw_sources["ClientBackend.cs"].decode("utf-8-sig")
    require(backend.count(MANIFEST_TOKEN) == backend.count(SETUP_TOKEN) == 1, "binding_template_invalid")
    with ValidatedBundle(bundle_path, bundle_sha256) as archive:
        manifest_sha, setup_sha = archive.manifest_sha256, archive.setup_sha256
        manifest_version, manifest_channel = archive.manifest["version"], archive.manifest["channel"]
    bound = backend.replace(MANIFEST_TOKEN, manifest_sha).replace(SETUP_TOKEN, setup_sha).encode("utf-8")
    receipt = {"schema": 1, "kind": "desktop_client_build", "ok": False,
               "bundle_sha256": bundle_sha256, "bundle_manifest_sha256": manifest_sha,
               "setup_sha256": setup_sha, "bundle_version": manifest_version,
               "bundle_channel": manifest_channel,
               "compiler": {"name": compiler.name, "sha256": digest_file(compiler, allow_hardlinks=True)},
               "sources": {name: {"template_sha256": hashlib.sha256(raw).hexdigest()}
                           for name, raw in raw_sources.items()},
               "harness": {"requested": bool(run_harness), "ok": False, "cases": []},
               "actual_installation_performed": False, "model_or_platform_called": False}
    receipt["sources"]["ClientBackend.cs"]["bound_sha256"] = hashlib.sha256(bound).hexdigest()
    output.mkdir()
    sources_out = output / "bound-source"
    sources_out.mkdir()
    for name, raw in raw_sources.items():
        (sources_out / name).write_bytes(bound if name == "ClientBackend.cs" else raw)
    env = {key: value for key, value in os.environ.items()
           if key.upper() in {"SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP"}}
    env["PATH"] = str(Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    def compile_one(name, target, main, filenames):
        executable = output / name
        args = [str(compiler), "/nologo", "/noconfig", "/nostdlib+", "/optimize+", "/debug-", "/codepage:65001",
                "/platform:x64", "/target:" + target, "/main:" + main, "/out:" + str(executable),
                *("/reference:" + str(path) for path in references),
                *(str(sources_out / filename) for filename in filenames)]
        result = subprocess.run(args, cwd=output, env=env, capture_output=True, timeout=90, creationflags=flags)
        (output / (name + ".stdout.txt")).write_bytes(result.stdout)
        (output / (name + ".stderr.txt")).write_bytes(result.stderr)
        require(result.returncode == 0, "desktop_compile_failed")
        return executable

    try:
        client = compile_one(CLIENT_NAME, "winexe", "LiveMonitorDesktop.Program",
                             ("ClientBackend.cs", "DesktopClient.cs"))
        receipt["client"] = {"filename": CLIENT_NAME, "bytes": client.stat().st_size, "sha256": digest_file(client)}
        if run_harness:
            harness = compile_one("IsolatedHarness.exe", "exe", "LiveMonitorDesktop.Harness",
                                  ("ClientBackend.cs", "DesktopClient.cs", "IsolatedHarness.cs"))
            scratch = fresh_path(harness_root or output / "proof-desktop")
            require(scratch.name.startswith("proof-"), "harness_root_name_invalid")
            disjoint(SOURCE, scratch)
            # The harness only creates redirected shortcuts and synthetic owners
            # under this fresh scratch root. It never runs CustomerSetup.exe.
            result = subprocess.run([str(harness), str(scratch), str(bundle_path)], cwd=output,
                                    capture_output=True, timeout=120, creationflags=flags)
            (output / "harness.stdout.txt").write_bytes(result.stdout)
            (output / "harness.stderr.txt").write_bytes(result.stderr)
            parsed = read_json_bytes(result.stdout)
            require(isinstance(parsed, dict) and isinstance(parsed.get("cases"), list), "harness_result_invalid")
            receipt["harness"] = {"requested": True, "returncode": result.returncode,
                                  "ok": parsed.get("ok") is True, "cases": parsed["cases"],
                                  "genuine_install": parsed.get("genuine_install") is True}
            (output / "harness-result.json").write_bytes(json_bytes(parsed))
            require(result.returncode == 0 and parsed.get("ok") is True
                    and parsed.get("genuine_install") is False, "desktop_harness_failed")
        receipt["ok"] = True
    except BaseException as error:
        receipt["error"] = str(error) if isinstance(error, PackagingError) else "desktop_build_interrupted_or_failed"
        raise
    finally:
        (output / "build-receipt.json").write_bytes(json_bytes(receipt))
    return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", required=True)
    parser.add_argument("--bundle-sha256", required=True, help="Expected SHA-256 obtained from a trusted source")
    parser.add_argument("--output-dir", required=True, help="A new external directory; existing paths are refused")
    parser.add_argument("--compiler", help="Explicit .NET Framework csc.exe, otherwise the system Framework64 compiler")
    parser.add_argument("--run-harness", action="store_true")
    parser.add_argument("--harness-root", help="Optional new short path whose final component starts with proof-")
    args = parser.parse_args(argv)
    try:
        receipt = build(args.bundle, args.bundle_sha256, args.output_dir, compiler=args.compiler,
                        run_harness=args.run_harness, harness_root=args.harness_root)
        result = {"ok": True, "client": receipt["client"], "harness": receipt["harness"]}
    except (OSError, ValueError, KeyError, zipfile.BadZipFile, subprocess.SubprocessError) as error:
        result = {"ok": False, "code": str(error) if isinstance(error, PackagingError) else "desktop_build_failed"}
    print(json.dumps(result, ensure_ascii=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
