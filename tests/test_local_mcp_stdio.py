"""Official mcp2.3 Client -> owned stdio child; no installed customer DATA."""
import hashlib
from functools import partial
import importlib.metadata
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

pytest.importorskip("mcp", reason="optional fixed requirements-mcp.txt is required")
if importlib.metadata.version("mcp") != "2.3.0":
    pytest.skip("this protocol suite binds mcp==2.3.0", allow_module_level=True)
import anyio
from mcp import Client, MCPError, StdioServerParameters

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]
from scripts import audio_segments as audio, transcript_results as results
from scripts import local_mcp as entry, material_common as common
from scripts.backfill_tasks import process_identity
from scripts.local_materials import MaterialStore

OUT = ROOT.parent / "task-10/sdk/tmp-parent"
OBS = []
TOOL_NAMES = {"materials_groups", "materials_query", "application_health", "materials_status"}


def snapshot(path):
    return {file.relative_to(path).as_posix(): hashlib.sha256(file.read_bytes()).hexdigest()
            for file in path.rglob("*") if file.is_file()}


@pytest.fixture
def case(tmp_path):
    assert tmp_path.resolve().is_relative_to(OUT.resolve()) and list(tmp_path.iterdir()) == []
    data, control = tmp_path / "generated-data", tmp_path / "control-only"
    data.mkdir(); control.mkdir()
    context = {"schema": 1, "root": str(ROOT), "instance_id": "sdk-task10-generated",
        "data": str(data), "config": str(tmp_path / "unused-config.json"), "roster": str(tmp_path / "unused-roster.json"),
        "dist": str(tmp_path / "unused-dist"), "python": str(Path(sys.executable)), "profile": str(tmp_path / "never-profile"),
        "port": 51234, "cdp_port": 51235}
    path = tmp_path / "explicit-context.json"
    path.write_bytes(common.encode(context))
    store = MaterialStore(context)
    value = {"base": tmp_path, "data": data, "control": control, "context": context, "path": path, "store": store}
    return value


def prepared(c):
    data = c["data"]
    folder = data / "audio_removed-room_2026-01-01"
    folder.mkdir()
    number, source = audio.reserve_segment(data, folder)
    with source.open("xb") as stream:
        stream.write(b"task10-generated-not-real-audio" * 256)
        stream.flush(); os.fsync(stream.fileno())
    stamp = 1767268800.0
    audio.publish_closed(data, folder, source, number, stamp, stamp + 60)
    rows = [{"start": 0, "end": 1, "text": "original https://example.invalid 13800138000"},
            {"start": 2, "end": 3, "text": "second generated row"}]
    assert results.process_segment(data, folder, source.name, lambda _path: (rows, 60), "fake-sdk-model")["completed_now"]
    results.rebuild_projection(data, folder)
    (data / "danmu_douyin_removed-room_2026-01-01.jsonl").write_bytes(common.encode(
        {"ts": stamp, "type": "danmaku", "text": "generated https://example.invalid"}))
    first = c["store"].refresh()
    assert first["state"] == "published", first
    c["first"] = first
    c["pointer_a"] = (c["store"].root / "current.json").read_bytes()


