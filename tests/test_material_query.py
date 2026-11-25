"""Read-only material pages, using only newly generated local fixture bytes."""
from contextlib import contextmanager
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import material_common as common
from scripts import material_query as query


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8") + b"\n"


def row(index=0, *, view="canonical", stream="speech", tag="房间甲", ts=None, text=None):
    value = {"stream": stream, "platform": "douyin", "tag": tag,
             "ts": index if ts is None else ts, "text": str(index) if text is None else text}
    if view == "canonical":
        value.update(time="2026-11-16 00:00:00", dur=1, seg="seg_0001", off=index,
                     view="canonical", redaction={"policy": "none", "applied": False},
                     source={"result_ref": "audio_房间甲_2026-11-16/.results/seg_0001.json",
                             "source_id": "a" * 64, "generation": "b" * 32,
                             "result_sha256": "c" * 64, "json_pointer": f"/rows/{index}"})
    return value


def group(tag="房间甲", date="2026-11-16", *, group_id="group1"):
    return {"id": group_id, "platform": "douyin", "tag": tag, "date": date,
            "from": f"{date}T00:00:00+08:00", "until_exclusive": "2026-11-17T00:00:00+08:00",
            "window_is_session": False,
            "source": {"version": "version1", "generated_at": 10, "verified_at": 10,
                       "data_refs": [f"audio_{tag}_{date}/.results/seg_0001.json"],
                       "time_basis": "writer_wall_clock_plus_offset", "tag_is_speaker": False},
            "completeness": {"state": "current_at_build", "coverage": "observed_material_only",
                             "raw_freshness": query.FRESHNESS, "segments": []},
            "views": {"canonical": {"available": True, "records": 0, "status": "current_at_build",
                                    "redaction": {"policy": "none", "applied": False}},
                      "shared": {"available": True, "records": 0, "status": "historical_unverified",
                                 "redaction": {"policy": "legacy_url_email_phone", "actual_replacements": None,
                                               "original_row": None}}}}


class Snapshot:
    def __init__(self, root):
        self.root = root
        root.mkdir()
        self.manifest = {"schema": 1, "version": "version1", "groups": [group()], "files": [],
                         "generated_at": 10, "verified_at": 10, "data_scope": {}, "discovery": {}}
        self.status = {"state": "success", "version": "version1", "updated_at": 10,
                       "last_success_at": 10, "error": None, "snapshot_stale": False,
                       "private_key": "must-not-be-returned"}
        self.cursor_key = b"x" * 32
        self.finishes = 0
        self.finish_hook = None

    def finish(self):
        self.finishes += 1
        if self.finish_hook:
            self.finish_hook()

    def add(self, rows=None, *, raw=None, name=None, view="canonical", group_index=0):
        name = name or f"payload/{len(self.manifest['files']):04}.jsonl"
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = raw if raw is not None else b"".join(encoded(item) for item in rows)
        path.write_bytes(raw)
        selected = self.manifest["groups"][group_index]
        entry = {"path": name, "view": view, "format": "canonical_records" if view == "canonical" else "shared_projection",
                 "tag": selected["tag"], "date": selected["date"], "platform": selected["platform"],
                 "group_id": selected["id"], "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
                 "identity": common.identity(path),
                 "chunks": [hashlib.sha256(raw[index:index + query.CHUNK_BYTES]).hexdigest()
                            for index in range(0, len(raw), query.CHUNK_BYTES)],
                 "records": len(rows) if rows is not None else 0}
        self.manifest["files"].append(entry)
        return path


@pytest.fixture
def snapshot(tmp_path):
    return Snapshot(tmp_path / "new-task09A-materials")


def reason(error):
    return error.value.reason


def test_default_canonical_two_pages_and_exact_material_location(snapshot):
    snapshot.add([row(i, text=f"原文{i} https://example.test 13800138000") for i in range(51)])
    first = query.query(snapshot)
    second = query.query(snapshot, cursor=first["next_cursor"])
    assert len(first["items"]) == 50 and len(second["items"]) == 1
    assert second["next_cursor"] is None and snapshot.finishes == 2
    item = first["items"][0]
    assert "https://example.test 13800138000" in item["text"]
    assert item["source"]["json_pointer"] == "/rows/0"
    assert item["source"]["material"] == {"version": "version1", "path": "payload/0000.jsonl",
                                            "sha256": snapshot.manifest["files"][0]["sha256"], "byte_offset": 0}
    assert item["redaction"] == {"policy": "none", "applied": False}
    assert item["completeness"]["coverage"] == "observed_material_only"
    assert first["source_freshness"] == query.FRESHNESS
    assert first["window_is_session"] is False and first["tag_is_speaker"] is False
    assert "private_key" not in first["refresh"] and b"x" * 32 not in repr(first).encode()


