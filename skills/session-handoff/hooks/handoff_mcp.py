#!/usr/bin/env python3
"""handoff-mcp: a read-only AgentsRoom MCP client for the resume staleness verdict.

Speaks just enough Streamable HTTP MCP (initialize, notifications/initialized,
tools/call; JSON or SSE answers) to ask backlog_get about one ticket.

Safety rules, each tested:
  - only tools in READ_TOOLS can be called; anything else is PermissionError
    before a byte leaves the machine;
  - the URL comes from the `AgentsRoom-MCP` entry of .mcp.json (the checkout,
    then the main checkout of a linked worktree) and must be loopback, so a
    repository-supplied .mcp.json cannot send the token elsewhere;
  - the Bearer token is AGENTSROOM_MCP_SESSION from the environment, never
    the file's header value; no proxies, no redirects;
  - error messages never carry headers, bodies or the token.

Every failure is staleness.BacklogUnavailable, so the verdict falls back to
git-only. Stdlib only.
"""

import json
import os
import socket
import urllib.error
import urllib.parse
import urllib.request

import handoff_staleness as staleness
import handoff_workspace as workspace

SERVER_KEY = 'AgentsRoom-MCP'
TOKEN_ENV = 'AGENTSROOM_MCP_SESSION'
READ_TOOLS = frozenset({'backlog_get'})
LOOPBACK = frozenset({'127.0.0.1', 'localhost', '::1'})
PROTOCOL_VERSION = '2025-06-18'
TIMEOUT = 3.0
CLOSED_STATUSES = frozenset({'done', 'wontfix', 'wont_fix', 'cancelled', 'canceled'})
NOT_FOUND_MARKERS = ("not in this project's backlog", 'not found', 'no such task')

Unavailable = staleness.BacklogUnavailable


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # urllib then raises the 3xx as an HTTPError


_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


def config_paths(root):
    """.mcp.json in the checkout, then in the main checkout when ROOT is a linked worktree."""
    paths = [os.path.join(root, '.mcp.json')]
    try:
        _, common = workspace.linked_common(root)
    except OSError:
        common = None
    if common and os.path.basename(common) == '.git':
        paths.append(os.path.join(os.path.dirname(common), '.mcp.json'))
    return paths


def server_url(root):
    for path in config_paths(root):
        try:
            with open(path, encoding='utf-8') as stream:
                config = json.load(stream)
        except FileNotFoundError:
            continue
        except (OSError, ValueError):
            raise Unavailable('.mcp.json is unreadable')
        entry = (config.get('mcpServers') or {}).get(SERVER_KEY) if isinstance(config, dict) else None
        url = entry.get('url') if isinstance(entry, dict) else None
        if isinstance(url, str) and url:
            return url
    raise Unavailable('no %s server in .mcp.json' % SERVER_KEY)


def check_loopback(url):
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ('http', 'https') or '@' in parts.netloc or parts.hostname not in LOOPBACK:
        raise Unavailable('%s URL is not a loopback http address; refusing to send the token' % SERVER_KEY)
    return url


class Client:
    def __init__(self, url, token, timeout=TIMEOUT):
        self._url, self._token, self._timeout = check_loopback(url), token, timeout
        self._session = None
        self._ready = False
        self._next_id = 0

    def _post(self, message):
        headers = {'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream',
                   'Authorization': 'Bearer ' + self._token}
        if self._session:
            headers['Mcp-Session-Id'] = self._session
        if self._ready:
            headers['MCP-Protocol-Version'] = PROTOCOL_VERSION
        request = urllib.request.Request(self._url, data=json.dumps(message).encode(), headers=headers, method='POST')
        try:
            with _OPENER.open(request, timeout=self._timeout) as response:
                self._session = response.headers.get('Mcp-Session-Id') or self._session
                kind = response.headers.get('Content-Type', '')
                body = response.read(4 * 1024 * 1024).decode('utf-8', 'replace')
        except urllib.error.HTTPError as exc:
            raise Unavailable('AgentsRoom MCP answered HTTP %d' % exc.code) from None
        except (urllib.error.URLError, socket.timeout, ConnectionError, OSError):
            raise Unavailable('AgentsRoom MCP is not reachable') from None
        if 'id' not in message:
            return None
        return _pick(_messages(body, kind), message['id'])

    def _request(self, method, params):
        self._next_id += 1
        reply = self._post({'jsonrpc': '2.0', 'id': self._next_id, 'method': method, 'params': params})
        if 'error' in reply:
            raise Unavailable('AgentsRoom MCP %s failed (code %s)' % (method, (reply['error'] or {}).get('code')))
        return reply.get('result') or {}

    def _initialize(self):
        if self._ready:
            return
        self._request('initialize', {'protocolVersion': PROTOCOL_VERSION, 'capabilities': {},
                                     'clientInfo': {'name': 'session-handoff-kit', 'version': '1'}})
        self._ready = True
        self._post({'jsonrpc': '2.0', 'method': 'notifications/initialized'})

    def call(self, tool, arguments):
        """tools/call for an allow-listed read tool; returns the MCP result object."""
        if tool not in READ_TOOLS:
            raise PermissionError('%r is not an allow-listed read tool' % tool)
        self._initialize()
        return self._request('tools/call', {'name': tool, 'arguments': arguments})


def _messages(body, kind):
    if 'text/event-stream' in kind:
        out, data = [], []
        for line in body.splitlines() + ['']:
            if line.startswith('data:'):
                data.append(line[5:].lstrip())
            elif not line and data:
                out.append('\n'.join(data))
                data = []
        texts = out
    else:
        texts = [body]
    parsed = []
    for text in texts:
        try:
            value = json.loads(text)
        except ValueError:
            continue
        parsed.extend(value if isinstance(value, list) else [value])
    return parsed


def _pick(messages, request_id):
    for message in messages:
        if isinstance(message, dict) and message.get('id') == request_id:
            return message
    raise Unavailable('AgentsRoom MCP sent no answer to the request')


def client_from_config(root, env=None):
    env = os.environ if env is None else env
    token = env.get(TOKEN_ENV, '')
    if not token:
        raise Unavailable('%s is not set' % TOKEN_ENV)
    return Client(server_url(root), token)


def ticket_state(result):
    """'open' | 'closed' | 'missing' from a backlog_get tool result."""
    text = ''.join(c.get('text', '') for c in result.get('content') or () if isinstance(c, dict))
    if result.get('isError'):
        if any(marker in text.lower() for marker in NOT_FOUND_MARKERS):
            return 'missing'
        raise Unavailable('backlog_get returned an error')
    try:
        task = json.loads(text).get('task') or {}
    except (ValueError, AttributeError):
        raise Unavailable('backlog_get answer is not a task') from None
    status = str(task.get('status', '')).lower()
    return 'closed' if task.get('wontFix') is True or status in CLOSED_STATUSES else 'open'


def backlog_lookup(root, env=None):
    """A staleness lookup over one lazily-built client; configuration errors surface on first use."""
    state = {}

    def lookup(ticket):
        if 'client' not in state:
            state['client'] = client_from_config(root, env)
        return ticket_state(state['client'].call('backlog_get', {'taskId': ticket}))

    return lookup
