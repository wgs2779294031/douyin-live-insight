"""Offline DNS/redirect/session tests: no adapters, real DNS, HTTP or curl."""
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import stream_network as net


RESOLVE = 10203  # Inject the option value: tests do not need curl_cffi installed.
PROXY = 10004


class Response:
    def __init__(self, status=200, location=None, events=None):
        self.status_code = status
        self.headers = {} if location is None else {"Location": location}
        self.closed = 0
        self.events = events if events is not None else []

    def close(self):
        self.closed += 1
        self.events.append("close_response")

    def iter_content(self, size):
        yield b"synthetic"


class Base:
    def __init__(self, responses=None, events=None):
        self.curl_options = {999: "preserved"}
        self.responses = list(responses or [Response()])
        self.calls, self.closed = [], 0
        self.events = events if events is not None else []

    def get(self, url, **kwargs):
        self.events.append("get")
        self.calls.append((url, kwargs, dict(self.curl_options)))
        return self.responses.pop(0)

    def close(self):
        self.closed += 1
        self.events.append("close_session")


class Provider:
    def __init__(self, records=None, events=None):
        self.records = records or [net.AddressRecord("43.1.2.3", 30)]
        self.calls = []
        self.events = events if events is not None else []

    def resolve(self, host):
        self.calls.append(host)
        self.events.append("resolve:" + host)
        return self.records


def wrap(base=None, provider=None, **kwargs):
    return net.wrap_session(base or Base(), provider or Provider(), resolve_option=RESOLVE,
                            proxy_option=PROXY, **kwargs)


@pytest.mark.parametrize("address", [
    "198.18.0.11", "198.19.255.254", "127.0.0.1", "10.0.0.1", "169.254.1.1",
    "192.168.1.1", "0.0.0.0", "224.0.0.1", "::1", "fe80::1", "fc00::1",
    "::ffff:198.18.0.11", "not-an-ip",
])
def test_fake_and_nonpublic_dns_results_never_reach_http(address):
    base = Base()
    with wrap(base, Provider([net.AddressRecord(address, 60)])) as session:
        with pytest.raises(net.StreamNetworkError, match="no_public_address"):
            session.get("https://cdn.example/stream?sign=private")
    assert base.calls == [] and base.closed == 1


def test_dual_stack_mapping_preserves_signed_url_and_other_options():
    provider = Provider([net.AddressRecord("43.1.2.3", 30),
                         net.AddressRecord("2606:4700:4700::1111", 50)])
    base = Base()
    original = base.curl_options
    url = "https://cdn.example:8443/live.flv?sign=a%2Fb%2Bq&expire=123"
    with wrap(base, provider) as session:
        response = session.get(url, stream=True, timeout=(10, 30), headers={"Referer": "kept"})
        assert list(response.iter_content(1)) == [b"synthetic"]
        assert base.calls == [(url, {"stream": True, "timeout": (10, 30),
                                     "headers": {"Referer": "kept"}, "allow_redirects": False},
                               {999: "preserved", PROXY: "", RESOLVE:
                                ["cdn.example:8443:43.1.2.3,[2606:4700:4700::1111]"]})]
        assert base.curl_options is original
    assert base.closed == 1 and response.closed == 1


def test_ttl_expiry_refreshes_mapping_and_never_pins_old_ip():
    clock = [100.0]
    provider = Provider([net.AddressRecord("43.1.2.3", 5)])
    base = Base([Response(), Response(), Response()])
    with wrap(base, provider, clock=lambda: clock[0]) as session:
        session.get("https://cdn.example/a").close()
        clock[0] += 4
        session.get("https://cdn.example/b").close()
        assert len(provider.calls) == 1
        provider.records = [net.AddressRecord("42.1.2.3", 5)]
        clock[0] += 1
        session.get("https://cdn.example/c").close()
    assert len(provider.calls) == 2
    assert base.calls[-1][2][RESOLVE] == ["cdn.example:443:42.1.2.3"]


def test_expired_cache_does_not_survive_dns_failure():
    clock = [100.0]
    provider = Provider([net.AddressRecord("43.1.2.3", 1)])
    base = Base()
    with wrap(base, provider, clock=lambda: clock[0]) as session:
        session.get("https://cdn.example/a").close()
        clock[0] += 1
        provider.records = [net.AddressRecord("198.18.0.11", 60)]
        with pytest.raises(net.StreamNetworkError, match="no_public_address"):
            session.get("https://cdn.example/a")
    assert len(base.calls) == 1 and len(provider.calls) == 2


@pytest.mark.parametrize("ttl,advance", [(86400, 60), (0, 0)])
def test_cache_is_capped_and_zero_ttl_is_not_cached(ttl, advance):
    clock = [100.0]
    provider = Provider([net.AddressRecord("43.1.2.3", ttl)])
    with wrap(Base([Response(), Response()]), provider, clock=lambda: clock[0]) as session:
        session.get("https://cdn.example/a").close()
        clock[0] += advance
        session.get("https://cdn.example/a").close()
    assert len(provider.calls) == 2


