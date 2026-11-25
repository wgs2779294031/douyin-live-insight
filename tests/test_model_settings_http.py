"""Real loopback HTTP framing with synthetic auth/data and no model requests."""
from contextlib import contextmanager
import http.client
import json
from pathlib import Path
import socket
import sys
import threading
import time
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import assist_board_server as board
import board_auth as auth

FAKE_KEY = 'SENTINEL_FIXED_FAKE_KEY_NEVER_RETURN'
ROUTES = ['/api/llm/settings', '/api/llm/key/delete', '/api/llm/test']


@pytest.fixture
def server(tmp_path, monkeypatch):
    calls, parsed, reads = [], [], []
    done = threading.Event()
    for name in ['save_provider_config', 'delete_provider_key', 'test_provider_connection']:
        def action(config, data, body, name=name):
            calls.append((name, body))
            return {'ok': True, 'config_saved': True,
                    'sync': {'total': 0, 'synced': 0, 'failed': 0}}
        monkeypatch.setattr(board.board_llm, name, action, raising=False)
    class CountReads:
        def __init__(self, source):self.source = source
        def __getattr__(self, name):return getattr(self.source, name)
        def read1(self, size):
            value = self.source.read1(size)
            reads.append(len(value))
            return value
    class H(board.Handler):
        max_json_body = 1024
        max_rejection_drain = 4096
        body_read_timeout = .25
        rejection_drain_timeout = .4
        auth_db = {'secret': 'synthetic-local-secret', 'users': [{'name': 'tester'}]}
        config_path = str(tmp_path / 'config.json')
        store = SimpleNamespace(data_dir=str(tmp_path), today=lambda:'2026-11-14')
        def setup(self):
            super().setup()
            self.rfile = CountReads(self.rfile)
        def _read_json(self):
            parsed.append(True)
            return super()._read_json()
        def do_POST(self):
            try:return super().do_POST()
            finally:done.set()
    httpd = board.ThreadingHTTPServer(('127.0.0.1', 0), H)
    worker = threading.Thread(target=httpd.serve_forever, kwargs={'poll_interval':.01})
    worker.start()
    port = httpd.server_address[1]
    token = auth.make_token('tester', H.auth_db['secret'])
    cookie = auth.cookie_header(token).split(';')[0]
    yield SimpleNamespace(port=port, cookie=cookie, calls=calls, parsed=parsed,
                          reads=reads, done=done, handler=H)
    httpd.shutdown()
    httpd.server_close()
    worker.join(2)
    assert not worker.is_alive()


def headers(server, path, length, *, authorized=True, origin=None, extra=''):
    value = (f'POST {path} HTTP/1.1\r\nHost: 127.0.0.1:{server.port}\r\n'
             f'Content-Type: application/json\r\nContent-Length: {length}\r\n')
    if authorized:value += f'Cookie: {server.cookie}\r\n'
    if origin:value += f'Origin: {origin}\r\n'
    return (value + extra + '\r\n').encode()


def read_response(sock, first=b''):
    raw = first
    while b'\r\n\r\n' not in raw:
        value = sock.recv(4096)
        assert value
        raw += value
    head, body = raw.split(b'\r\n\r\n', 1)
    length = next(int(line.split(b':',1)[1]) for line in head.split(b'\r\n')
                  if line.lower().startswith(b'content-length:'))
    while len(body) < length:
        value = sock.recv(4096)
        assert value
        body += value
    return int(head.split()[1]), json.loads(body[:length])


@pytest.mark.parametrize('path', ROUTES + ['/api/llm/gate'])
@pytest.mark.parametrize('denial', ['missing_auth', 'cross_site'])
def test_rejection_survives_body_sent_after_response_headers(server, path, denial):
    """The old handler closed here before the body; no retry/coalescing workaround."""
    payload = json.dumps({'provider':'synthetic','api_key':FAKE_KEY}).encode()
    with socket.create_connection(('127.0.0.1', server.port), timeout=2) as sock:
        sock.sendall(headers(server,path,len(payload),
                             authorized=denial != 'missing_auth',
                             origin='https://cross-site.invalid' if denial=='cross_site' else None))
        response = sock.recv(4096)
        assert response.startswith(b'HTTP/1.0 40')
        # Response is available, but reads must stay open for the bounded upload.
        assert not server.done.is_set()
        time.sleep(.03)
        sock.sendall(payload[:7])
        time.sleep(.02)
        sock.sendall(payload[7:])
        code, body = read_response(sock, response)
        assert code == (401 if denial=='missing_auth' else 403)
        assert FAKE_KEY not in json.dumps(body)
        assert server.done.wait(1)
    assert server.calls == server.parsed == []
    assert sum(server.reads) == len(payload)