def test_shared_six_streams_footer_and_explicit_redaction(snapshot):
    snapshot.add([row(99, text="canonical only")])
    values = [row(i, view="shared", stream=stream, text="[链接] [电话] [邮箱]")
              for i, stream in enumerate(query.STREAMS)]
    footer = {"stream": "export_info", "schema": 1, "platform": "douyin", "tag": "房间甲",
              "window_is_session": False}
    snapshot.add(values + [footer], view="shared")
    result = query.query(snapshot, view="shared", stream=list(reversed(query.STREAMS)))
    assert [item["stream"] for item in result["items"]] == list(query.STREAMS)
    assert result["scan"]["lines"] == 7 and result["next_cursor"] is None
    assert all("result_ref" not in item["source"] for item in result["items"])
    assert result["items"][0]["redaction"] == {"policy": "legacy_url_email_phone", "actual_replacements": None,
                                                 "original_row": None}
    assert query.query(snapshot)["items"][0]["text"] == "canonical only"


def test_filters_are_half_open_without_global_time_sort(snapshot):
    snapshot.add([row(0, ts=9), row(1, ts=2), row(2, ts=7), row(3, ts=4)])
    snapshot.manifest["groups"].append(group("房间乙", group_id="group2"))
    snapshot.add([row(5, tag="房间乙", ts=3)], group_index=1)
    result = query.query(snapshot, room="房间甲", date="2026-11-16", platform="douyin",
                         stream="speech", time_from=2, time_until=7)
    assert [item["ts"] for item in result["items"]] == [2, 4]
    assert result["order"] == "manifest_file_then_line"


