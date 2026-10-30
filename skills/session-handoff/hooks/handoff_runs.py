#!/usr/bin/env python3
"""handoff-runs: registered long-running processes that must survive a handoff (#164).

An agent registers work that outlives its turn (a full test suite, a verifier,
a build) once, with the pid or AgentsRoom commands_* id and its log. One sealed
record per run lives in <root>/.handoffs/runs/<run_id>.json:

    {"run_id", "session", "terminal", "cmd", "pid", "pid_start", "command_id",
     "log", "rc_file", "cwd", "started_at", "abandoned", "mac"}

The kit only OBSERVES a run: os.kill(pid, 0) and `ps -o lstart=`. It never
signals, kills, re-runs or restarts anything. State:

    running       pid alive with the recorded start time
    exited        pid gone or reused (a stale pid never holds), rc from rc_file
    abandoned     the agent released it explicitly with run-abandon
    unobservable  no pid (commands_* only): delivered, never holds a restart

A record whose MAC fails is counted as a problem and never delivered or trusted.
Stdlib only.
"""
import hashlib
import hmac
import json
import os
import re
import secrets
import subprocess
from datetime import datetime

import handoff_identity
import handoff_record as record

RUNS_HEADING = '## Registered processes'
MAC_DOMAIN = 'handoff-run\0'
FIELD_LIMIT = 2000
RENDER_LIMIT = 20
_RUN_ID = re.compile(r'run-[0-9a-f]{16}')


def _runs_dir(root, create=False):
    return handoff_identity.local_state_dir(root, 'runs', create=create)


def record_path(root, run_id):
    if not _RUN_ID.fullmatch(str(run_id)):
        raise ValueError('Unknown run id %r.' % str(run_id)[:80])
    return os.path.join(_runs_dir(root), run_id + '.json')


def _digest(entry):
    payload = {k: v for k, v in entry.items() if k != 'mac'}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _seal(entry, key):
    entry = dict(entry)
    entry['mac'] = record._mac(key, MAC_DOMAIN + entry['run_id'], _digest(entry))
    return entry


def _valid(entry, key):
    return bool(key and isinstance(entry, dict) and isinstance(entry.get('mac'), str)
                and isinstance(entry.get('run_id'), str) and _RUN_ID.fullmatch(entry['run_id'])
                and hmac.compare_digest(entry['mac'], record._mac(key, MAC_DOMAIN + entry['run_id'], _digest(entry))))


