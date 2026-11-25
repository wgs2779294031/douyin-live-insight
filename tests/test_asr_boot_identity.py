"""Boot credentials use fake registry values only; no real system state reads."""
import hashlib
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]
from scripts import asr_protocol as protocol

MACHINE = "11111111-2222-3333-4444-555555555555"


class Key:
    def __init__(self, name, calls):
        self.name, self.calls = name, calls

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.calls.append(("close", self.name))


@pytest.fixture
def registry(monkeypatch):
    calls = []
    values = {"BootId": (10, 4), "MachineGuid": (MACHINE, 1)}
    def open_key(hive, name, reserved, access):
        assert hive == "fake-hklm" and reserved == 0 and access == 0x101
        assert name in {r"SYSTEM\CurrentControlSet\Control\Session Manager\Memory Management\PrefetchParameters",
                        r"SOFTWARE\Microsoft\Cryptography"}
        calls.append(("open", name))
        return Key(name, calls)
    def query(key, name):
        assert name == ("BootId" if key.name.startswith("SYSTEM") else "MachineGuid")
        calls.append(("query", name))
        value = values[name]
        if isinstance(value, Exception):
            raise value
        return value
    fake = SimpleNamespace(HKEY_LOCAL_MACHINE="fake-hklm", KEY_QUERY_VALUE=1, KEY_WOW64_64KEY=0x100,
                           REG_DWORD=4, REG_SZ=1, OpenKey=open_key, QueryValueEx=query)
    monkeypatch.setitem(sys.modules, "winreg", fake)
    return values, calls


def test_boot_credential_uses_only_two_readonly_keys_and_hashes_installation(registry):
    values, calls = registry
    first = protocol.boot_identity()
    assert first == {"schema": 1, "kind": "windows_boot", "installation": hashlib.sha256(MACHINE.encode()).hexdigest(), "counter": 10}
    assert protocol.valid_boot_identity(first) and MACHINE not in str(first)
    assert [entry[0] for entry in calls] == ["open", "query", "close", "open", "query", "close"]
    values["MachineGuid"] = ("{" + MACHINE.upper() + "}", 1)
    assert protocol.boot_identity() == first


@pytest.mark.parametrize("name,value", [
    ("BootId", (True, 4)), ("BootId", (-1, 4)), ("BootId", (2**32, 4)), ("BootId", (10, 1)),
    ("BootId", OSError("fake denied")), ("MachineGuid", ("invalid", 1)),
    ("MachineGuid", ("00000000-0000-0000-0000-000000000000", 1)),
    ("MachineGuid", (MACHINE, 4)), ("MachineGuid", OSError("fake missing")),
], ids=["boolean-counter", "negative-counter", "overflow-counter", "wrong-counter-kind", "counter-denied",
        "invalid-guid", "empty-guid", "wrong-guid-kind", "guid-missing"])
def test_unavailable_or_malformed_boot_evidence_never_has_a_clock_fallback(registry, name, value):
    registry[0][name] = value
    assert protocol.boot_identity() is None


@pytest.mark.parametrize("patch,expected", [
    ({"counter": 11}, True), ({"counter": 10}, False), ({"counter": 9}, False),
    ({"installation": "b" * 64, "counter": 11}, False), ({"counter": True}, False),
    ({"counter": 2**32}, False), ({"kind": "uptime"}, False),
    ({"installation": "A" * 64}, False), ({"extra": "bad"}, False),
], ids=["advanced", "same", "backwards", "other-installation", "boolean", "overflow", "wrong-source", "bad-digest", "extra"])
def test_only_strictly_advanced_same_installation_boot_proves_old_tree_ended(patch, expected):
    old = {"schema": 1, "kind": "windows_boot", "installation": "a" * 64, "counter": 10}
    assert protocol.boot_advanced(old, dict(old, **patch)) is expected
    assert protocol.boot_advanced(None, dict(old, **patch)) is False
    assert protocol.boot_advanced(old, None) is False