def test_redirect_closes_old_response_before_resolving_new_host():
    events = []
    first = Response(302, "https://new.example/media?sign=redirect%2Fprivate", events)
    last = Response(events=events)
    base = Base([first, last], events)
    provider = Provider(events=events)
    with wrap(base, provider) as session:
        response = session.get("https://old.example/media?sign=original")
        assert response.status_code == 200
        assert events == ["resolve:old.example", "get", "close_response", "resolve:new.example", "get"]
        assert base.calls[1][0] == "https://new.example/media?sign=redirect%2Fprivate"
        assert base.calls[1][2][RESOLVE] == ["new.example:443:43.1.2.3"]
    assert first.closed == last.closed == base.closed == 1


def test_relative_redirect_keeps_origin_and_reuses_only_live_ttl():
    provider = Provider()
    base = Base([Response(307, "/other?sign=unchanged%2F"), Response()])
    with wrap(base, provider) as session:
        session.get("https://cdn.example/original?old=signature").close()
    assert provider.calls == ["cdn.example"]
    assert base.calls[1][0] == "https://cdn.example/other?sign=unchanged%2F"


def test_redirect_loop_and_limit_close_all_intermediate_responses():
    a, b = Response(302, "/b"), Response(302, "/a")
    base = Base([a, b])
    with wrap(base) as session:
        with pytest.raises(net.StreamNetworkError, match="redirect_loop"):
            session.get("https://cdn.example/a")
    assert a.closed == b.closed == base.closed == 1
    responses = [Response(302, "/" + str(n)) for n in range(5)]
    base = Base(responses)
    with wrap(base, max_redirects=2) as session:
        with pytest.raises(net.StreamNetworkError, match="redirect_limit"):
            session.get("https://cdn.example/start")
    assert len(base.calls) == 3 and [r.closed for r in responses] == [1, 1, 1, 0, 0]


def test_redirect_dns_failure_and_private_literal_fail_closed():
    class FailsSecond(Provider):
        def resolve(self, host):
            if host == "new.example":
                raise RuntimeError("do-not-log-private-output")
            return super().resolve(host)
    first = Response(302, "https://new.example/a?secret=value")
    base = Base([first])
    with wrap(base, FailsSecond()) as session:
        with pytest.raises(net.StreamNetworkError) as error:
            session.get("https://old.example/a")
        assert str(error.value) == "uplink_dns_resolution_failed"
    assert first.closed == base.closed == 1 and len(base.calls) == 1
    first = Response(302, "http://198.18.0.11/a")
    base = Base([first])
    with wrap(base) as session:
        with pytest.raises(net.StreamNetworkError, match="ip_not_public"):
            session.get("https://cdn.example/a")
    assert first.closed == 1 and len(base.calls) == 1


@pytest.mark.parametrize("url", ["https://43.1.2.3/a?sign=x", "https://[2606:4700:4700::1111]/a"])
def test_public_ip_literal_does_not_query_dns(url):
    provider = Provider()
    base = Base()
    with wrap(base, provider) as session:
        session.get(url).close()
    assert provider.calls == [] and RESOLVE not in base.calls[0][2]
    assert base.calls[0][0] == url


@pytest.mark.parametrize("url", [
    "file:///private", "https://user:secret@cdn.example/a", "https://cdn.example:0/a",
    "https://cdn.example:65536/a", "https://[broken/a", "https://cdn.example/a\nsecret",
])
def test_invalid_urls_fail_before_dns_and_http(url):
    provider, base = Provider(), Base()
    with wrap(base, provider) as session:
        with pytest.raises(net.StreamNetworkError, match="invalid_stream_url"):
            session.get(url)
    assert provider.calls == base.calls == []


@pytest.mark.parametrize("hostname", ["2130706433", "127.1", "0177.0.0.1", "0x7f000001", "0x7f.0.1"])
def test_legacy_numeric_ip_forms_cannot_pose_as_dns_names(hostname):
    provider, base = Provider(), Base()
    with wrap(base, provider) as session:
        with pytest.raises(net.StreamNetworkError, match="noncanonical_stream_ip"):
            session.get("http://" + hostname + "/media")
    assert provider.calls == base.calls == []


def test_session_close_is_idempotent_and_closes_pending_final_response():
    base = Base()
    session = wrap(base)
    response = session.get("https://cdn.example/a")
    session.close()
    response.close()
    session.close()
    assert base.closed == response.closed == 1
    with pytest.raises(net.StreamNetworkError, match="session_closed"):
        session.get("https://cdn.example/a")


def test_wrap_initialization_failure_closes_raw_session():
    base = Base()
    with pytest.raises(ValueError):
        wrap(base, max_redirects=100)
    assert base.closed == 1


