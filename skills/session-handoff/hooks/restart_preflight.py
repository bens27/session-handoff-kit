#!/usr/bin/env python3
"""Durable restart intent and retrieval receipt; never launches a host restart.

The host adapter owns capability/identity/worker inventory observations. JSON CLI
inputs are caller assertions, never proof that a host restarted or a model read.
"""
import argparse
import hashlib
import json
import os
import re
import shlex
from pathlib import Path
import sys
import time
import fcntl
import hmac
import math
import stat
from datetime import datetime

import handoff_ledger as ledger
import handoff_protocol as protocol


def _location(root, request_id):
    if not isinstance(request_id, str) or not request_id.strip():
        raise ValueError('A nonempty restart request ID is required.')
    directory = Path(root).resolve() / '.handoffs' / 'restart'
    directory.mkdir(parents=True, exist_ok=True)
    return str(directory / (hashlib.sha256(request_id.encode()).hexdigest() + '.json'))


def adopt_command(root, request_id):
    """The one successor command a restart prompt or dispatcher hands over."""
    return 'python3 %s adopt --root %s --request-id %s' % (
        shlex.quote(os.path.realpath(__file__)), shlex.quote(os.path.realpath(root)),
        shlex.quote(request_id))


INTENT_MAX_AGE = 7 * 86400  # a pending restart older than this is not adopted
SETTLED_RETENTION = 30 * 86400  # retrieved/refused intents kept at least this long


def intent_expired(record, now=None):
    """A pending intent older than INTENT_MAX_AGE is never adopted, by any path."""
    if not isinstance(record, dict) or record.get('state') != 'pending':
        return False
    try:
        age = (time.time() if now is None else now) - float(record.get('intent_at') or 0)
    except (TypeError, ValueError):
        return True
    return not math.isfinite(age) or age > INTENT_MAX_AGE
BINDING_KEYS = ('checkpoint_id', 'path', 'request_id', 'root', 'session', 'terminal')


def binding_command(binding):
    """The successor command that carries the exact binding as its only argument."""
    return 'python3 %s adopt %s' % (
        shlex.quote(os.path.realpath(__file__)),
        shlex.quote(json.dumps({k: binding[k] for k in BINDING_KEYS}, sort_keys=True, separators=(',', ':'))))


def _accepted_event(record, event):
    """Validate the external native event against an already validated binding."""
    try:
        if event.get('type') == 'claude-native-pair':
            call, reply = event['call'], event['reply']
            if call.get('sessionId') != record['session'] or reply.get('sessionId') != record['session']:
                return False
            uses = [c for c in call['message']['content'] if c.get('type') == 'tool_use'
                    and c.get('name') in ('mcp__AgentsRoom-MCP__agents_restart', 'mcp__AgentsRoom_MCP__agents_restart')]
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
                    if _accepted_event(record, native):
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


def _acceptance_valid(record):
    proof = record.get('host_acceptance')
    try:
        return (isinstance(proof, dict) and proof.get('source') in ('codex-native-transcript', 'claude-native-transcript')
                and proof.get('request_id') == record['request_id']
                and isinstance(proof.get('transcript'), str) and bool(proof['transcript'])
                and _confined_transcript(record, proof['transcript'])[1] == proof['source']
                and isinstance(proof.get('native_line_sha256'), str)
                and re.fullmatch(r'[0-9a-f]{64}', proof['native_line_sha256']) is not None
                and proof.get('event_sha256') == hashlib.sha256(
                    json.dumps(proof['event'], sort_keys=True).encode()).hexdigest()
                and _accepted_event(record, proof['event']))
    except (ValueError, TypeError, KeyError, RecursionError):
        return False


