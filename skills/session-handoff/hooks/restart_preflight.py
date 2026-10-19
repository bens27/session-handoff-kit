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
              host_capable=False, hold=False):
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
                if ((other['state'] == 'pending' and
                     (other['terminal'] == terminal or other['path'] == path))
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
        # Deliver the exact binding to the next conversation, not a bare lookup
        # phrase that can select other work or acknowledge before recording receipt.
        identity = json.dumps(dict(binding, checkpoint_id=record['checkpoint_id']), sort_keys=True)
        ledger_cli = shlex.quote(os.path.realpath(ledger.__file__))
        restart_cli = shlex.quote(os.path.realpath(__file__))
        prompt = (
            'Continue the handoff using this exact restart recovery binding: ' + identity + '. '
            'Do not restart or dispatch another intent. Read current identity with python3 '
            + ledger_cli + ' session --root ' + shlex.quote(root) + '. '
            'Require identity_source exactly hook or host, an actual session different from predecessor session, the same terminal, '
            'and no conflicting host_session_id; never generate or substitute an identity. '
            'Before any generic resume shortcut, run python3 ' + ledger_cli + ' prepare '
            + shlex.quote(path) + ' --root ' + shlex.quote(root) + '. '
            'Require outcome retrieved and this exact path, then read the full body and required references. '
            'Keep its delivery_receipt in this context. Run python3 ' + restart_cli + ' successor '
            'with stdin JSON containing root and request_id from the binding, the actual successor session, '
            'the same terminal and that delivery_receipt. Require state retrieved; host_confirmed remains false. '
            'Only then run python3 ' + ledger_cli + ' resume ' + shlex.quote(path)
            + ' --root ' + shlex.quote(root) + ' and follow its authorization, holds and returned action. '
            'If identity, preparation, receipt or resume fails, preserve state and report; do not acknowledge '
            'blindly, choose another checkpoint or restart. Report observed predecessor/successor identity '
            'separately from host request acceptance.'
        )
        if len(prompt) > 20000:
            return blocked('restart-prompt-too-large')
        record['restart_prompt'] = prompt
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


def cli():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['preflight', 'status', 'refusal', 'successor'])
    parser.add_argument('--input', help='JSON argument object; defaults to stdin')
    args = parser.parse_args()
    try:
        data = protocol.load_state_json(args.input) if args.input else protocol.decode_state_json(sys.stdin.read())
        function = {'preflight': preflight, 'status': status,
                    'refusal': record_refusal, 'successor': confirm_successor}[args.operation]
        print(json.dumps(function(**data)))
    except (OSError, ValueError, TypeError, KeyError, ledger.ConflictError) as exc:
        print(json.dumps(dict(outcome='blocked', dispatch=False, reason=str(exc),
                              host_confirmed=False)))
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(cli())
