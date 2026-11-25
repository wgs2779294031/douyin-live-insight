# Desktop helper and guided distribution

These tools build the Windows desktop helper from source and wrap a trusted
installation bundle with a beginner guide. They do not run the installation
bundle, collect live data, load a model, or change its internal version/channel.
The desktop source is retained with only the two bundle hashes changed into
build-time placeholders. Historical machine paths, compiled executables, old
test receipts and private working directories are not build inputs.

Prerequisites: Python 3.11 or newer, Windows x64, and the existing .NET Framework
4.x `csc.exe` plus its framework assemblies. Use a trusted, separately generated
bundle ZIP; verify its expected SHA-256 through a trusted channel. A checksum
by itself is not a publisher signature. The builders preserve all original
manifest bytes and keep `customer-local-11B2` / `local-candidate` semantics when
those are present in the input bundle.

## Build and verify the desktop helper

Use fresh output and scratch paths outside this source directory. Short paths
help the existing native path-limit checks. The optional `--compiler` parameter
selects an explicit framework compiler; otherwise the system Framework64
compiler is used. Existing files and directories are refused.

```powershell
python -B packaging/build_desktop_client.py `
  --bundle <trusted-bundle.zip> `
  --bundle-sha256 <expected-64-lowercase-hex-digits> `
  --output-dir <new-external-build-directory> `
  --run-harness `
  --harness-root <new-short-proof-directory>
```

The last path's final component must start with `proof-`. The build computes
the manifest and `CustomerSetup.exe` hashes from the validated ZIP, binds those
values into a copy of `ClientBackend.cs`, and compiles `安装直播监控.exe`. It writes
`build-receipt.json` containing the exact binary/source hashes and this run's
harness results. Bound source, compiler logs and test artifacts stay in the
external output paths. The harness uses synthetic owners/process results and
redirected shortcut folders under the fresh scratch path; it does not run
`CustomerSetup.exe` or create shortcuts on the real desktop. Its final UI check
renders forms to local images without showing them. It does not establish real
installation, live-platform, model, long-running or clean-machine acceptance.

## Create the guided ZIP

```powershell
python -B packaging/package_with_guide.py `
  --bundle <same-trusted-bundle.zip> `
  --bundle-sha256 <same-expected-sha256> `
  --guide packaging/guide.template.html `
  --client <external-build-directory>/安装直播监控.exe `
  --receipt <external-build-directory>/build-receipt.json `
  --output <new-guided-distribution.zip>
```

Packaging requires a passing current harness receipt bound to the same bundle
and helper bytes. It binds the guide's manifest placeholder and writes a new
ZIP containing the guide, helper and `安装文件/` directory. A verification JSON is
written beside the ZIP (override with `--output-receipt`). Existing paths are
never replaced. Failed partial outputs remain for inspection.

Every member is checked against the original manifest, including size, SHA-256,
directory inventory, CRC and attributes. Duplicate/case-colliding members,
Windows device/ADS paths, traversal, symlinks, encrypted or unsupported ZIP
entries and unlisted files are rejected. The tools never extract the bundle.
JSON command output uses ASCII escaping so Windows console code pages do not
corrupt it.

## Synthetic tests

```powershell
python -B -m unittest discover -s packaging/tests -v
```

Set `RUN_DESKTOP_HARNESS=1` on Windows to include native compilation and the
seven-case isolated C# harness. Set `PACKAGING_TEST_ROOT` to an existing short
external directory to retain the native build, redirected scratch artifacts and
archive receipt; per-case ZIP fixtures are temporary. These tests use tiny
synthetic bundles, never the full runtime/model package. They cover valid
archives, bad trust hashes, member corruption, traversal/ADS, duplicate or
symlink entries, receipt binding, unrun harnesses and overwrite refusal.

## Source provenance

The desktop and guide sources were adapted from the project's previously
tested Windows distribution helper. Original source SHA-256 values:

| Source | Original SHA-256 |
| --- | --- |
| `ClientBackend.cs` | `6eb9d52409423ad2f29a22df0eda87b696d5af8ce54c4615e0c57cff93ddeff1` |
| `DesktopClient.cs` | `460df9679c539f10a87dc648c7100cd41fb7b9e231e3b9dfbce076a00a0d2c43` |
| `IsolatedHarness.cs` | `50da991f064138afd04e289a4ae899a9a96fe66d6bfdf2f8be4eadbd4af3464c` |
| Guide HTML | `05b2a1df9f295160ea5cf25445847ae623a64446ea205b085d6d37f4fa2002a6` |

The outer wrapper is a reusable rewrite of the former one-off archive wrapper;
its new tests do not inherit acceptance claims from a previous package. Public
release notes must state what was actually verified for that release.