def bootstrap(c, *, fake=False):
    """Test-only observation/forbidden-write seams; production CLI is unchanged."""
    control, data, derived = c["control"], c["data"], c["store"].root
    source = r'''
import atexit,json,os,sys,time,logging
from pathlib import Path
ROOT=Path(__ROOT__);sys.path[:0]=[str(ROOT),str(ROOT/'scripts')]
DATA=Path(__DATA__);DERIVED=Path(__DERIVED__);CONTROL=Path(__CONTROL__)
audit_counts={'business_write':0,'network':0,'process':0,'registry':0,'eventloop_loopback':0};guards=[]
def protected(raw):
    try:
        p=Path(raw).absolute()
        return p.is_relative_to(DATA) or p.is_relative_to(DERIVED)
    except (TypeError,ValueError): return False
def audit(event,args):
    bad=None
    if event in {'socket.bind','socket.connect'}:
        frame=sys._getframe(1);internal=False
        while frame is not None:
            if frame.f_code.co_name=='socketpair' and Path(frame.f_code.co_filename).absolute()==Path(sys.base_prefix)/'Lib/socket.py': internal=True;break
            frame=frame.f_back
        address=args[1]
        if internal and isinstance(address,tuple) and address[0]=='127.0.0.1' and (event!='socket.bind' or address[1]==0):
            audit_counts['eventloop_loopback']+=1;return
    if event=='open' and protected(args[0]) and args[2] & (os.O_WRONLY|os.O_RDWR|os.O_CREAT|os.O_TRUNC|os.O_APPEND): bad='business_write'
    if event in {'os.remove','os.rmdir','os.mkdir','os.rename'} and any(protected(x) for x in args[:2]): bad='business_write'
    if event in {'socket.connect','socket.bind','socket.getaddrinfo'}: bad='network'
    if event in {'subprocess.Popen','os.system','os.spawn'}: bad='process'
    if event.startswith('winreg.'): bad='registry'
    if bad:
        audit_counts[bad]+=1;raise AssertionError('forbidden MCP side effect')
sys.addaudithook(audit)
(CONTROL/'pid.txt').write_text(str(os.getpid()))
from scripts import local_mcp as e,audio_cleanup as cleanup,material_common as common,audio_segments as audio
from scripts.local_materials import MaterialStore
from scripts.local_mcp_data import ReadService
def forbidden(*args,**kwargs): raise AssertionError('MCP write/ASR/cleanup forbidden')
for cls in (cleanup._Guard,common._DirectoryGuard):
    original=cls.__enter__
    def tracked(guard,original=original):
        result=original(guard);guards.append(guard);return result
    cls.__enter__=tracked
MaterialStore.refresh=forbidden;common.write_json=forbidden;audio.atomic_json=forbidden
cleanup._disposition_flag=forbidden;cleanup.cleanup_segment=forbidden
original_writer=e._BoundedOutput
class ObservedOutput(original_writer):
    def _write_wire(self,raw):
        assert len(raw)<=e.MAX_WIRE_FRAME
        with (CONTROL/'actual-wire.jsonl').open('ab') as stream: stream.write(raw)
        return super()._write_wire(raw)
e._BoundedOutput=ObservedOutput
if __FAKE__:
    class Fake:
        def groups(self,room=None,**kwargs):
            if room=='leak':
                print('DO_NOT_ECHO_PRINT_CANARY');print('DO_NOT_ECHO_PRINT_CANARY',file=sys.stderr)
                os.write(1,b'DO_NOT_ECHO_PRINT_CANARY');os.write(2,b'DO_NOT_ECHO_PRINT_CANARY')
                logging.error('DO_NOT_ECHO_PRINT_CANARY',exc_info=True)
            if room=='slow':
                with common.open_file(DATA,'readonly-lease.json') as stream:
                    (CONTROL/'lease-open.txt').write_text('open')
                    time.sleep(.35)
                    assert stream.read()
                (CONTROL/'lease-closed.txt').write_text('closed')
            text='漢'*500000 if room=='utf8' else '"'*720000 if room=='escaped' else 'small'
            return {'ok':True,'code':None,'error':None,'data':{'text':text},'content_is_untrusted':True}
        query=groups
        def health(self,**kwargs): return self.groups()
        def status(self,**kwargs): return self.groups()
    ReadService.from_context_file=classmethod(lambda cls,path:Fake())
def finish():
    (CONTROL/'audit.json').write_text(json.dumps({'audit':audit_counts,'guards':len(guards),'all_closed':all(g.fd is None for g in guards)}))
atexit.register(finish)
raise SystemExit(e.main(['--context',__CONTEXT__]))
'''
    for key, value in {"__ROOT__": str(ROOT), "__DATA__": str(data), "__DERIVED__": str(derived),
                       "__CONTROL__": str(control), "__CONTEXT__": str(c["path"])}.items():
        source = source.replace(key, repr(value))
    source = source.replace("__FAKE__", repr(fake))
    path = control / "observed_server.py"
    path.write_text(source, encoding="utf-8")
    return path


