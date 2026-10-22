#!/usr/bin/env python3
"""handoff-intents: the one store of durable restart intents.

Owns everything both the predecessor (restart_preflight) and the dispatcher
(handoff_dispatch) need to agree on:

  layout      .handoffs/restart/<sha256(request_id)>.json under the canonical root,
              a `.lock` sidecar per record, a `registry` lock, `.observations`
  TTL         one table: pending adoption window, settled retention, receipt age
  states      pending -> refused | retrieved; a refusal may be re-recorded;
              nothing else moves
  validation  status() reads one record strictly; invalid records raise ValueError
              and are preserved for recovery
  commands    the successor adopt command, and the short restart handle: a
              hash-bound locator under ~/.local/state that names one intent

Callers decide *whether* to move a record; this module decides *where* it lives,
*what* a valid record is, and *which* moves exist. It imports neither caller.
Stdlib only.
"""
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import time
from datetime import datetime

import handoff_contract as contract
import handoff_ledger as ledger
import handoff_protocol as protocol
from handoff_record import live as _checkpoint_live

DAY = 86400
TTL = dict(
    pending=7 * DAY,       # a pending restart older than this is never adopted, by any path
    settled=30 * DAY,      # retrieved/refused intents are kept at least this long
    delivery_receipt=7200,  # a successor's delivery receipt must be at most this old
)

STATES = ('pending', 'refused', 'retrieved')
# refused -> refused: record_refusal has always re-recorded a later host refusal reason.
TRANSITIONS = frozenset({('pending', 'refused'), ('pending', 'retrieved'), ('refused', 'refused')})
SETTLED = ('retrieved', 'refused')

ADOPT_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'restart_preflight.py')
RESTART_TOOLS = ('mcp__AgentsRoom-MCP__agents_restart', 'mcp__AgentsRoom_MCP__agents_restart')


# -- layout --------------------------------------------------------------------

def directory(root, create=True):
    """The canonical intent directory; created unless a read-only caller asks not to."""
    path = Path(root).resolve() / '.handoffs' / 'restart'
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def location(root, request_id):
    if not isinstance(request_id, str) or not request_id.strip():
        raise ValueError('A nonempty restart request ID is required.')
    return str(directory(root) / (hashlib.sha256(request_id.encode()).hexdigest() + '.json'))


def registry(location_):
    """The lock that serializes intent creation across checkpoints and callers."""
    return str(Path(location_).parent / 'registry')


def observations(location_):
    return location_[:-len('.json')] + '.observations'


def entries(root):
    """Record files, sorted, without creating anything. FileNotFoundError means none;
    any other OSError propagates so the caller decides whether it blocks."""
    path = directory(root, create=False)
    try:
        names = sorted(os.listdir(path))
    except FileNotFoundError:
        return []
    return [str(path / name) for name in names if name.endswith('.json')]


def locked(location_):
    return ledger.locked(location_)


def write(location_, record):
    protocol.write_json(location_, record)


# -- commands ------------------------------------------------------------------

def adopt_command(root, request_id, script=ADOPT_SCRIPT):
    """The one successor command a restart prompt or dispatcher hands over."""
    return contract.adopt_command(script, root, request_id)


BINDING_KEYS = ('checkpoint_id', 'path', 'request_id', 'root', 'session', 'terminal')