def test_provider_runs_bounded_hidden_dns_worker():
    calls = []
    def runner(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout=json.dumps({"records": [
            {"address": "43.1.2.3", "ttl": 42}]}).encode())
    provider = net.WindowsDnsProvider(runner=runner)
    assert provider.resolve("cdn.example") == [net.AddressRecord("43.1.2.3", 42)]
    args, kwargs = calls[0]
    worker = Path(net.__file__).with_name("stream_dns_worker.py")
    assert args == [sys.executable, "-B", "-E", "-s", str(worker), "cdn.example"]
    assert Path(kwargs["cwd"]) == Path(net.__file__).resolve().parents[1]
    assert kwargs["timeout"] == 15 and kwargs["creationflags"] == net.NO_WINDOW
    assert kwargs["capture_output"] and "shell" not in kwargs



def test_provider_keeps_a_complete_public_answer_when_a_later_lookup_times_out():
    def runner(*_args, **_kwargs):
        output = (b'\xef\xbb\xbf{"records":[{"address":"43.1.2.3","ttl":42}]}\r\n'
                  b'{"records":[{"address":"2606:')
        raise subprocess.TimeoutExpired("dns_worker", 15, output=output)

    provider = net.WindowsDnsProvider(runner=runner)
    assert provider.resolve("cdn.example") == [net.AddressRecord("43.1.2.3", 42)]


@pytest.mark.parametrize("output", [
    b'{"records":[{"address":"43.1.2.3","ttl":42}]}',
    b'{"records":[{"address":"198.18.0.11","ttl":42}]}\n',
])
def test_provider_requires_a_complete_public_answer_to_recover_from_timeout(output):
    def runner(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("dns_worker", 15, output=output)

    provider = net.WindowsDnsProvider(runner=runner)
    with pytest.raises(net.StreamNetworkError, match="^uplink_dns_query_timeout$"):
        provider.resolve("cdn.example")


@pytest.mark.parametrize(("mode", "detail"), [
    ("timeout", "uplink_dns_query_timeout"),
    ("exit", "uplink_dns_query_failed"),
    ("malformed", "uplink_dns_query_failed"),
    ("wrong_shape", "uplink_dns_reply_invalid"),
])
def test_provider_failures_have_safe_reasons(mode, detail):
    def runner(*args, **kwargs):
        if mode == "timeout":
            raise subprocess.TimeoutExpired("private-server", 15, stderr=b"private-output")
        return SimpleNamespace(returncode=1 if mode == "exit" else 0,
                               stdout=b"{}" if mode == "wrong_shape" else b"not-json",
                               stderr=b"private-output")

    provider = net.WindowsDnsProvider(runner=runner)
    with pytest.raises(net.StreamNetworkError) as error:
        provider.resolve("cdn.example")
    assert str(error.value) == detail

    with wrap(provider=provider) as session:
        with pytest.raises(net.StreamNetworkError) as wrapped_error:
            session.get("https://cdn.example/private?sign=do-not-log")
    assert str(wrapped_error.value) == "uplink_dns_resolution_failed"
    assert wrapped_error.value.dns_reason == detail


def test_wrapper_does_not_keep_an_unrecognized_provider_reason():
    def provider(_host):
        raise net.StreamNetworkError("private resolver output")

    with wrap(provider=provider) as session:
        with pytest.raises(net.StreamNetworkError) as error:
            session.get("https://cdn.example/private?sign=do-not-log")
    assert str(error.value) == "uplink_dns_resolution_failed"
    assert error.value.dns_reason is None


def test_connect_error_closes_attached_response_and_keeps_safe_reason():
    response = Response()
    class Fails(Base):
        def get(self, url, **kwargs):
            error = RuntimeError("https://cdn.example/private?sign=do-not-log")
            error.response, error.code = response, 28
            raise error
    base = Fails()
    original = base.curl_options
    with wrap(base) as session:
        with pytest.raises(net.StreamNetworkError) as error:
            session.get("https://cdn.example/private?sign=do-not-log")
        assert str(error.value) == "stream_connect_failed" and error.value.code == 28
        assert base.curl_options is original
    assert response.closed == base.closed == 1


def test_windows_dns_worker_queries_only_configured_servers_and_flushes_records(monkeypatch, capsys):
    from scripts import stream_dns_worker as worker

    resolvers = ["172.16.0.1", "fe80::1%7"]
    calls = []
    monkeypatch.setattr(worker, "configured_uplink_dns", lambda: resolvers)

    def query(host, resolver, record_type):
        calls.append((host, resolver, record_type))
        if record_type == 1:
            return [("43.1.2.3", 20)]
        return [("2606:4700:4700::1111", 30)]

    monkeypatch.setattr(worker, "query_dns", query)
    worker.emit_dns("cdn.example")
    assert calls == [("cdn.example", server, kind) for server in resolvers for kind in (1, 28)]
    batches = [json.loads(line)["records"] for line in capsys.readouterr().out.splitlines()]
    assert batches == [[{"address": "43.1.2.3", "ttl": 20}],
                       [{"address": "2606:4700:4700::1111", "ttl": 30}]] * 2 + [[]]