async def session(c, action, *, mode="auto", fake=False):
    script = bootstrap(c, fake=fake)
    before_data, before_material = snapshot(c["data"]), snapshot(c["store"].root) if c["store"].root.exists() else {}
    with (c["control"] / "stderr.txt").open("w", encoding="utf-8") as errors:
        params = StdioServerParameters(command=sys.executable, args=["-X", "utf8", "-B", str(script)], cwd=str(ROOT),
            env={"PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "TEMP": str(c["control"]),
                 "TMP": str(c["control"]), "DY_CUSTOMER_CONTEXT": ""})
        # The public Client accepts the official stdio context manager as a
        # Transport; errlog captures only this owned child's stderr.
        from mcp.client.stdio import stdio_client
        async with Client(stdio_client(params, errlog=errors), mode=mode, cache=None, read_timeout_seconds=8) as client:
            pid = int((c["control"] / "pid.txt").read_text())
            identity = process_identity(pid)
            assert identity["state"] == "alive"
            protocol = client.protocol_version
            await action(client)
    deadline = time.monotonic() + 2
    while process_identity(pid).get("state") == "alive" and process_identity(pid).get("created") == identity["created"] and time.monotonic() < deadline:
        await anyio.sleep(.01)
    after = process_identity(pid)
    assert after.get("state") == "dead" or after.get("state") == "alive" and after.get("created") != identity["created"]
    assert snapshot(c["data"]) == before_data
    assert (snapshot(c["store"].root) if c["store"].root.exists() else {}) == before_material
    audit = json.loads((c["control"] / "audit.json").read_text())
    assert {key: audit["audit"][key] for key in ("business_write", "network", "process", "registry")} == {"business_write": 0, "network": 0, "process": 0, "registry": 0}
    assert audit["audit"]["eventloop_loopback"] in (0, 2) and audit["all_closed"]
    raw = (c["control"] / "actual-wire.jsonl").read_bytes()
    assert raw.endswith(b"\n") and all(len(line) + 1 <= entry.MAX_WIRE_FRAME for line in raw.splitlines())
    assert all(isinstance(json.loads(line), dict) for line in raw.splitlines())
    assert "DO_NOT_ECHO" not in raw.decode() and "DO_NOT_ECHO" not in (c["control"] / "stderr.txt").read_text()
    OBS.append({"mode": mode, "protocol": protocol, "identity": identity, "after": after,
                "wire_bytes": len(raw), "max_frame_bytes": max(map(len, raw.splitlines())) + 1, "audit": audit})


def run_session(c, action, **options):
    anyio.run(partial(session, c, action, **options))


@pytest.mark.parametrize("mode", ["auto", "legacy"])
def test_official_client_discovers_only_four_strict_tools_and_reads_real_materials(case, mode):
    c = case
    prepared(c)
    async def exercise(client):
        listed = await client.list_tools()
        assert {tool.name for tool in listed.tools} == TOOL_NAMES
        assert client.protocol_version == ("2026-07-28" if mode == "auto" else "2025-11-25")
        for tool in listed.tools:
            assert tool.input_schema["additionalProperties"] is False
            assert tool.annotations.read_only_hint is True and tool.annotations.open_world_hint is False
        groups = await client.call_tool("materials_groups", {})
        assert not groups.is_error and groups.structured_content["data"]["items"][0]["tag"] == "removed-room"
        first = await client.call_tool("materials_query", {"limit": 1})
        assert first.structured_content["data"]["next_cursor"]
        assert "https://example.invalid" in first.structured_content["data"]["items"][0]["text"]
        following = await client.call_tool("materials_query", {"limit": 1, "cursor": first.structured_content["data"]["next_cursor"]})
        assert following.structured_content["data"]["items"][0]["text"] == "second generated row"
        shared = await client.call_tool("materials_query", {"view": "shared", "stream": "speech"})
        assert "https://example.invalid" not in json.dumps(shared.structured_content)
        health = await client.call_tool("application_health", {})
        assert health.structured_content["data"]["runtime"]["state"] == "unknown"
        status = await client.call_tool("materials_status", {})
        assert status.structured_content["data"]["material"]["version"] == c["first"]["version"]
        assert json.loads(status.content[0].text) == status.structured_content
        for method in (client.list_resources, client.list_prompts):
            with pytest.raises(MCPError):
                await method()
    run_session(c, exercise, mode=mode)


