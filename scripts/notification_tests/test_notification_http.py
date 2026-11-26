"""Real loopback HTTP guards with owned fixtures and a fake Feishu sender."""
import http.client
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'scripts')]
with patch.dict(os.environ, {k: v for k, v in os.environ.items() if not k.startswith('DY_CUSTOMER')}, clear=True):
    from scripts import local_app
    import assist_board_server as board
from scripts import notify

WEBHOOK = 'https://open.feishu.cn/open-apis/bot/v2/hook/synthetic-http-only-id'


def free_port():
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        return listener.getsockname()[1]


class NotificationHTTPTests(unittest.TestCase):
    def setUp(self):
        base = os.environ.get('DLI_NOTIFICATION_TEST_ROOT')
        if base:
            Path(base).mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix='notification-http-', dir=base)
        self.addCleanup(self.temporary.cleanup)
        work = Path(self.temporary.name)
        data, dist = work / 'data', work / 'dist'
        data.mkdir()
        dist.mkdir()
        (dist / 'index.html').write_text('<title>synthetic notification fixture</title>', encoding='utf-8')
        self.config = work / 'config.json'
        self.config.write_text(json.dumps({'rooms': [], 'llm': {'enabled': False}, 'feishu': {'enabled': False, 'backend': 'dry_run'}}), encoding='utf-8')
        roster = work / 'anchors.json'
        roster.write_text('{"version":1,"anchors":[]}', encoding='utf-8')
        self.port = free_port()
        context = local_app.context_from_args(SimpleNamespace(root=str(ROOT), data=str(data), config=str(self.config),
            roster=str(roster), dist=str(dist), python=sys.executable, port=self.port, cdp_port=free_port()))
        self.manager = SimpleNamespace(stopping=False, lock=threading.RLock(), inflight=0)
        # The test exercises the real HTTP handler, not background data readers.
        with patch.object(board.Store, 'poll_loop', return_value=None), patch.object(board.broadcast.Broadcaster, 'start', return_value=None):
            self.server, self.thread, self.broadcaster = local_app.create_board(context, self.manager, {'pid': 0, 'created': 0})
        self.addCleanup(self.stop)
        self.headers = {}

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)
        self.assertFalse(self.thread.is_alive())
        self.assertEqual(self.manager.inflight, 0)

    def request(self, method, path='/api/notifications/feishu', body=None, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=3)
        merged = {'Content-Type': 'application/json', **self.headers, **(headers or {})}
        raw = None if body is None else body if isinstance(body, str) else json.dumps(body)
        try:
            connection.request(method, path, body=raw, headers=merged)
            response = connection.getresponse()
            return response.status, json.loads(response.read()), response.getheader('Set-Cookie')
        finally:
            connection.close()

    def session(self):
        code, body, cookie = self.request('POST', '/api/local/session', {})
        self.assertEqual(code, 200)
        self.headers = {'Cookie': cookie.split(';', 1)[0], 'X-Local-Write-Token': body['write_token']}

    def test_session_and_cross_origin_guards_run_before_notification(self):
        with patch.object(notify, 'send_result') as sender:
            self.assertEqual(self.request('GET')[0], 401)
            self.assertEqual(self.request('POST', body={'action': 'test'})[0], 401)
            self.session()
            self.assertEqual(self.request('POST', body={'action': 'test'}, headers={'Origin': 'https://unrelated.invalid'})[0], 403)
            self.assertEqual(self.request('POST', body={'action': 'test'}, headers={'X-Local-Write-Token': 'wrong'})[0], 403)
            self.assertEqual(self.request('GET', headers={'Host': 'unrelated.invalid'})[0], 403)
            sender.assert_not_called()

    def test_save_is_private_and_only_explicit_test_sends(self):
        self.session()
        with patch.object(notify, 'send_result', return_value={'code': 'delivered', 'delivered': True}) as sender:
            code, saved, _ = self.request('POST', body={'action': 'save', 'enabled': True, 'transport': 'webhook', 'webhook_url': WEBHOOK, 'signing_secret': 'synthetic-http-secret'})
            self.assertEqual(code, 200)
            self.assertTrue(saved['ok'])
            sender.assert_not_called()
            code, status, _ = self.request('GET')
            self.assertEqual(code, 200)
            self.assertNotIn(WEBHOOK, json.dumps(status))
            self.assertNotIn('synthetic-http-secret', json.dumps(status))
            self.assertEqual(self.request('POST', body={'action': 'test', 'webhook_url': WEBHOOK})[0], 400)
            sender.assert_not_called()
            code, sent, _ = self.request('POST', body={'action': 'test'})
            self.assertEqual(code, 200)
            self.assertTrue(sent['delivered'])
            sender.assert_called_once()

    def test_methods_capacity_and_shutdown_do_not_send(self):
        self.session()
        with patch.object(notify, 'send_result') as sender:
            self.assertEqual(self.request('PATCH', body={'action': 'test'})[0], 405)
            self.assertEqual(self.request('POST', body=' ' * 16385)[0], 413)
            self.manager.stopping = True
            self.assertEqual(self.request('POST', body={'action': 'test'})[0], 503)
            sender.assert_not_called()


if __name__ == '__main__':
    unittest.main()