@pytest.mark.parametrize('path', ROUTES)
def test_authorized_fragmented_json_calls_business_once(server, path):
    payload = json.dumps({'provider':'synthetic','api_key':FAKE_KEY}).encode()
    with socket.create_connection(('127.0.0.1',server.port),timeout=2) as sock:
        sock.sendall(headers(server,path,len(payload)))
        for part in [payload[:1],payload[1:19],payload[19:]]:
            sock.sendall(part)
            time.sleep(.02)
        code, body = read_response(sock)
    assert code==200 and body['ok']
    assert len(server.calls)==len(server.parsed)==1
    assert sum(server.reads)==len(payload)


@pytest.mark.parametrize('payload',[b'{broken', b'\xff', b'[]', b'null'])
def test_bad_json_has_zero_business_effect(server,payload):
    with socket.create_connection(('127.0.0.1',server.port),timeout=2) as sock:
        sock.sendall(headers(server,ROUTES[0],len(payload))+payload)
        assert read_response(sock)[0]==400
    assert server.calls==[]


@pytest.mark.parametrize('length,extra',[
    ('not-a-number',''),('-1',''),('2','Content-Length: 2\r\n'),
    ('2','Transfer-Encoding: chunked\r\n')])
def test_ambiguous_or_invalid_framing_is_rejected(server,length,extra):
    with socket.create_connection(('127.0.0.1',server.port),timeout=2) as sock:
        sock.sendall(headers(server,ROUTES[0],length,extra=extra))
        assert read_response(sock)[0]==400
    assert server.calls==[]


def test_oversized_body_is_drained_within_transport_limit_without_business(server):
    payload=b'x'*2048
    with socket.create_connection(('127.0.0.1',server.port),timeout=2) as sock:
        sock.sendall(headers(server,ROUTES[0],len(payload)))
        first=sock.recv(4096)
        sock.sendall(payload[:1000]);sock.sendall(payload[1000:])
        assert read_response(sock,first)[0]==413
        assert server.done.wait(1)
    assert server.calls==[] and sum(server.reads)==len(payload)


def test_huge_declared_body_cannot_hold_rejection_open_forever(server):
    with socket.create_connection(('127.0.0.1',server.port),timeout=2) as sock:
        start=time.monotonic()
        sock.sendall(headers(server,ROUTES[0],2_000_000_000))
        assert read_response(sock)[0]==413
        assert server.done.wait(1)
        assert time.monotonic()-start<1.2
    assert sum(server.reads)<=server.handler.max_rejection_drain
    assert server.calls==[]


def test_partial_body_times_out_before_any_business(server):
    with socket.create_connection(('127.0.0.1',server.port),timeout=2) as sock:
        start=time.monotonic()
        sock.sendall(headers(server,ROUTES[0],30)+b'{')
        assert read_response(sock)[0]==408
        assert time.monotonic()-start<1.2
    assert server.calls==[]


def test_short_body_eof_is_400_without_business(server):
    with socket.create_connection(('127.0.0.1',server.port),timeout=2) as sock:
        sock.sendall(headers(server,ROUTES[0],30)+b'{')
        sock.shutdown(socket.SHUT_WR)
        assert read_response(sock)[0]==400
    assert server.calls==[]


def test_exceptions_never_echo_key_or_exception_details(server,monkeypatch,capsys):
    def fail(*_args):raise RuntimeError(FAKE_KEY)
    monkeypatch.setattr(board.board_llm,'save_provider_config',fail)
    conn=http.client.HTTPConnection('127.0.0.1',server.port,timeout=2)
    conn.request('POST',ROUTES[0],json.dumps({'provider':'synthetic','api_key':FAKE_KEY}),
                 {'Cookie':server.cookie,'Content-Type':'application/json'})
    response=conn.getresponse();body=response.read();conn.close()
    assert response.status==500 and FAKE_KEY.encode() not in body
    captured=capsys.readouterr()
    assert FAKE_KEY not in captured.out+captured.err


@pytest.mark.parametrize('action,result,status',[
    ('save_provider_config',{'ok':False,'config_saved':True,'code':'partial_sync','error':'同步未完成'},503),
    ('save_provider_config',{'ok':False,'code':'invalid','error':'参数无效'},400),
    ('test_provider_connection',{'ok':False,'code':'auth_failed','message':'模型认证失败'},200),
])
def test_safe_result_status_distinguishes_board_and_upstream_auth(server,monkeypatch,action,result,status):
    monkeypatch.setattr(board.board_llm,action,lambda *_args:result)
    conn=http.client.HTTPConnection('127.0.0.1',server.port,timeout=2)
    conn.request('POST',ROUTES[2] if action=='test_provider_connection' else ROUTES[0],
                 '{}',{'Cookie':server.cookie,'Content-Type':'application/json'})
    response=conn.getresponse();body=json.loads(response.read());conn.close()
    assert response.status==status and body==result