def test_actual_tool_calls_reject_unknown_names_extra_write_fields_and_wrong_types(case):
    c = case
    async def exercise(client):
        invalid = [ {"force": True}, {"path": "DO_NOT_ECHO_PATH"}, {"url": "https://outside.invalid"},
            {"limit": "50"}, {"limit": True}, {"limit": 201}, {"date": "2026-02-30"},
            {"cursor": "x" * 2049}, {"time_from": 2, "time_until": 1}]
        for args in invalid:
            result = await client.call_tool("materials_query", args)
            assert result.is_error and result.structured_content["code"] == "invalid_parameters"
        for args in ({"request_id": "0" * 36}, {"request_id": "00000000-0000-0000-0000-000000000001"}):
            result = await client.call_tool("materials_status", args)
            assert result.is_error and result.structured_content["code"] == "invalid_parameters"
        result = await client.call_tool("refresh_DO_NOT_ECHO_PATH", {})
        assert result.is_error and result.structured_content["code"] == "unknown_tool"
    run_session(c, exercise)


@pytest.mark.parametrize("state", ["not_ready", "corrupt"])
def test_unbuilt_and_corrupt_materials_are_structured_errors_not_empty_success(case, state):
    c = case
    if state == "corrupt":
        prepared(c)
        (c["store"].root / "current.json").write_bytes(b"generated-broken-current")
    async def exercise(client):
        result = await client.call_tool("materials_query", {})
        assert result.is_error and result.structured_content["code"] == "materials_" + state
        result = await client.call_tool("materials_status", {})
        assert result.structured_content["data"]["material"]["state"] == state
    run_session(c, exercise)


def test_real_sdk_text_structured_utf8_bytes_and_oversize_are_bounded_on_wire(case):
    c = case
    async def exercise(client):
        utf8 = await client.call_tool("materials_groups", {"room": "utf8"})
        assert not utf8.is_error and len(utf8.structured_content["data"]["text"]) == 500000
        escaped = await client.call_tool("materials_groups", {"room": "escaped"})
        assert escaped.is_error and escaped.structured_content["code"] == "result_too_large" and escaped.structured_content["data"] is None
        await client.call_tool("materials_groups", {"room": "leak"})
    run_session(c, exercise, fake=True)
    wire = (c["control"] / "actual-wire.jsonl").read_bytes()
    assert max(map(len, wire.splitlines())) > 2500000


def test_sdk_serialized_wire_fallback_includes_large_rpc_id_and_lf():
    from mcp.types import CallToolResult, JSONRPCResponse, TextContent
    result = CallToolResult(content=[TextContent(type="text", text="generated" * entry.MAX_INPUT_FRAME)])
    request_id = "generated-rpc-" + "i" * 64000
    response = JSONRPCResponse(jsonrpc="2.0", id=request_id, result=result.model_dump(mode="json", by_alias=True, exclude_unset=True))
    raw = response.model_dump_json(by_alias=True, exclude_unset=True) + "\n"
    # Force the final transport guard independently of the handler result guard.
    # This is a real SDK JSONRPCResponse serialization, not a dictionary estimate.
    oversized = JSONRPCResponse(jsonrpc="2.0", id=request_id, result={"generated": "x" * entry.MAX_WIRE_FRAME})
    output = io.BytesIO()
    entry._BoundedOutput(output).write(oversized.model_dump_json(by_alias=True, exclude_unset=True) + "\n")
    final = output.getvalue()
    assert final.endswith(b"\n") and len(final) <= entry.MAX_WIRE_FRAME
    decoded = json.loads(final)
    assert decoded["id"] == request_id and decoded["result"]["structuredContent"]["code"] == "result_too_large"
    assert len(raw.encode("utf-8")) > 64000


def test_official_dispatcher_large_rpc_id_is_retained_in_actual_stdio_wire(case):
    c = case
    request_id = "generated-rpc-" + "i" * 60000
    async def exercise(client):
        # CallOptions.request_id is SDK2.3's public dispatcher API. The fixed
        # release's session dispatcher is used only by this protocol-edge test.
        raw = await client.session._dispatcher.send_raw_request("tools/call",
            {"name": "application_health", "arguments": {}}, {"request_id": request_id, "timeout": 5})
        assert raw["structuredContent"]["ok"] is True
    run_session(c, exercise, mode="legacy", fake=True)
    frames = [json.loads(line) for line in (c["control"] / "actual-wire.jsonl").read_bytes().splitlines()]
    assert any(frame.get("id") == request_id for frame in frames)