def binding_digest(binding):
    return hashlib.sha256(json.dumps({k: binding[k] for k in BINDING_KEYS},
                                    sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def handle_location(handle):
    if not isinstance(handle, str) or re.fullmatch(r'h-[0-9a-f]{24}', handle) is None:
        raise ValueError('invalid-restart-handle')
    return Path.home() / '.local/state/session-handoff/restart-handles' / (handle + '.json')


def read_locator(location_):
    fd = os.open(location_, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 16384:
            raise ValueError('Restart locator must be a bounded regular file.')
        raw = stream.read(16385)
        if len(raw) > 16384:
            raise ValueError('Restart locator exceeds read bounds.')
        return protocol.decode_state_json(raw.decode('utf-8'))


def publish_handle(binding):
    """Write (or confirm) the locator for `binding`; returns its handle."""
    digest = binding_digest(binding)
    handle = 'h-' + digest[:24]
    path = handle_location(handle)
    locator = dict(root=binding['root'], request_id=binding['request_id'], binding_sha256=digest)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with ledger.locked(str(path)):
        if path.exists():
            if read_locator(path) != locator:
                raise ValueError('restart-handle-collision')
        else:
            protocol.write_json(str(path), locator)
    return handle


def handle_command(handle, script=ADOPT_SCRIPT):
    return contract.handle_command(script, handle)


# -- TTL and states --------------------------------------------------------------

def expired(record, now=None):
    """A pending intent older than TTL['pending'] is never adopted, by any path."""
    if not isinstance(record, dict) or record.get('state') != 'pending':
        return False
    try:
        age = (time.time() if now is None else now) - float(record.get('intent_at') or 0)
    except (TypeError, ValueError):
        return True
    return not math.isfinite(age) or age > TTL['pending']


def settle(record, state, **fields):
    """Move a record along the only allowed edges; callers check their own preconditions first.

    Creation (absent -> pending) and re-recording the same record happen outside this table."""
    if (record.get('state'), state) not in TRANSITIONS:
        raise ValueError('Illegal restart intent transition %s -> %s.' % (record.get('state'), state))
    record.update(fields, state=state)
    return record


# -- validation ------------------------------------------------------------------

def in_flight_mailbox(worker):
    return worker.get('state') == 'in_flight' and worker.get('via') == 'mailbox'


def worker_record_valid(worker):
    """A persisted worker is a durable report, a settled worker without one, or a
    mailbox delegation still in flight (its reply reaches the restarted agent)."""
    report = worker.get('report')
    if isinstance(report, dict) and report:
        return (isinstance(report.get('path'), str) and bool(report['path'])
                and isinstance(report.get('sha256'), str) and len(report['sha256']) == 64
                and all(c in '0123456789abcdef' for c in report['sha256']))
    if report not in (None, {}):
        return False
    return worker.get('settled') is True or in_flight_mailbox(worker)


def transcript_roots():
    home = Path.home()
    roots = [(home / '.claude/projects', 'claude-native-transcript')]
    roots.extend((p / 'sessions', 'codex-native-transcript')
                 for p in (home / '.agentsroom/codex').glob('*'))
    roots.append((Path(os.environ.get('CODEX_HOME') or home / '.codex') / 'sessions',
                  'codex-native-transcript'))
    return [(p.resolve(), source) for p, source in roots]


def confined_transcript(record, transcript):
    path = Path(transcript)
    real = path.resolve()
    source = next((source for root, source in transcript_roots()
                   if real.is_relative_to(root)), None)
    session = record['session']
    if (not source or not re.search(r'(?<![a-zA-Z0-9])' + re.escape(session)
                                   + r'(?![a-zA-Z0-9])', real.stem)
            or real.suffix != '.jsonl'):
        raise ValueError('Transcript must be a predecessor session file under a native sessions root.')
    return real, source


def accepted_event(record, event):
    """Validate the external native event against an already validated binding."""
    try:
        if event.get('type') == 'claude-native-pair':
            call, reply = event['call'], event['reply']
            if call.get('sessionId') != record['session'] or reply.get('sessionId') != record['session']:
                return False
            uses = [c for c in call['message']['content'] if c.get('type') == 'tool_use'
                    and c.get('name') in RESTART_TOOLS]
            results = reply['message']['content']
            completed = datetime.fromisoformat(reply['timestamp'].replace('Z', '+00:00')).timestamp()
            for use in uses:
                for result in results:
                    if (result.get('type') != 'tool_result' or result.get('tool_use_id') != use.get('id')
                            or result.get('is_error', False) is not False):
                        continue
                    content = result.get('content')
                    if isinstance(content, str):
                        content = [dict(type='text', text=content)]
                    native = dict(type='event_msg', payload=dict(type='item_completed',
                        thread_id=record['session'], completed_at_ms=completed * 1000,
                        item=dict(type='McpToolCall', server='AgentsRoom-MCP', tool='agents_restart',
                                  status='completed', arguments=use.get('input'),
                                  result=dict(isError=False, content=content))))
                    if accepted_event(record, native):
                        return True
            return False
        payload = event['payload']
        item = payload['item']
        completed = payload['completed_at_ms']
        prompt = record.get('restart_prompt')
        if (not isinstance(prompt, str) or not prompt
                or event.get('type') != 'event_msg' or payload.get('type') != 'item_completed'
                or payload.get('thread_id') != record['session']
                or item.get('type') != 'McpToolCall' or item.get('server') != 'AgentsRoom-MCP'
                or item.get('tool') != 'agents_restart' or item.get('status') != 'completed'
                or item.get('arguments', {}).get('prompt') != prompt
                or not isinstance(completed, (int, float)) or isinstance(completed, bool)
                or not math.isfinite(completed) or not record['intent_at'] <= completed / 1000 <= time.time()):
            return False
        result = item['result']
        if result.get('isError') is not False:
            return False
        replies = [json.loads(c['text']) for c in result.get('content', []) if c.get('type') == 'text']
        return any(r.get('ok') is True and r.get('scheduled') is True
                   and r.get('agent', {}).get('id') == record['terminal'] for r in replies)
    except (ValueError, TypeError, AttributeError, KeyError, RecursionError):
        return False


def acceptance_valid(record):
    proof = record.get('host_acceptance')
    try:
        return (isinstance(proof, dict) and proof.get('source') in ('codex-native-transcript', 'claude-native-transcript')
                and proof.get('request_id') == record['request_id']
                and isinstance(proof.get('transcript'), str) and bool(proof['transcript'])
                and confined_transcript(record, proof['transcript'])[1] == proof['source']
                and isinstance(proof.get('native_line_sha256'), str)
                and re.fullmatch(r'[0-9a-f]{64}', proof['native_line_sha256']) is not None
                and proof.get('event_sha256') == hashlib.sha256(
                    json.dumps(proof['event'], sort_keys=True).encode()).hexdigest()
                and accepted_event(record, proof['event']))
    except (ValueError, TypeError, KeyError, RecursionError):
        return False


def status(root, request_id):
    """Read validated durable state; only FileNotFound means no intent.

    Existing invalid records raise ValueError and are preserved for recovery.
    """
    try:
        record = protocol.load_state_json(location(root, request_id))
    except FileNotFoundError:
        return None
    required_strings = ('root', 'path', 'session', 'terminal', 'request_id',
                        'checkpoint_id', 'fingerprint')
    valid = (isinstance(record, dict)
             and all(isinstance(record.get(k), str) and record[k].strip()
                     for k in required_strings)
             and record.get('state') in STATES
             and (record.get('host_confirmed') is False
                  or (record.get('host_confirmed') is True and acceptance_valid(record)))
             and isinstance(record.get('workers'), list)
             and isinstance(record.get('intent_at'), (int, float))
             and not isinstance(record.get('intent_at'), bool)
             and math.isfinite(record['intent_at']) and record['intent_at'] > 0)
    if valid:
        valid = (record['root'] == os.path.realpath(root)
                 and record['path'] == os.path.realpath(record['path'])
                 and record['request_id'] == request_id)
    if valid:
        valid = all(len(record[k]) == 64 and all(c in '0123456789abcdef' for c in record[k])
                    for k in ('checkpoint_id', 'fingerprint'))
    if valid:
        workers = record['workers']
        valid = all(isinstance(w, dict) and isinstance(w.get('id'), str)
                    and bool(w['id'].strip()) and worker_record_valid(w)
                    for w in workers)
        if valid:
            valid = len({w['id'] for w in workers}) == len(workers)
    if valid and 'restart_prompt' in record:
        valid = (isinstance(record['restart_prompt'], str)
                 and 0 < len(record['restart_prompt']) <= 20000)
    if valid and record['state'] == 'refused':
        valid = isinstance(record.get('refusal'), str)
    if valid and record['state'] == 'retrieved':
        valid = (isinstance(record.get('successor_session'), str)
                 and bool(record['successor_session'].strip())
                 and record['successor_session'] != record['session']
                 and isinstance(record.get('retrieval_fingerprint'), str)
                 and bool(record['retrieval_fingerprint']))
    if not valid:
        raise ValueError('Invalid persisted restart intent; preserve it and reconcile with the host.')
    return record


def canonical(root, entry):
    """The validated record stored at `entry`, or ValueError when the file does not sit
    at its own request ID's location (a copied or renamed record)."""
    request_id = protocol.load_state_json(str(entry)).get('request_id')
    record = status(root, request_id)
    if record is None or os.path.realpath(entry) != location(root, record['request_id']):
        raise ValueError('Noncanonical restart intent.')
    return record


# -- retention ---------------------------------------------------------------------

def prune_settled(root, now=None, held=()):
    """Delete retrieved/refused intents older than TTL['settled'] whose checkpoint is done.

    Pending intents (expired or not) and invalid records are never touched. Call with
    the registry lock held; each candidate is re-read under its own lock before deletion
    (``held`` names locations the caller already holds; they are skipped, never re-locked).
    Returns the pruned request IDs.
    """
    now = time.time() if now is None else now
    pruned = []
    def eligible(record):
        return (record is not None and record['state'] in SETTLED
                and now - record['intent_at'] > TTL['settled']
                and not _checkpoint_live(root, record['path']))

    held = {os.path.realpath(h) for h in held}
    try:
        listed = entries(root)
    except OSError:
        return pruned  # retention is best-effort; an unreadable directory prunes nothing
    for entry in map(Path, listed):
        if os.path.realpath(entry) in held:
            continue
        try:
            request_id = protocol.load_state_json(str(entry)).get('request_id')
            if os.path.realpath(entry) != location(root, request_id) or not eligible(status(root, request_id)):
                continue
            with open(str(entry) + '.lock', 'a') as lock:
                # Never wait under the registry lock: a busy record is kept for a later prune.
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                try:
                    if not eligible(status(root, request_id)):
                        continue
                    entry.unlink()
                    # Intent writers hold the registry lock this caller holds, so no
                    # new record can be created at this location against the old sidecar.
                    os.unlink(str(entry) + '.lock')
                    pruned.append(request_id)
                finally:
                    fcntl.flock(lock, fcntl.LOCK_UN)
        except (OSError, ValueError, TypeError, AttributeError):
            continue  # unreadable, invalid or busy: preserve it
    return pruned