def _start_time(pid):
    """Process start time as ps prints it, or None when the pid is gone."""
    try:
        os.kill(pid, 0)  # signal 0: existence probe only, nothing is delivered
    except ProcessLookupError:
        return None
    except PermissionError:
        pass  # alive, owned by someone else
    try:
        out = subprocess.run(['ps', '-o', 'lstart=', '-p', str(pid)], capture_output=True,
                             text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return ' '.join(out.stdout.split()) or None


def _write(path, entry):
    tmp = '%s.%s.tmp' % (path, secrets.token_hex(4))
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        json.dump(entry, f, sort_keys=True, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _text(value, name):
    if value is None:
        return ''
    if not isinstance(value, str) or len(value) > FIELD_LIMIT:
        raise ValueError('%s must be text of at most %d characters.' % (name, FIELD_LIMIT))
    return value


def register(root, cmd, log, cwd, session, terminal, pid=None, command_id=None, rc_file=None):
    """Record one run. Returns {run_id, state}. Raises ValueError on bad input."""
    log, cmd, cwd = _text(log, 'log'), _text(cmd, 'cmd'), _text(cwd, 'cwd')
    command_id, rc_file = _text(command_id, 'command_id'), _text(rc_file, 'rc_file')
    if not log.strip():
        raise ValueError('A run needs --log: the successor reads the result there.')
    if pid is not None and (isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0):
        raise ValueError('pid must be a positive integer.')
    if pid is None and not command_id.strip():
        raise ValueError('A run needs --pid or --command-id.')
    key = record._receipt_key(create=True)
    if not key:
        raise ValueError('Local receipt key unavailable; a run cannot be sealed.')
    os.makedirs(_runs_dir(root, create=True), mode=0o700, exist_ok=True)
    entry = dict(run_id='run-' + secrets.token_hex(8), session=str(session or ''),
                 terminal=str(terminal or ''), cmd=cmd, pid=pid,
                 pid_start=_start_time(pid) if pid else None, command_id=command_id,
                 log=os.path.abspath(log), rc_file=os.path.abspath(rc_file) if rc_file else '',
                 cwd=os.path.abspath(cwd or '.'), started_at=datetime.now().isoformat(timespec='seconds'),
                 abandoned='')
    _write(record_path(root, entry['run_id']), _seal(entry, key))
    return dict(run_id=entry['run_id'], state=observe(entry)['state'])


def _load(path, key):
    try:
        with open(path, encoding='utf-8') as f:
            entry = json.load(f)
    except (OSError, ValueError):
        return None
    return entry if _valid(entry, key) else None


def abandon(root, run_id, reason):
    """Explicit release: the run stops holding restarts. The process is untouched."""
    key = record._receipt_key(create=True)
    path = record_path(root, run_id)
    entry = _load(path, key)
    if entry is None:
        raise ValueError('Run %s is missing or failed its seal; nothing changed.' % run_id)
    entry = {k: v for k, v in entry.items() if k != 'mac'}
    entry['abandoned'] = (_text(reason, 'reason').strip() or 'abandoned')[:200]
    _write(path, _seal(entry, key))
    return observe(entry)


def _rc(entry):
    if not entry.get('rc_file'):
        return None
    try:
        with open(entry['rc_file'], encoding='utf-8') as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def observe(entry):
    """Delivery view of one record with its current state."""
    view = {k: entry.get(k) for k in ('run_id', 'session', 'terminal', 'cmd', 'pid', 'command_id',
                                      'log', 'cwd', 'started_at')}
    view['abandoned'] = entry.get('abandoned') or ''
    view['rc'] = _rc(entry)
    view['log_missing'] = not os.path.exists(entry.get('log') or '')
    if view['abandoned']:
        view['state'] = 'abandoned'
    elif not entry.get('pid'):
        view['state'] = 'exited' if view['rc'] is not None else 'unobservable'
    else:
        alive = _start_time(entry['pid'])
        view['state'] = 'running' if alive and alive == entry.get('pid_start') else 'exited'
    return view


def _all(root):
    key = record._receipt_key()
    try:
        names = sorted(os.listdir(_runs_dir(root)))
    except (FileNotFoundError, handoff_identity.UntrustedStateDir):
        return [], 0
    valid, bad = [], 0
    for name in names:
        if not name.endswith('.json'):
            continue
        entry = _load(os.path.join(_runs_dir(root), name), key)
        if entry is None or entry['run_id'] + '.json' != name:
            bad += 1
        else:
            valid.append(entry)
    return valid, bad


def problems(root):
    return _all(root)[1]


def for_session(root, session, terminal=None):
    """Observed runs registered by this session (or terminal), oldest first."""
    found = [e for e in _all(root)[0] if e.get('session') == session
             or (terminal and e.get('terminal') == terminal)]
    return [observe(e) for e in sorted(found, key=lambda e: (e.get('started_at') or '', e['run_id']))]


def running(root, session, terminal):
    """Runs that hold a restart: running, registered by this session or terminal."""
    return [r for r in for_session(root, session, terminal) if r['state'] == 'running']


SNAPSHOT_FIELDS = ('run_id', 'session', 'terminal', 'cmd', 'pid', 'pid_start', 'command_id',
                   'log', 'rc_file', 'cwd', 'started_at')


def snapshot(root, session, terminal=None):
    """JSON for a checkpoint's frontmatter: the non-abandoned runs, enough to re-observe them."""
    found = [e for e in _all(root)[0] if not e.get('abandoned') and (e.get('session') == session
             or (terminal and e.get('terminal') == terminal))]
    found.sort(key=lambda e: (e.get('started_at') or '', e['run_id']))
    return json.dumps([{k: e.get(k) for k in SNAPSHOT_FIELDS} for e in found[:RENDER_LIMIT]],
                      sort_keys=True, ensure_ascii=False) if found else ''


def from_snapshot(text):
    """Observed views of a sealed checkpoint's snapshot; malformed entries are skipped."""
    try:
        items = json.loads(text or '[]')
    except (TypeError, ValueError):
        return []
    views = []
    for item in items if isinstance(items, list) else []:
        if not (isinstance(item, dict) and _RUN_ID.fullmatch(str(item.get('run_id')))):
            continue
        entry = {k: item.get(k) for k in SNAPSHOT_FIELDS}
        if not isinstance(entry['pid'], int) or isinstance(entry['pid'], bool) or entry['pid'] <= 0:
            entry['pid'] = None
        entry = {k: (v if k == 'pid' or v is None or isinstance(v, str) else str(v)) for k, v in entry.items()}
        views.append(observe(entry))
    return views


def _one_line(value):
    return re.sub(r'\s+', ' ', str(value or '')).strip()[:300]


def render(views):
    """Quoted section for delivery after the body; field text cannot inject a heading."""
    if not views:
        return ''
    lines = ['', RUNS_HEADING,
             'Read each result from its log before rerunning anything; the kit never re-runs or kills these.']
    for view in views[:RENDER_LIMIT]:
        state = view['state'] + (' rc=%s' % view['rc'] if view['rc'] is not None else '')
        if view['log_missing']:
            state += ', log missing'
        lines.append('- %s: %s' % (view['run_id'], state))
        for label, value in (('cmd', view['cmd']), ('pid', view['pid']), ('command', view['command_id']),
                             ('log', view['log']), ('cwd', view['cwd']), ('started', view['started_at'])):
            if value:
                lines.append('  > %s: %s' % (label, _one_line(value)))
    if len(views) > RENDER_LIMIT:
        lines.append('- %d more run(s) omitted; list them with run-list.' % (len(views) - RENDER_LIMIT))
    return '\n'.join(lines) + '\n'