def test_stable_pages_across_files(snapshot):
    snapshot.add([row(0), row(1)])
    snapshot.add([row(2), row(3)])
    cursor, values = None, []
    for _ in range(5):
        page = query.query(snapshot, cursor=cursor, limit=1)
        values.extend(item["text"] for item in page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert values == ["0", "1", "2", "3"] and cursor is None


def test_low_hit_line_budget_returns_empty_page_and_progress(snapshot):
    snapshot.add([row(i, view="shared", stream="stats") for i in range(1001)], view="shared")
    first = query.query(snapshot, view="shared", stream="speech")
    assert first["items"] == [] and first["scan"]["lines"] == 1000
    assert first["scan"]["limited"] and first["next_cursor"]
    second = query.query(snapshot, view="shared", stream="speech", cursor=first["next_cursor"])
    assert second["items"] == [] and second["scan"]["lines"] == 1 and second["next_cursor"] is None


def test_actual_chunk_byte_budget_and_page_local_cache(snapshot, monkeypatch):
    snapshot.add([row(i, view="shared", stream="stats", text="字" * 19000) for i in range(40)], view="shared")
    original, reads = common.open_file, []
    @contextmanager
    def observed(*args, **kwargs):
        with original(*args, **kwargs) as handle:
            class Reader:
                def seek(self, offset):
                    return handle.seek(offset)
                def read(self, size):
                    raw = handle.read(size)
                    reads.append(len(raw))
                    return raw
            yield Reader()
    monkeypatch.setattr(common, "open_file", observed)
    cursor, scanned = None, 0
    for _ in range(10):
        reads.clear()
        result = query.query(snapshot, view="shared", stream="speech", cursor=cursor)
        assert result["scan"]["bytes"] == sum(reads) <= query.MAX_DATA_BYTES
        assert len(reads) <= 16
        scanned += result["scan"]["lines"]
        cursor = result["next_cursor"]
        if cursor is None:
            break
        assert result["scan"]["limited"] and not result["items"]
    assert cursor is None and scanned == 40


def test_file_budget_can_continue_without_dropping_files(snapshot):
    for i in range(65):
        snapshot.add([row(i, view="shared", stream="stats")], view="shared")
    first = query.query(snapshot, view="shared", stream="speech")
    second = query.query(snapshot, view="shared", stream="speech", cursor=first["next_cursor"])
    assert first["scan"]["files"] == 64 and first["scan"]["limited"]
    assert second["scan"]["files"] == 1 and second["next_cursor"] is None


@pytest.mark.parametrize("value", [0, 201, True, 2.5, "50", None])
def test_limit_rejects_invalid(snapshot, value):
    with pytest.raises(query.MaterialQueryError) as error:
        query.query(snapshot, limit=value)
    assert reason(error) == "invalid_limit"


def test_maximum_limit_200(snapshot):
    snapshot.add([row(i) for i in range(201)])
    assert len(query.query(snapshot, limit=200)["items"]) == 200


@pytest.mark.parametrize("filters", [
    {"view": []}, {"view": "both"}, {"room": "bad\x00room"}, {"date": "2026-02-30"},
    {"date": "20261116"}, {"platform": []}, {"stream": []}, {"stream": ["stats"] * 7},
    {"stream": [True]}, {"time_from": True}, {"time_until": float("nan")},
    {"time_from": 5, "time_until": 5}, {"time_from": float("inf")},
])
def test_bad_filters_have_safe_errors(snapshot, filters):
    with pytest.raises(query.MaterialQueryError) as error:
        query.query(snapshot, **filters)
    assert reason(error) == "invalid_filter"


def test_cursor_hmac_filters_kind_version_and_length(snapshot):
    snapshot.add([row(0), row(1)])
    cursor = query.query(snapshot, limit=1)["next_cursor"]
    cases = [(cursor[:-1] + ("a" if cursor[-1] != "a" else "b"), {}, "invalid_cursor"),
             ("x" * 2049, {}, "invalid_cursor"), (cursor, {"room": "different"}, "invalid_cursor")]
    for token, filters, expected in cases:
        with pytest.raises(query.MaterialQueryError) as error:
            query.query(snapshot, cursor=token, **filters)
        assert reason(error) == expected
    with pytest.raises(query.MaterialQueryError) as error:
        query.list_groups(snapshot, cursor=cursor)
    assert reason(error) == "invalid_cursor"
    snapshot.manifest["version"] = "version2"
    with pytest.raises(query.MaterialQueryError) as error:
        query.query(snapshot, cursor=cursor)
    assert reason(error) == "cursor_expired"


def test_cursor_byte_boundary_and_out_of_range(snapshot):
    snapshot.add([row(0), row(1)])
    filters = query._filters("canonical", None, None, None)
    for position in ({"file": 0, "offset": 1}, {"file": 0, "offset": 10**9}, {"file": 4, "offset": 0}):
        token = query._encode_cursor(snapshot, "query", filters, position)
        with pytest.raises(query.MaterialQueryError) as error:
            query.query(snapshot, cursor=token)
        assert reason(error) == "invalid_cursor"


@pytest.mark.parametrize("raw", [b"\n", b"{\"tag\":1,\"tag\":2}\n", b"{\"ts\":NaN}\n", b"\xff\n",
                                 encoded(row(0))[:-1], encoded(dict(row(0), tag="other")),
                                 encoded(dict(row(0), stream="secret")), encoded(dict(row(0), platform="other")),
                                 encoded(dict(row(0), source=None))])
def test_corrupt_rows_discard_page(snapshot, raw):
    snapshot.add(raw=encoded(row(0)) + raw)
    with pytest.raises(query.MaterialQueryError) as error:
        query.query(snapshot)
    assert reason(error) == "material_corrupt"
    assert snapshot.finishes == 0


def test_cross_chunk_line_and_maximum_line_size(snapshot):
    base = row(0, view="shared", text="")
    overhead = len(encoded(base))
    base["text"] = "x" * (query.MAX_LINE_BYTES - overhead)
    raw = encoded(row(1, view="shared", text="prefix")) + encoded(base)
    snapshot.add(raw=raw, view="shared")
    page = query.query(snapshot, view="shared", limit=1)
    tail = query.query(snapshot, view="shared", cursor=page["next_cursor"])
    assert len(encoded(base)) == query.MAX_LINE_BYTES
    assert tail["items"][0]["text"] == base["text"] and tail["next_cursor"] is None
    assert tail["scan"]["bytes"] <= 3 * query.CHUNK_BYTES


def test_oversized_line_is_explicit(snapshot):
    snapshot.add([row(0, text="x" * query.MAX_LINE_BYTES)])
    with pytest.raises(query.MaterialQueryError) as error:
        query.query(snapshot)
    assert reason(error) == "material_line_too_large"


def test_same_stat_content_tamper_detected_by_consumed_chunk(snapshot):
    path = snapshot.add([row(0, text="AAA")])
    before = path.stat()
    path.write_bytes(path.read_bytes().replace(b"AAA", b"BBB"))
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert common.identity(path) == snapshot.manifest["files"][0]["identity"]
    with pytest.raises(query.MaterialQueryError) as error:
        query.query(snapshot)
    assert reason(error) == "material_changed"


@pytest.mark.parametrize("change", ["append", "replacement"])
def test_observable_file_change_rejected(snapshot, change):
    path = snapshot.add([row(0)])
    if change == "append":
        with path.open("ab") as handle:
            handle.write(encoded(row(1)))
    else:
        retained = path.with_suffix(".retained")
        path.rename(retained)
        path.write_bytes(retained.read_bytes())
    with pytest.raises(common.MaterialError) as error:
        query.query(snapshot)
    assert reason(error) == "material_changed"


@pytest.mark.parametrize("dev,ino", [
    pytest.param(2**63 + 17, 2**50 + 5, id="guest-width"),
    pytest.param(2**64 - 1, 2**128 - 1, id="unsigned-maxima"),
    pytest.param(0, 0, id="zero"),
])
def test_manifest_accepts_windows_file_identity_width(snapshot, dev, ino):
    snapshot.add([row()])
    # Group listing validates serialized identity without opening the payload.
    snapshot.manifest["files"][0]["identity"].update(dev=dev, ino=ino)
    assert len(query.list_groups(snapshot)["items"]) == 1


@pytest.mark.parametrize("field,value", [
    ("dev", 2**64), ("ino", 2**128), ("dev", -1), ("ino", -1),
    ("dev", True), ("ino", "42"), ("mtime_ns", 2**63),
])
def test_manifest_rejects_invalid_file_identity_values(snapshot, field, value):
    snapshot.add([row()])
    snapshot.manifest["files"][0]["identity"][field] = value
    with pytest.raises(query.MaterialQueryError) as error:
        query.list_groups(snapshot)
    assert reason(error) == "material_corrupt"


@pytest.mark.parametrize("field,maximum", [("dev", 2**64 - 1), ("ino", 2**128 - 1)])
def test_changed_wide_file_identity_is_rejected_on_open(snapshot, field, maximum):
    snapshot.add([row()])
    identity = snapshot.manifest["files"][0]["identity"]
    identity[field] = maximum if identity[field] != maximum else maximum - 1
    # A valid manifest value must still equal the real file's opened identity.
    with pytest.raises(common.MaterialError) as error:
        query.query(snapshot)
    assert reason(error) == "material_changed"


def test_finish_version_switch_discards_entire_page(snapshot):
    snapshot.add([row(0)])
    def switched():
        raise common.MaterialError("material_version_changed")
    snapshot.finish_hook = switched
    with pytest.raises(common.MaterialError) as error:
        query.query(snapshot)
    assert reason(error) == "material_version_changed" and snapshot.finishes == 1


def test_list_keeps_unavailable_view_and_reads_no_payload(snapshot, monkeypatch):
    snapshot.add([row(0)])
    snapshot.manifest["groups"][0]["views"]["shared"].update(available=False, status="unsupported_tag")
    def forbidden(*_args, **_kwargs):
        raise AssertionError("group list opened data")
    monkeypatch.setattr(common, "open_file", forbidden)
    result = query.list_groups(snapshot, view="shared")
    assert len(result["items"]) == 1 and result["items"][0]["view_info"]["available"] is False
    assert result["items"][0]["view_info"]["status"] == "unsupported_tag"
    assert "views" not in result["items"][0] and result["scan"]["bytes"] == 0


def test_groups_page_and_filters(snapshot):
    snapshot.manifest["groups"] = [group(f"房{i}", group_id=f"group{i}") for i in range(51)]
    first = query.list_groups(snapshot)
    second = query.list_groups(snapshot, cursor=first["next_cursor"])
    assert len(first["items"]) == 50 and len(second["items"]) == 1 and second["next_cursor"] is None
    assert query.list_groups(snapshot, room="房3")["items"][0]["tag"] == "房3"


def test_query_performs_no_discovery_audio_hash_or_business_actions(snapshot, monkeypatch):
    from scripts import audio_segments as audio, transcript_results as results, audio_cleanup
    snapshot.add([row(0)])
    def forbidden(*_args, **_kwargs):
        raise AssertionError("query caused a business or discovery action")
    monkeypatch.setattr(os, "scandir", forbidden)
    monkeypatch.setattr(results, "validate_success", forbidden)
    monkeypatch.setattr(audio_cleanup, "cleanup_segment", forbidden)
    if hasattr(audio, "fingerprint"):
        monkeypatch.setattr(audio, "fingerprint", forbidden)
    assert query.query(snapshot)["items"][0]["text"] == "0"
    assert query.list_groups(snapshot)["items"]


@pytest.mark.parametrize("field,value", [("path", "../outside.jsonl"), ("path", "C:/outside.jsonl"),
                                         ("path", "payload\\alias.jsonl"), ("bytes", True),
                                         ("chunks", []), ("group_id", "missing")])
def test_bad_manifest_safe_error(snapshot, field, value):
    snapshot.add([row(0)])
    snapshot.manifest["files"][0][field] = value
    with pytest.raises(query.MaterialQueryError) as error:
        query.query(snapshot)
    assert reason(error) == "material_corrupt"


def test_manifest_capacity_is_explicit(snapshot):
    snapshot.manifest["discovery"]["oversize"] = "x" * query.MAX_MANIFEST_BYTES
    with pytest.raises(query.MaterialQueryError) as error:
        query.query(snapshot)
    assert reason(error) == "manifest_capacity"


def test_source_and_facts_known_keys_only(snapshot):
    record = row(0)
    record["source"]["private_key"] = "not-public"
    snapshot.add([record])
    selected = snapshot.manifest["groups"][0]
    selected["private_key"] = "not-public"
    selected["source"]["owner"] = "not-public"
    selected["completeness"]["private_key"] = "not-public"
    selected["completeness"]["segments"] = [{"segment": "seg_0001.flv", "state": "current_speech",
                                              "reason": None, "source_present": True, "private_key": "not-public"}]
    selected["views"]["canonical"]["private_key"] = "not-public"
    for result in (query.query(snapshot), query.list_groups(snapshot)):
        assert "not-public" not in repr(result)
        assert result["items"][0]["completeness"]["segments"][0]["state"] == "current_speech"


@pytest.mark.parametrize("target", ["source_ref", "generation", "redaction", "unknown_row_field"])
def test_bad_canonical_authority_or_unknown_row_is_rejected(snapshot, target):
    record = row(0)
    if target == "source_ref":
        record["source"]["result_ref"] = "C:/private.json"
    elif target == "generation":
        record["source"]["generation"] = "bad"
    elif target == "redaction":
        record["redaction"]["applied"] = True
    else:
        record["private_key"] = "not-public"
    snapshot.add([record])
    with pytest.raises(query.MaterialQueryError) as error:
        query.query(snapshot)
    assert reason(error) == "material_corrupt"


def test_empty_unknown_group_is_not_no_livestream_claim(snapshot):
    selected = snapshot.manifest["groups"][0]
    selected["completeness"]["state"] = "unknown"
    selected["views"]["canonical"].update(available=False, status="incomplete")
    assert query.query(snapshot)["items"] == []
    result = query.list_groups(snapshot)
    assert result["items"][0]["completeness"]["state"] == "unknown"
    assert result["items"][0]["view_info"]["available"] is False
    assert "no_livestream" not in repr(result)


def test_group_low_hit_budget_has_empty_continuation(snapshot):
    snapshot.manifest["groups"] = [group(f"房{i}", group_id=f"group{i}") for i in range(1001)]
    first = query.list_groups(snapshot, room="未命中")
    assert first["items"] == [] and first["scan"]["lines"] == 1000 and first["next_cursor"]
    second = query.list_groups(snapshot, room="未命中", cursor=first["next_cursor"])
    assert second["items"] == [] and second["scan"]["lines"] == 1 and second["next_cursor"] is None


def test_builder_shape_preserves_nested_counts_and_cleaned_source(snapshot):
    record = row(0)
    record["source"].update(audio_state="cleaned", cleanup_state="deleted", verified_at=10, original_line=None)
    snapshot.add([record])
    counts = {"canonical_records": 1, "audio_cleaned": 1, "no_speech": 0, "raw_records": None,
              "redaction_replacements": None, "private_key": "not-public"}
    selected = snapshot.manifest["groups"][0]
    selected["completeness"]["counts"] = counts
    snapshot.manifest["files"][0]["completeness"] = dict(selected["completeness"], segments=[])
    item = query.query(snapshot)["items"][0]
    assert item["source"]["audio_state"] == "cleaned" and item["source"]["cleanup_state"] == "deleted"
    assert item["source"]["original_line"] is None and item["source"]["verified_at"] == 10
    assert item["completeness"]["segments"] == []
    assert item["completeness"]["counts"] == {key: value for key, value in counts.items() if key != "private_key"}
    assert query.list_groups(snapshot)["items"][0]["completeness"]["counts"] == item["completeness"]["counts"]


def test_huge_timestamp_has_safe_filter_and_material_errors(snapshot):
    huge = 10**400
    with pytest.raises(query.MaterialQueryError) as error:
        query.query(snapshot, time_from=huge)
    assert reason(error) == "invalid_filter"
    snapshot.add([row(0, ts=huge)])
    with pytest.raises(query.MaterialQueryError) as error:
        query.query(snapshot)
    assert reason(error) == "material_corrupt"


def test_time_basis_stays_with_view_not_mixed_group_or_legacy_speech(snapshot):
    selected = snapshot.manifest["groups"][0]
    selected["source"]["time_basis"] = "view_dependent_unverified"
    snapshot.add([row(0)])
    snapshot.manifest["files"][-1]["source"] = dict(selected["source"], time_basis="writer_wall_clock_plus_offset")
    snapshot.add([row(1, view="shared", stream="watch"), row(2, view="shared", stream="speech")], view="shared")
    snapshot.manifest["files"][-1]["source"] = dict(selected["source"], time_basis="stored_record_timestamp_unverified",
                                                    generation="b" * 32, result_ref="must-not-be-canonical.json")
    canonical = query.query(snapshot)["items"][0]
    shared = query.query(snapshot, view="shared")["items"]
    assert canonical["source"]["time_basis"] == "writer_wall_clock_plus_offset"
    assert all(item["source"]["time_basis"] == "stored_record_timestamp_unverified" for item in shared)
    assert all("generation" not in item["source"] and "result_ref" not in item["source"] for item in shared)
    for view in ("canonical", "shared"):
        assert query.list_groups(snapshot, view=view)["items"][0]["source"]["time_basis"] == "view_dependent_unverified"


def test_native_directory_lease_pins_query_without_self_conflict(snapshot, monkeypatch):
    """New fixture only: DELETE access/rename denied until the real lease closes."""
    if os.name != "nt":
        pytest.skip("native Windows directory lease")
    import ctypes
    from scripts import audio_cleanup

    snapshot.add([row(0)])
    payload = snapshot.root / "payload"
    native = audio_cleanup._Windows()
    invalid = (None, ctypes.c_void_p(-1).value)

    def delete_access(path, *, denied):
        handle = native.api.CreateFileW(audio_cleanup._extended(path), 0x10000, 0x7,
                                       None, 3, 0x200000 | 0x2000000, None)
        code = ctypes.get_last_error()
        try:
            if denied:
                assert handle in invalid and code == 32, (path, handle, code)
            else:
                assert handle not in invalid, (path, code)
        finally:
            if handle not in invalid:
                native.api.CloseHandle(handle)

    def rename_denied(path):
        renamed = path.with_name(path.name + "-lease-probe")
        moved = False
        try:
            try:
                path.rename(renamed)
                moved = True
            except OSError as error:
                assert error.winerror in (5, 32), error
            else:
                pytest.fail("a directory moved while its namespace lease was held")
        finally:
            if moved:
                renamed.rename(path)

    # A positive control proves this test user has DELETE access.  No delete
    # disposition is requested; every successful native handle is closed.
    delete_access(payload, denied=False)
    with common.guard_directory(payload):
        delete_access(payload, denied=True)
        rename_denied(payload)
    delete_access(payload, denied=False)
    retained = payload.with_name("payload-rename-control")
    payload.rename(retained)
    retained.rename(payload)

    original, observed = common.open_file, []
    @contextmanager
    def pinned_read(*args, **kwargs):
        with original(*args, **kwargs) as handle:
            for path in (snapshot.root, payload):
                delete_access(path, denied=True)
                rename_denied(path)
                observed.append(path)
            yield handle
    monkeypatch.setattr(common, "open_file", pinned_read)
    # Store also pins its version directories.  The nested actual query opens
    # must be share-compatible while continuing to reject namespace deletion.
    with common.guard_directory(snapshot.root), common.guard_directory(payload):
        result = query.query(snapshot)
    assert observed == [snapshot.root, payload]
    assert result["items"][0]["text"] == "0" and snapshot.finishes == 1
    delete_access(snapshot.root, denied=False)
    delete_access(payload, denied=False)