def status(root, request_id):
    """Read validated durable state; only FileNotFound means no intent.

    Existing invalid records raise ValueError and are preserved for recovery.
    """
    try:
        record = protocol.load_state_json(_location(root, request_id))
    except FileNotFoundError:
        return None
    required_strings = ('root', 'path', 'session', 'terminal', 'request_id',
                        'checkpoint_id', 'fingerprint')
    valid = (isinstance(record, dict)
             and all(isinstance(record.get(k), str) and record[k].strip()
                     for k in required_strings)
             and record.get('state') in ('pending', 'refused', 'retrieved')
             and (record.get('host_confirmed') is False
                  or (record.get('host_confirmed') is True and _acceptance_valid(record)))
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
                    and bool(w['id'].strip()) and isinstance(w.get('report'), dict)
                    and isinstance(w['report'].get('path'), str)
                    and bool(w['report']['path'])
                    and isinstance(w['report'].get('sha256'), str)
                    and len(w['report']['sha256']) == 64
                    and all(c in '0123456789abcdef' for c in w['report']['sha256'])
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


def _checkpoint_live(root, path):
    """True while a checkpoint is open and authoritative, i.e. an intent still guards it."""
    try:
        if ledger._read_fm(path).get('status') != 'open':
            return False
        # resolve() keeps the caller's root spelling; compare canonical paths, or
        # an open checkpoint under a symlinked root reads as done and loses its guard.
        authoritative = ledger.resolve(path, root).get('authoritative')
        return (authoritative is not None
                and os.path.realpath(authoritative) == os.path.realpath(path))
    except (OSError, ValueError):
        return os.path.exists(path)  # unreadable but present: keep the guard


def prune_settled(root, now=None, held=()):
    """Delete retrieved/refused intents older than SETTLED_RETENTION whose checkpoint is done.

    Pending intents (expired or not) and invalid records are never touched. Call with
    the registry lock held; each candidate is re-read under its own lock before deletion
    (``held`` names locations the caller already holds; they are skipped, never re-locked).
    Returns the pruned request IDs.
    """
    now = time.time() if now is None else now
    pruned = []
    def eligible(record):
        return (record is not None and record['state'] in ('retrieved', 'refused')
                and now - record['intent_at'] > SETTLED_RETENTION
                and not _checkpoint_live(root, record['path']))

    held = {os.path.realpath(h) for h in held}
    for entry in sorted(Path(_location(root, 'x')).parent.glob('*.json')):
        if os.path.realpath(entry) in held:
            continue
        try:
            request_id = protocol.load_state_json(str(entry)).get('request_id')
            if os.path.realpath(entry) != _location(root, request_id) or not eligible(status(root, request_id)):
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


def preflight(root, path, session, terminal, request_id, workers=None,
              host_capable=False, hold=False, observations=None):
    """Persist intent before returning dispatch=True exactly once per request.

    workers=None means inventory unknown. Each worker needs a durable report
    {path, sha256}; an active worker without one blocks. Host mailbox adapters
    must materialize retrieved reports locally before calling. A duplicate intent
    is uncertain after a crash: reconcile with the host, never blindly dispatch.
    """
    def blocked(reason):
        return dict(outcome='blocked', dispatch=False, reason=reason,
                    host_confirmed=False)
    # Native UUIDs, AgentsRoom terminal IDs and deterministic request IDs fit
    # this ASCII token format; arbitrary prose is not a first-message identifier.
    if any(not isinstance(value, str) or re.fullmatch(r'[A-Za-z0-9._:@-]{1,128}', value) is None
           for value in (session, terminal, request_id)):
        return blocked('invalid-identifier')
    root, path = os.path.realpath(root), os.path.realpath(path)
    location = _location(root, request_id)
    binding = dict(root=root, path=path, session=session, terminal=terminal,
                   request_id=request_id)
    # The durable registry lock also covers different checkpoints and callers
    # with different TMPDIRs; the publisher lock alone is temp-directory scoped.
    registry = str(Path(location).parent / 'registry')
    with ledger.locked(ledger.session_note_path(root, 'publisher')), ledger.locked(registry), ledger.locked(path), ledger.locked(location):
        try:
            prune_settled(root, held=(location,))
            previous = status(root, request_id)
        except ValueError:
            return blocked('invalid-persisted-intent')
        if previous is not None:
            if any(previous.get(k) != v for k, v in binding.items()):
                return blocked('request-identity-conflict')
            return dict({k: v for k, v in previous.items() if k != 'restart_prompt'},
                        outcome=previous['state'], dispatch=False)
        if not session or not terminal:
            return blocked('missing-identity')
        try:
            ledger._check_owner(path, session)
        except ledger.ConflictError:
            return blocked('checkpoint-owned-by-another-session')
        text = Path(path).read_text()
        fm = ledger.parse_front_matter(text)
        if (os.path.realpath(fm.get('project', '')) != root or fm.get('session_id') != session
                or (fm.get('terminal') and fm['terminal'] != terminal)):
            return blocked('stale-identity')
        if hold or fm.get('waiting_input') or fm.get('automatic_paused'):
            return blocked('human-hold')
        if fm.get('status') != 'open' or not ledger.publication_valid(path, text, fm):
            return blocked('publication-not-ready')
        if fm.get('project_hooks'):
            workspace = fm.get('workspace') or root
            try:
                baseline = json.loads(fm['project_hooks'])
                current = protocol.project_hook_snapshot(workspace)
                if not isinstance(baseline, dict) or set(baseline) != set(current):
                    return blocked('invalid-hook-baseline')
                for name, digest in current.items():
                    if baseline[name] != digest:
                        return dict(blocked('hooks-changed'), path=os.path.join(workspace, name),
                                    action='Project hook configuration changed since save; resolve host hook approval before a new restart.')
            except (OSError, ValueError, TypeError):
                return blocked('hook-config-unreadable')
        if ledger.resolve(path, root).get('authoritative') != path:
            return blocked('stale-checkpoint')
        # Validate every persisted intent, including legacy request IDs. A
        # different request name must not turn uncertain dispatch into permission.
        try:
            for entry in Path(location).parent.glob('*.json'):
                raw = protocol.load_state_json(entry)
                other = status(root, raw.get('request_id'))
                if other is None or os.path.realpath(entry) != _location(root, other['request_id']):
                    raise ValueError('Noncanonical restart intent.')
                same_checkpoint = (other['path'] == path or
                                   other['checkpoint_id'] == fm.get('checkpoint_id'))
                # An expired intent is never adopted: it only still guards its own checkpoint
                if ((other['state'] == 'pending' and (other['path'] == path or (
                        other['terminal'] == terminal and not intent_expired(other))))
                        or (other['state'] in ('retrieved', 'refused') and same_checkpoint)):
                    return blocked('conflicting-restart-intent')
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            return blocked('invalid-persisted-intent')
        if workers is None:
            return blocked('worker-inventory-unknown')
        if not isinstance(workers, list):
            return blocked('worker-inventory-invalid')
        seen = set()
        for worker in workers:
            if (not isinstance(worker, dict) or not isinstance(worker.get('id'), str)
                    or not worker['id'].strip() or worker['id'] in seen):
                return blocked('worker-inventory-invalid')
            seen.add(worker['id'])
            report = worker.get('report') or {}
            if not isinstance(report, dict):
                return blocked('worker-report-not-durable')
            if report and (not isinstance(report.get('path'), str)
                           or not isinstance(report.get('sha256'), str)):
                return blocked('worker-report-not-durable')
            try:
                digest = hashlib.sha256(Path(report['path']).read_bytes()).hexdigest()
                if digest != report['sha256']:
                    return blocked('worker-report-changed')
            except (OSError, KeyError, TypeError):
                return blocked('worker-report-not-durable')
        if host_capable is not True:
            return blocked('host-capability-absent')
        record = dict(binding, checkpoint_id=fm.get('checkpoint_id'),
                      fingerprint=ledger.content_fingerprint(text), state='pending',
                      host_confirmed=False, workers=workers, intent_at=time.time())
        # Deliver the exact binding inside the one successor command; adopt enforces
        # identity, retrieval, receipt and resume, so the prompt carries no rules.
        prompt = 'Continue the handoff: run exactly ' + binding_command(
            dict(binding, checkpoint_id=record['checkpoint_id']))
        if len(prompt) > 20000:
            return blocked('restart-prompt-too-large')
        record['restart_prompt'] = prompt
        if observations is not None:
            # Under the intent lock and only with a new intent, so the evidence
            # always describes the attempt that produced the dispatchable prompt.
            protocol.write_json(location[:-len('.json')] + '.observations', observations)
        protocol.write_json(location, record)
        return dict(record, outcome='ready', dispatch=True)


TRANSCRIPT_MAX_BYTES = 64 * 1024 * 1024
TRANSCRIPT_MAX_LINE = 2 * 1024 * 1024


def _transcript_roots():
    home = Path.home()
    roots = [(home / '.claude/projects', 'claude-native-transcript')]
    roots.extend((p / 'sessions', 'codex-native-transcript')
                 for p in (home / '.agentsroom/codex').glob('*'))
    roots.append((Path(os.environ.get('CODEX_HOME') or home / '.codex') / 'sessions',
                  'codex-native-transcript'))
    return [(p.resolve(), source) for p, source in roots]


def _confined_transcript(record, transcript):
    path = Path(transcript)
    real = path.resolve()
    source = next((source for root, source in _transcript_roots()
                   if real.is_relative_to(root)), None)
    session = record['session']
    if (not source or not re.search(r'(?<![a-zA-Z0-9])' + re.escape(session)
                                   + r'(?![a-zA-Z0-9])', real.stem)
            or real.suffix != '.jsonl'):
        raise ValueError('Transcript must be a predecessor session file under a native sessions root.')
    return real, source


def _transcript_evidence(record, transcript):
    path = Path(transcript)
    real, source = _confined_transcript(record, transcript)
    session = record['session']
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > TRANSCRIPT_MAX_BYTES:
            raise ValueError('Transcript must be a bounded regular file.')
        calls = {}
        consumed = 0
        evidence = None
        while True:
            line = stream.readline(TRANSCRIPT_MAX_LINE + 1)
            if not line:
                break
            consumed += len(line)
            if len(line) > TRANSCRIPT_MAX_LINE or consumed > TRANSCRIPT_MAX_BYTES:
                raise ValueError('Transcript exceeds read bounds.')
            try:
                event = json.loads(line)
                if source == 'claude-native-transcript':
                    content = event.get('message', {}).get('content', [])
                    if not isinstance(content, list):
                        continue
                    for item in content:
                        if item.get('type') == 'tool_use' and event.get('sessionId') == session:
                            calls[item.get('id')] = event
                    matching = [calls[c.get('tool_use_id')] for c in content
                                if c.get('type') == 'tool_result' and c.get('tool_use_id') in calls]
                    event = next((dict(type='claude-native-pair', call=call, reply=event)
                                  for call in matching if _accepted_event(record,
                                      dict(type='claude-native-pair', call=call, reply=event))), {})
                if _accepted_event(record, event):
                    evidence = dict(source=source, request_id=record['request_id'],
                        transcript=str(real), event_sha256=hashlib.sha256(
                            json.dumps(event, sort_keys=True).encode()).hexdigest(),
                        native_line_sha256=hashlib.sha256(line).hexdigest(),
                        event=event, observed_at=time.time())
            except (ValueError, TypeError, AttributeError, KeyError, RecursionError):
                continue
        return evidence


def _import_automatic(record):
    if record['host_confirmed'] or intent_expired(record):
        return record
    session = record['session']
    # Do not interpolate a session id into a glob: native identity is data.
    for root, _ in _transcript_roots():
        for path in root.rglob('*.jsonl'):
            if session not in path.name:
                continue
            try:
                evidence = _transcript_evidence(record, path)
            except (OSError, ValueError):
                continue
            if evidence:
                record.update(host_confirmed=True, host_acceptance=evidence)
                break
        if record['host_confirmed']:
            break
    return record


def record_acceptance(root, request_id, transcript):
    """Import bounded native scheduling evidence; never dispatch a restart."""
    location = _location(root, request_id)
    with ledger.locked(location):
        record = status(root, request_id)
        if not record or record['state'] == 'refused' or intent_expired(record):
            raise ValueError('No unexpired pending or retrieved restart intent.')
        evidence = _transcript_evidence(record, transcript)
        if evidence is None:
            raise ValueError('No matching native host acceptance; preserve the intent and reconcile without redispatch.')
        record.update(host_confirmed=True, host_acceptance=evidence)
        protocol.write_json(location, record)
        return record


def record_refusal(root, request_id, reason):
    """Record a returned host refusal/error. Never closes the checkpoint."""
    location = _location(root, request_id)
    with ledger.locked(location):
        record = status(root, request_id)
        if not record:
            raise ValueError('No restart intent.')
        if record['state'] == 'retrieved':
            raise ValueError('Successor receipt already recorded.')
        if record['host_confirmed']:
            raise ValueError('Host acceptance already recorded; reconcile without marking a refusal.')
        record.update(state='refused', refusal=str(reason), host_confirmed=False)
        protocol.write_json(location, record)
        return record


def confirm_successor(root, request_id, session, terminal=None, delivery_receipt=None):
    """Validate a real local preparation receipt, not an agent's claim to read.

    This proves kit retrieval only. Host process replacement and agent semantic
    understanding remain unproven; only native acceptance evidence sets host_confirmed.
    Call after read-only prepare; this never claims or acknowledges work.
    """
    location = _location(root, request_id)
    with ledger.locked(location):
        record = status(root, request_id)
        if not record or record['state'] == 'refused':
            raise ValueError('No pending restart intent.')
        # The terminal is the environment's, never a caller claim: a forged stdin cannot adopt a foreign intent
        local = protocol.terminal_id()
        if (session == record['session'] or not session or not local
                or local != record['terminal'] or (terminal is not None and terminal != local)):
            raise ValueError('Successor identity does not match restart intent.')
        if record['state'] == 'retrieved':
            if record['successor_session'] != session:
                raise ValueError('Another successor already confirmed.')
            return record
        if intent_expired(record):
            raise ValueError('Restart intent expired.')
        receipt = protocol.load_state_json(protocol.receipt_path(record['root'], session) + '.delivery')
        token = receipt.get('token')
        if (not isinstance(token, str) or not isinstance(delivery_receipt, str)
                or not hmac.compare_digest(token.encode(), delivery_receipt.encode()) or not delivery_receipt
                or os.path.realpath(receipt.get('path', '')) != record['path']
                or receipt.get('fingerprint') != protocol.snapshot(record['path'])):
            raise ValueError('Missing or stale successor delivery receipt.')
        created = receipt.get('created')
        now = time.time()
        if (not isinstance(created, (int, float)) or not math.isfinite(created)
                or not record.get('intent_at') or created <= record['intent_at']
                or created > now or now - created > 7200):
            raise ValueError('Successor receipt must follow intent and be at most two hours old.')
        references = receipt.get('references')
        if (not isinstance(references, dict) or not all(isinstance(spec, str)
                and isinstance(expected, str) for spec, expected in references.items())):
            raise ValueError('Invalid successor delivery reference fingerprints.')
        for spec, expected in references.items():
            _, content = protocol.reference(record['root'], spec)
            if hashlib.sha256(content.encode()).hexdigest() != expected:
                raise ValueError('Required dependency changed after retrieval.')
        text = Path(record['path']).read_text()
        if ledger.content_fingerprint(text) != record['fingerprint']:
            raise ValueError('Checkpoint changed since restart intent.')
        # Keep the receipt proven here: a later resume rewrites the live .delivery
        # file. Only the token's digest is kept; the token itself grants reuse.
        record = _import_automatic(record)
        record.update(state='retrieved', successor_session=session,
                      retrieval_fingerprint=receipt['fingerprint'],
                      retrieval_receipt=dict(token_sha256=hashlib.sha256(delivery_receipt.encode()).hexdigest(),
                                             path=record['path'], fingerprint=receipt['fingerprint'],
                                             created=created, references=dict(references), confirmed_at=now))
        protocol.write_json(location, record)
        return record


def adopt(root, request_id, binding=None):
    """Successor's single atomic command: identity, retrieval, receipt, resume.

    Identity comes only from hook/host facts and the environment terminal, never
    from caller claims. The body is emitted once, by resume; the internal
    prepare only produces the delivery receipt. Any failure preserves state.
    """
    def blocked(stage, reason):
        return dict(outcome='blocked', stage=stage, reason=str(reason)[:500], host_confirmed=False,
                    action='Preserve state and report; never restart, dispatch another intent or pick another checkpoint.')
    root = os.path.realpath(root)
    info = protocol.session_info(root)
    session, terminal = info['session_id'], protocol.terminal_id()
    if (info['identity_source'] not in ('hook', 'host') or not session
            or info['host_session_id'] not in ('', session)):
        return blocked('identity', 'identity-not-recorded-by-hook-or-host')
    try:
        record = status(root, request_id)
    except ValueError as exc:
        return blocked('intent', exc)
    if record is None or record['state'] == 'refused':
        return blocked('intent', 'no-pending-restart-intent')
    if binding is not None and any(binding.get(k) != record.get(k) for k in BINDING_KEYS):
        return blocked('binding', 'binding-does-not-match-recorded-intent')
    identity_facts = dict(
        expected=dict(predecessor_session=record['session'], terminal=record['terminal'],
                      successor_session='different from predecessor'),
        observed=dict(session=session, terminal=terminal, host_session=info['host_session_id'],
                      source=info['identity_source']))
    try:
        fm = ledger._read_fm(record['path'])
    except FileNotFoundError:
        fm = {}  # Let prepare/resume report a missing checkpoint at its established stage.
    except (OSError, ValueError) as exc:
        return blocked('prepare', exc)
    workspace = fm.get('workspace') or fm.get('project') or root
    missing = not os.path.isdir(workspace)
    workspace_facts = dict(path=workspace, exists=not missing,
                           git=fm.get('workspace_git') or fm.get('git', ''), recovery_root=root)
    if not terminal or terminal != record['terminal'] or session == record['session']:
        return dict(blocked('identity', 'successor-identity-mismatch'), identity=identity_facts,
                    workspace=workspace_facts)
    if missing:
        return dict(blocked('workspace', 'workspace-missing'), identity=identity_facts,
                    workspace=workspace_facts,
                    action=('Preserve the checkpoint. Restore workspace %s from recorded Git position %s, '
                            'or explicitly recover its body from project root %s and reconcile paths before continuing. '
                            'Do not restart or automatically choose another workspace.' % (
                                workspace, workspace_facts['git'] or '(unavailable)', root)))
    if intent_expired(record):
        return blocked('intent', 'restart-intent-expired')
    if record['state'] == 'pending':
        try:
            prepared = protocol.prepare(record['path'], root, session)
        except (OSError, ValueError, TypeError, KeyError, ledger.ConflictError) as exc:
            return blocked('prepare', exc)
        if prepared.get('outcome') != 'retrieved' or prepared.get('path') != record['path']:
            return blocked('prepare', prepared.get('action') or prepared.get('outcome'))
        try:
            record = confirm_successor(root, request_id, session, None, prepared['delivery_receipt'])
        except (OSError, ValueError, TypeError, KeyError) as exc:
            return blocked('successor', exc)
    elif record['successor_session'] != session:
        return blocked('successor', 'another-successor-already-confirmed')
    identity = dict(request_id=request_id, predecessor_session=record['session'],
                    successor_session=session, terminal=terminal,
                    state=record['state'], host_confirmed=record['host_confirmed'])
    report = ('Observed identity %s -> %s on terminal %s (hook/host recorded). %s' % (
        record['session'], session, terminal,
        'Native transcript records host acceptance/scheduling: host_confirmed=true; replacement is a separate observation.'
        if record['host_confirmed'] else 'Host request acceptance is not observed: host_confirmed=false.'))
    # Dispatcher holds gate execution, not retrieval: the receipt above stays recorded.
    import handoff_dispatch as dispatch
    try:
        topic = ledger._read_fm(record['path']).get('topic')
    except (OSError, ValueError):
        topic = None
    try:
        holds = dispatch.applicable_holds(dispatch.read_holds(root), topic)
    except OSError as exc:
        return dict(blocked('hold', exc), restart=identity, report=report)
    if holds:
        return dict(outcome='held', stage='hold', holds=holds, restart=identity, report=report,
                    host_confirmed=False, action='Retrieval is recorded but a dispatcher hold is in force: do '
                    'not resume or execute. Report the hold; after an explicit release rerun this command.')
    try:
        result = protocol.resume(root, session, target=record['path'])
    except (OSError, ValueError, TypeError, KeyError, ledger.ConflictError) as exc:
        result = dict(outcome='blocked', action=str(exc))
    if result.get('outcome') not in ('resumed', 'needs-confirmation'):
        return dict(blocked('resume', result.get('action') or result.get('outcome')), restart=identity, report=report)
    delivered = protocol.inbox_document(root, result)
    if delivered is not result:
        result = delivered
        result['action'] = protocol.INBOX_FIRST + ' ' + result.get('action', '')
    result['restart'], result['report'] = identity, report
    return result


def request_id_for(terminal, checkpoint_id):
    """Deterministic, reusable request ID: never mint a new one to evade state."""
    return 'agentsroom:' + hashlib.sha256((terminal + '\0' + checkpoint_id).encode()).hexdigest()


def restart(root, workers=None, host_capable=False, hold=False, path=None):
    """Predecessor's single command: identity, checkpoint, holds, observations, preflight.

    The caller still supplies what only the host can observe: the complete worker
    inventory (None = unknown, blocks) and the restart capability. Holds are the
    union of the caller's observation and durable dispatcher holds.
    """
    import handoff_dispatch as dispatch

    def blocked(reason):
        return dict(outcome='blocked', dispatch=False, reason=reason, host_confirmed=False,
                    action='Do not call agents_restart. Keep the checkpoint open and report; '
                           'continue manually in a fresh session if needed.')
    root = os.path.realpath(root)
    info = protocol.session_info(root)
    session, terminal = info['session_id'], protocol.terminal_id()
    if (info['identity_source'] not in ('hook', 'host') or not session or not terminal
            or info['host_session_id'] not in ('', session)):
        return blocked('identity-not-recorded-by-hook-or-host')
    if path is None:
        errors = []
        own = [r for r in ledger._records(root, errors) if ledger._is_open(r['fm'])
               and r['fm'].get('session_id') == session
               and r['fm'].get('terminal', terminal) == terminal]
        if errors:
            return blocked('checkpoints-unreadable')
        if len(own) != 1:
            return blocked('no-open-checkpoint' if not own else 'ambiguous-checkpoint')
        path = own[0]['path']
    path = os.path.realpath(path)
    try:
        fm = ledger._read_fm(path)
    except OSError:
        return blocked('checkpoint-unreadable')
    checkpoint_id = fm.get('checkpoint_id') or ''
    if not checkpoint_id:
        return blocked('publication-not-ready')
    request_id = request_id_for(terminal, checkpoint_id)
    try:
        holds = dispatch.applicable_holds(dispatch.read_holds(root), fm.get('topic'))
    except OSError as exc:
        return dict(blocked('hold'), stage='hold', detail=str(exc))
    hold = bool(hold) or bool(holds)
    observations = dict(root=root, path=path, session=session, terminal=terminal,
                        request_id=request_id, workers=workers, host_capable=host_capable is True,
                        hold=hold, holds=holds, identity_source=info['identity_source'],
                        host_session_id=info['host_session_id'], observed_at=time.time())
    result = preflight(root, path, session, terminal, request_id, workers=workers,
                       host_capable=host_capable is True, hold=hold, observations=observations)
    out = dict(outcome=result['outcome'], dispatch=result['dispatch'], request_id=request_id,
               path=path, host_confirmed=False)
    if result['outcome'] == 'ready':
        out.update(call='agents_restart', agent=terminal, restart_prompt=result['restart_prompt'],
                   action='Write the session status, then call agents_restart(prompt=restart_prompt) as the '
                          'final tool action and end the turn with one short line.')
    else:
        out.update({k: result[k] for k in ('reason', 'state') if k in result},
                   action='Do not call agents_restart. Pending means reconcile with host history; '
                          'refused means the host declined this checkpoint\'s deterministic request, so a '
                          'retry needs a new save once the cause is resolved; blocked means keep the '
                          'checkpoint open and report.')
    return out


def cli():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['preflight', 'status', 'refusal', 'successor', 'adopt', 'restart', 'acceptance'])
    parser.add_argument('binding', nargs='?', help='adopt: exact restart binding JSON from the restart prompt')
    parser.add_argument('--input', help='JSON argument object; defaults to stdin')
    parser.add_argument('--root', help='adopt: project root')
    parser.add_argument('--request-id', help='adopt: restart request ID from the binding')
    parser.add_argument('--workers', help='restart: complete worker inventory JSON list; omit when unknown')
    parser.add_argument('--host-capable', action='store_true', help='restart: agents_restart with prompt is available')
    parser.add_argument('--hold', action='store_true', help='restart: a live human hold or needsInput is observed')
    parser.add_argument('--path', help='restart: exact checkpoint when this session has several open')
    args = parser.parse_args()
    try:
        if args.operation == 'adopt':
            if args.binding is not None:
                binding = json.loads(args.binding)
                if not isinstance(binding, dict) or set(binding) != set(BINDING_KEYS):
                    raise ValueError('adopt binding must hold exactly: ' + ', '.join(BINDING_KEYS))
                result = adopt(binding['root'], binding['request_id'], binding)
            elif not args.root or not args.request_id:
                raise ValueError('adopt requires the binding JSON or --root and --request-id.')
            else:
                result = adopt(args.root, args.request_id)
            print(json.dumps(result))
            return 0 if result.get('outcome') == 'resumed' else 1
        if args.operation == 'restart':
            if not args.root:
                raise ValueError('restart requires --root.')
            workers = None if args.workers is None else json.loads(args.workers)
            result = restart(args.root, workers, args.host_capable, args.hold, args.path)
            print(json.dumps(result))
            return 0 if result['outcome'] == 'ready' else 1
        data = protocol.load_state_json(args.input) if args.input else protocol.decode_state_json(sys.stdin.read())
        function = {'preflight': preflight, 'status': status,
                    'refusal': record_refusal, 'successor': confirm_successor,
                    'acceptance': record_acceptance}[args.operation]
        result = function(**data)
        if args.operation == 'successor':
            result = {k: v for k, v in result.items() if k != 'restart_prompt'}
        print(json.dumps(result))
    except (OSError, ValueError, TypeError, KeyError, ledger.ConflictError) as exc:
        print(json.dumps(dict(outcome='blocked', dispatch=False, reason=str(exc),
                              host_confirmed=False)))
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(cli())