def test_oversize_input_frame_closes_without_running_a_tool_or_echoing_input(case):
    c = case
    async def exercise(client):
        with pytest.raises(MCPError):
            await client.call_tool("materials_groups", {"room": "x" * (entry.MAX_INPUT_FRAME + 1024)})
    run_session(c, exercise, fake=True)
    assert "mcp_input_too_large" in (c["control"] / "stderr.txt").read_text()


def test_prebuilt_material_version_switch_expires_cursor_without_refreshing_in_stdio(case):
    c = case
    prepared(c)
    second = c["store"].refresh()
    assert second["state"] == "published"
    pointer = c["store"].root / "current.json"
    pointer_b = pointer.read_bytes()
    pointer.write_bytes(c["pointer_a"])
    cursors = []
    async def first(client):
        result = await client.call_tool("materials_query", {"limit": 1})
        assert result.structured_content["data"]["version"] == c["first"]["version"]
        cursors.append(result.structured_content["data"]["next_cursor"])
    run_session(c, first)
    # Both versions were prepared before either server. Only the harness points
    # to the other prebuilt snapshot between child runs; no MCP tool writes.
    pointer.write_bytes(pointer_b)
    following = dict(c, control=c["base"] / "control-second-version")
    following["control"].mkdir()
    async def changed(client):
        result = await client.call_tool("materials_query", {"limit": 1, "cursor": cursors[0]})
        assert result.is_error and result.structured_content["code"] == "cursor_expired"
        fresh = await client.call_tool("materials_query", {"limit": 1})
        assert fresh.structured_content["data"]["version"] == second["version"]
    run_session(following, changed)


