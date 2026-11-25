"""The recorder closes direct-DNS sessions on replacement and every exit."""
from enum import IntEnum
from pathlib import Path
import sys
from types import SimpleNamespace
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import stream_recorder as recorder
from stream_network import StreamNetworkError

@pytest.mark.skipif(sys.platform != "win32", reason="uses Windows narrow CA paths")
@pytest.mark.parametrize("configured", [False, True])
def test_installed_recorder_uses_same_ca_with_non_ansi_install_root(tmp_path, monkeypatch, configured):
    import stream_network

    install = tmp_path / "customer-\U0001f680"
    app = install / "app"
    app.mkdir(parents=True)
    runtime = install / "runtime"
    runtime.mkdir()
    default_ca = str(runtime / "certifi.pem")
    configured_ca = str(runtime / "custom.pem")
    monkeypatch.chdir(app)
    monkeypatch.setattr(recorder, "installed_layout", lambda: {})
    curl_opt = SimpleNamespace(CAINFO="CAINFO", PROXY_CAINFO="PROXY_CAINFO")
    session = SimpleNamespace(verify=configured_ca if configured else True,
                              curl_options={"existing": "retained"})
    monkeypatch.setitem(sys.modules, "curl_cffi",
                        SimpleNamespace(CurlOpt=curl_opt, requests=SimpleNamespace(Session=lambda **_: session)))
    monkeypatch.setitem(sys.modules, "curl_cffi.curl", SimpleNamespace(DEFAULT_CACERT=default_ca))
    monkeypatch.setattr(stream_network, "wrap_session", lambda raw: raw)

    assert recorder.build_session({}) is session
    assert session.verify is True
    filename = "custom.pem" if configured else "certifi.pem"
    expected = str(Path("..") / "runtime" / filename).encode("ascii")
    assert session.curl_options == {"existing": "retained", "CAINFO": expected, "PROXY_CAINFO": expected}


class Session:
    def __init__(self):self.closed=0
    def close(self):self.closed+=1

def call():
    return recorder.pull_loop('synthetic','synthetic',None,lambda *_:None,30,60,0,'node',{},[False])

@pytest.mark.parametrize('reason',['offline','quota','error','stalled','signal'])
def test_every_stop_reason_closes_current_session(monkeypatch,reason):
    session=Session();monkeypatch.setattr(recorder,'build_session',lambda _:session)
    def impl(*args):args[-1]({});return reason
    monkeypatch.setattr(recorder,'_pull_loop_impl',impl)
    assert call()==reason and session.closed==1

def test_replacement_closes_old_session_before_returning_replacement(monkeypatch):
    first,second=Session(),Session();remaining=iter((first,second))
    monkeypatch.setattr(recorder,'build_session',lambda _:next(remaining))
    def impl(*args):
        factory=args[-1];assert factory({}) is first
        assert factory({}) is second
        assert first.closed==1 and second.closed==0
        return 'quota'
    monkeypatch.setattr(recorder,'_pull_loop_impl',impl)
    assert call()=='quota' and first.closed==second.closed==1

def test_failure_building_replacement_still_closes_old_session(monkeypatch):
    first=Session();calls=[]
    def build(_):
        calls.append(True)
        if len(calls)>1:raise RuntimeError('synthetic')
        return first
    monkeypatch.setattr(recorder,'build_session',build)
    def impl(*args):args[-1]({});args[-1]({})
    monkeypatch.setattr(recorder,'_pull_loop_impl',impl)
    with pytest.raises(RuntimeError,match='synthetic'):call()
    assert first.closed==1


@pytest.mark.parametrize(("branch", "stop_reason", "expected"), [
    ("get_error", "error", "reason=stream_connect_failed dns_reason=None curl_code=77 http_status=403"),
    ("dns_error", "error", "reason=uplink_dns_resolution_failed dns_reason=uplink_dns_query_timeout curl_code=None"),
    ("http_response", "error", "HTTP 403(3/3)"),
    ("body_error", "stalled", "reason=request_failed dns_reason=None curl_code=77 http_status=200"),
])
def test_real_pull_loop_logs_only_safe_request_diagnostics(monkeypatch, branch, stop_reason, expected):
    class CurlCode(IntEnum):
        CA_LOAD_FAILED = 77

    secret_url = "https://cdn.example/private.flv?sign=secret-token"
    raw_error = "signed payload and cookie must not appear"

    def read_body(_size):
        error = RuntimeError(raw_error)
        error.code = CurlCode.CA_LOAD_FAILED
        raise error

    def read_media(url, **kwargs):
        assert url == secret_url
        assert kwargs["stream"] is True and kwargs["timeout"] == (10, 30)
        if branch == "dns_error":
            raise StreamNetworkError("uplink_dns_resolution_failed", dns_reason="uplink_dns_query_timeout")
        if branch == "get_error":
            error = StreamNetworkError("stream_connect_failed", code=CurlCode.CA_LOAD_FAILED,
                                       response=SimpleNamespace(status_code=403))
            error.args = (raw_error,)
            raise error
        return SimpleNamespace(status_code=403 if branch == "http_response" else 200,
                               close=lambda: None, iter_content=read_body)

    monkeypatch.setattr(recorder, "read_watch_state", lambda _tag: None)
    monkeypatch.setattr(recorder, "capture_stream", lambda *_args: None)
    monkeypatch.setattr(recorder, "interruptible_wait", lambda *_args: False)
    writer = SimpleNamespace(seg_seconds=60, quota_bytes=1024, stop_check=None,
                             begin_connection=lambda: None, end_connection=lambda _phase: None)
    logs = []
    result = recorder._pull_loop_impl(
        "synthetic", "synthetic", writer, logs.append, 30, 60, 0, "node",
        {"playerFlvUrls": [secret_url]}, [False], "auto", 900,
        lambda _capture: SimpleNamespace(get=read_media),
    )
    output = "\n".join(logs)
    assert result == stop_reason
    assert expected in output and "elapsed_ms=" in output
    assert "cdn.example" not in output and "secret-token" not in output and raw_error not in output
