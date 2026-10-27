"""Read-only AgentsRoom MCP client used by the resume staleness verdict (ticket #99)."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import unittest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'skills/session-handoff/hooks'))
import handoff_mcp as mcp  # noqa: E402
import handoff_staleness as staleness  # noqa: E402

TOKEN = 'sekrit-token-4f2a'
TICKET = '6cf33871-1ff9-4d27-a175-a869d775ee84'


class FakeServer:
    """A Streamable-HTTP MCP endpoint answering backlog_get from `tasks`."""

    def __init__(self, tasks=None, sse=False, fail=None):
        self.tasks, self.sse, self.fail = tasks or {}, sse, fail
        self.requests = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                fake.requests.append((dict(self.headers), body))
                if fake.fail == 'redirect':
                    self.send_response(307)
                    self.send_header('Location', 'http://127.0.0.1:1/elsewhere')
                    self.end_headers()
                    return
                if fake.fail == 'echo':  # an error page that echoes the credentials back
                    data = ('bad request, you sent ' + str(self.headers.get('Authorization'))).encode()
                    self.send_response(500)
                    self.send_header('Content-Length', str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                if 'id' not in body:
                    self.send_response(202)
                    self.send_header('Content-Length', '0')
                    self.end_headers()
                    return
                if body['method'] == 'initialize':
                    result = {'protocolVersion': '2025-06-18', 'capabilities': {}, 'serverInfo': {'name': 'fake'}}
                else:
                    task = fake.tasks.get(body['params']['arguments']['taskId'])
                    if task is None:
                        text = "Task x is not in this project's backlog."
                        result = {'isError': True, 'content': [{'type': 'text', 'text': text}]}
                    else:
                        result = {'content': [{'type': 'text', 'text': json.dumps({'task': task})}]}
                message = json.dumps({'jsonrpc': '2.0', 'id': body['id'], 'result': result})
                if fake.sse:
                    data = ('event: message\ndata: %s\n\n' % message).encode()
                    kind = 'text/event-stream'
                else:
                    data, kind = message.encode(), 'application/json'
                self.send_response(200)
                self.send_header('Content-Type', kind)
                self.send_header('Content-Length', str(len(data)))
                if body['method'] == 'initialize':
                    self.send_header('Mcp-Session-Id', 'sess-1')
                self.end_headers()
                self.wfile.write(data)

        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.url = 'http://127.0.0.1:%d/mcp/app?AR_PROJECT_ID=p1' % self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.realpath(self.tmp.name)
        self.env = {'AGENTSROOM_MCP_SESSION': TOKEN}

    def serve(self, **kw):
        server = FakeServer(**kw)
        self.addCleanup(server.close)
        self.configure(server.url)
        return server

    def configure(self, url, headers=None):
        entry = {'type': 'http', 'url': url, 'headers': headers or {'Authorization': 'Bearer ${AGENTSROOM_MCP_SESSION}'}}
        Path(self.root, '.mcp.json').write_text(json.dumps({'mcpServers': {'AgentsRoom-MCP': entry}}))

    def lookup(self):
        return mcp.backlog_lookup(self.root, self.env)

    def assertUnavailable(self, ticket=TICKET, contains=''):
        with self.assertRaises(staleness.BacklogUnavailable) as raised:
            self.lookup()(ticket)
        self.assertNotIn(TOKEN, str(raised.exception))
        self.assertIn(contains, str(raised.exception))
        return str(raised.exception)

    def test_ticket_states_over_json(self):
        server = self.serve(tasks={TICKET: {'status': 'todo', 'wontFix': False},
                                   'aaaaaaaa': {'status': 'done', 'wontFix': False},
                                   'bbbbbbbb': {'status': 'in_progress', 'wontFix': True}})
        lookup = self.lookup()
        self.assertEqual(lookup(TICKET), 'open')
        self.assertEqual(lookup('aaaaaaaa'), 'closed')
        self.assertEqual(lookup('bbbbbbbb'), 'closed')
        self.assertEqual(lookup('cccccccc'), 'missing')
        headers, first = server.requests[0]
        self.assertEqual(first['method'], 'initialize')
        self.assertEqual(headers['Authorization'], 'Bearer ' + TOKEN)
        calls = [body for _, body in server.requests if body.get('method') == 'tools/call']
        self.assertEqual({c['params']['name'] for c in calls}, {'backlog_get'})
        later = [h for h, body in server.requests[1:]]
        self.assertTrue(all(h.get('Mcp-Session-Id') == 'sess-1' for h in later), later)

    def test_ticket_state_over_sse(self):
        self.serve(sse=True, tasks={TICKET: {'status': 'done'}})
        self.assertEqual(self.lookup()(TICKET), 'closed')

    def test_only_read_tools_can_be_called(self):
        server = self.serve()
        client = mcp.client_from_config(self.root, self.env)
        for tool in ('backlog_update', 'backlog_delete', 'agents_send', ''):
            with self.subTest(tool=tool), self.assertRaises(PermissionError):
                client.call(tool, {'taskId': TICKET})
        self.assertEqual(server.requests, [])

    def test_token_comes_from_env_not_config(self):
        server = self.serve(tasks={TICKET: {'status': 'todo'}})
        self.configure(server.url, headers={'Authorization': 'Bearer from-the-file'})
        self.lookup()(TICKET)
        self.assertEqual(server.requests[0][0]['Authorization'], 'Bearer ' + TOKEN)

    def test_missing_token_is_unavailable_without_connecting(self):
        server = self.serve()
        self.env = {}
        self.assertUnavailable(contains='AGENTSROOM_MCP_SESSION')
        self.assertEqual(server.requests, [])

    def test_non_loopback_url_is_refused(self):
        for url in ('http://example.com/mcp', 'http://10.0.0.5:54694/mcp', 'https://127.0.0.1.evil.test/mcp',
                    'file:///etc/passwd', 'http://user@127.0.0.1@evil.test/'):
            with self.subTest(url=url):
                self.configure(url)
                self.assertUnavailable(contains='loopback')

    def test_missing_config_is_unavailable(self):
        self.assertUnavailable(contains='.mcp.json')

    def test_unreachable_server_is_unavailable(self):
        self.configure('http://127.0.0.1:%d/mcp/app' % free_port())
        self.assertUnavailable(contains='not reachable')

    def test_error_bodies_never_leak_the_token(self):
        self.serve(fail='echo')
        self.assertUnavailable()

    def test_redirects_are_not_followed(self):
        server = self.serve(fail='redirect')
        self.assertUnavailable()
        self.assertEqual(len(server.requests), 1)

    def test_config_found_in_main_checkout_from_linked_worktree(self):
        import subprocess
        server = self.serve(tasks={TICKET: {'status': 'todo'}})
        env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
        env.update(GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@t', GIT_COMMITTER_NAME='t',
                   GIT_COMMITTER_EMAIL='t@t', GIT_CONFIG_NOSYSTEM='1', HOME=self.root)
        def git(*args):
            subprocess.run(['git', '-C', self.root, *args], env=env, check=True, capture_output=True)
        git('init', '-q', '-b', 'main')
        git('commit', '-q', '--allow-empty', '-m', 'init')
        worker = os.path.join(self.root, 'wt')
        git('worktree', 'add', '-q', worker)
        self.assertEqual(mcp.backlog_lookup(worker, self.env)(TICKET), 'open')
        self.assertTrue(server.requests)


if __name__ == '__main__':
    unittest.main()