@pytest.mark.parametrize("cancel", [False, True])
def test_eof_and_call_cancellation_wait_for_real_synchronous_lease_close(case, cancel):
    c = case
    (c["data"] / "readonly-lease.json").write_bytes(b'{"generated":"lease"}\n')
    script = bootstrap(c, fake=True)
    source = script.read_text(encoding="utf-8").replace("                    time.sleep(.35)",
        "                    deadline=time.monotonic()+3\n"
        "                    while not (CONTROL/'release-read.txt').exists() and time.monotonic()<deadline: time.sleep(.01)\n"
        "                    assert (CONTROL/'release-read.txt').exists()")
    source = source.replace("original_writer=e._BoundedOutput", "original_reader=e._BoundedInput\nclass ObservedInput(original_reader):\n    def readline(self):\n        value=super().readline()\n        if value:\n            with (CONTROL/'actual-input.jsonl').open('ab') as stream: stream.write(value.encode('utf-8'))\n        else: (CONTROL/'actual-eof.txt').write_text('EOF')\n        return value\ne._BoundedInput=ObservedInput\noriginal_writer=e._BoundedOutput")
    script.write_text(source, encoding="utf-8")
    before_data = snapshot(c["data"])
    async def exercise():
        import asyncio
        from unittest.mock import patch
        import mcp.client.stdio as transport
        created, scopes = [], []
        original_create = transport._create_platform_compatible_process
        async def observed_create(*args, **kwargs):
            process = await original_create(*args, **kwargs)
            created.append(process)
            return process
        params = StdioServerParameters(command=sys.executable, args=["-X", "utf8", "-B", str(script)], cwd=str(ROOT),
            env={"PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "DY_CUSTOMER_CONTEXT": ""})
        async def wait_file(name):
            with anyio.fail_after(3):
                while not (c["control"] / name).exists():
                    await anyio.sleep(.01)
        with (c["control"] / "stderr.txt").open("w", encoding="utf-8") as errors:
            with patch.object(transport, "_create_platform_compatible_process", observed_create):
                async with Client(transport.stdio_client(params, errlog=errors), mode="auto", cache=None, read_timeout_seconds=6) as client:
                    protocol = client.protocol_version
                    pid = int((c["control"] / "pid.txt").read_text())
                    identity = process_identity(pid)
                    assert identity["state"] == "alive" and len(created) == 1
                    async def read():
                        with anyio.CancelScope() as scope:
                            scopes.append(scope)
                            return await client.call_tool("materials_groups", {"room": "slow"})
                    pending = asyncio.create_task(read())
                    try:
                        await wait_file("lease-open.txt")
                        if cancel:
                            scopes[0].cancel()
                            await pending
                            busy = await client.call_tool("application_health", {})
                            assert busy.is_error and busy.structured_content["code"] == "read_busy"
                            assert not (c["control"] / "actual-eof.txt").exists()
                        else:
                            # Close the real SDK-created child's stdin pipe,
                            # without cancelling the in-flight request first.
                            await created[0].stdin.aclose()
                            await wait_file("actual-eof.txt")
                        assert not (c["control"] / "lease-closed.txt").exists()
                        assert process_identity(pid) == identity
                        (c["control"] / "release-read.txt").write_text("release")
                        await wait_file("lease-closed.txt")
                        if cancel:
                            following = await client.call_tool("application_health", {})
                            assert not following.is_error and following.structured_content["ok"]
                            assert not (c["control"] / "actual-eof.txt").exists()
                        else:
                            with pytest.raises(MCPError):
                                await pending
                    finally:
                        (c["control"] / "release-read.txt").write_text("release")
                        if not pending.done():
                            pending.cancel()
                            try:
                                await pending
                            except (asyncio.CancelledError, MCPError):
                                pass
        after = process_identity(pid)
        assert after["state"] == "dead" or after["state"] == "alive" and after["created"] != identity["created"]
        audit = json.loads((c["control"] / "audit.json").read_text())
        assert audit["all_closed"] and {key: audit["audit"][key] for key in ("business_write", "network", "process", "registry")} == {"business_write": 0, "network": 0, "process": 0, "registry": 0}
        assert snapshot(c["data"]) == before_data and not c["store"].root.exists()
        assert (c["control"] / "lease-closed.txt").read_text() == "closed"
        incoming = [json.loads(line) for line in (c["control"] / "actual-input.jsonl").read_bytes().splitlines()]
        wire = (c["control"] / "actual-wire.jsonl").read_bytes()
        frames = [json.loads(line) for line in wire.splitlines()]
        slow_id = next(frame["id"] for frame in incoming if frame.get("method") == "tools/call" and frame.get("params", {}).get("arguments", {}).get("room") == "slow")
        if cancel:
            assert not any(frame.get("id") == slow_id and frame.get("result", {}).get("structuredContent", {}).get("ok") is True for frame in frames)
        assert all(len(line) + 1 <= entry.MAX_WIRE_FRAME for line in wire.splitlines())
        OBS.append({"mode": "cancel_connection_open" if cancel else "stdin_eof_without_prior_call_cancel", "protocol": protocol,
            "identity": identity, "after": after, "audit": audit, "lease_closed": True, "data_unchanged": True})
    anyio.run(exercise)


@pytest.mark.parametrize("arguments", [["--force", "DO_NOT_ECHO_STARTUP"], ["--context", "DO_NOT_ECHO_STARTUP"]])
def test_real_entry_startup_errors_have_no_raw_stdout_stderr_or_argument_echo(case, arguments):
    result = subprocess.run([sys.executable, "-X", "utf8", "-B", str(ROOT / "scripts/local_mcp.py"), *arguments],
        cwd=ROOT, input=b"", stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=8,
        env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1", PYTHONIOENCODING="utf-8"))
    assert result.returncode == 2 and result.stdout == b""
    assert result.stderr.strip() in {b"mcp_invalid_startup", b"mcp_context_unavailable"}
    assert b"DO_NOT_ECHO" not in result.stderr and b"Traceback" not in result.stderr


@pytest.fixture(scope="session", autouse=True)
def evidence(request):
    base = Path(request.config.option.basetemp)
    assert base.parent.resolve() == OUT.resolve()
    yield
    (OUT.parent / (base.name + "-observations.json")).write_text(json.dumps(OBS, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def test_explicit_empty_context_never_falls_back_to_valid_environment_context(case):
    c = case
    before = snapshot(c["data"])
    result = subprocess.run([sys.executable, "-X", "utf8", "-B", str(ROOT / "scripts/local_mcp.py"), "--context", ""],
        cwd=ROOT, input=b"", stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=8,
        env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1", PYTHONIOENCODING="utf-8",
                 DY_CUSTOMER_CONTEXT=str(c["path"])))
    assert result.returncode == 2 and result.stdout == b"" and result.stderr.strip() == b"mcp_invalid_startup"
    assert snapshot(c["data"]) == before and not c["store"].root.exists()
