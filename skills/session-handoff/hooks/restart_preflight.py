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
import math

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
             and record.get('host_confirmed') is False
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


def record_refusal(root, request_id, reason):
    """Record a returned host refusal/error. Never closes the checkpoint."""
    location = _location(root, request_id)
    with ledger.locked(location):
        record = status(root, request_id)
        if not record:
            raise ValueError('No restart intent.')
        if record['state'] == 'retrieved':
            raise ValueError('Successor receipt already recorded.')
        record.update(state='refused', refusal=str(reason), host_confirmed=False)
        protocol.write_json(location, record)
        return record


def confirm_successor(root, request_id, session, terminal=None, delivery_receipt=None):
    """Validate a real local preparation receipt, not an agent's claim to read.

    This proves kit retrieval only. Host process replacement and agent semantic
    understanding remain unproven; host_confirmed is deliberately always false.
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
        if (receipt.get('token') != delivery_receipt or not delivery_receipt
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
        record.update(state='retrieved', successor_session=session,
                      retrieval_fingerprint=receipt['fingerprint'], host_confirmed=False)
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
    if not terminal or terminal != record['terminal'] or session == record['session']:
        return blocked('identity', 'successor-identity-mismatch')
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
                    state=record['state'], host_confirmed=False)
    report = ('Observed identity %s -> %s on terminal %s (hook/host recorded). Host request '
              'acceptance is not observed: host_confirmed=false.' % (record['session'], session, terminal))
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
    holds = dispatch.applicable_holds(dispatch.read_holds(root), fm.get('topic'))
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
    parser.add_argument('operation', choices=['preflight', 'status', 'refusal', 'successor', 'adopt', 'restart'])
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
                    'refusal': record_refusal, 'successor': confirm_successor}[args.operation]
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
