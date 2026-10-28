#!/usr/bin/env python3
"""handoff-notes: append-only, sealed updates to a published checkpoint.

A published checkpoint is never edited: its content fingerprint is sealed, so
any edit makes it untrusted. Facts learned after publishing go to a sidecar,
<checkpoint>.notes.jsonl, one JSON object per line:

    {"seq", "created", "session", "terminal", "text", "checkpoint_id", "mac"}

mac = HMAC(local receipt key, MAC_DOMAIN + checkpoint_id, sha256(canonical entry
without mac)): the receipt key, domain-separated from publication receipts, so a
cloned or hand-edited sidecar line is reported as a problem and never delivered
and a note MAC can never stand in for a receipt MAC.

Delivery (prepare/resume) appends the open notes after the body under
NOTES_HEADING. A successor save folds the pending notes into its body once and
then closes the sidecar with a sealed {"closed": true, "by": successor} line;
a note on a closed sidecar is refused with a pointer to the successor.

Auto-close (#162) appends a sealed {"retired": true, "reason": "ticket-done",
"evidence": {...}} line when the checkpoint's ticket is done and its commits
are on origin/main. The ledger then lists the checkpoint as `retired` (closed);
the .md is still never written, and notes remain allowed after a retire.
"""

import hashlib
import json
import os
import re
from datetime import datetime

import handoff_record as record

NOTES_HEADING = '## Updates since checkpoint'
NOTE_TEXT_LIMIT = 16_000      # refuse larger single notes at write time
NOTE_RENDER_BYTES = 2_000     # per-note delivery cap
NOTES_RENDER_BYTES = 8_000    # total delivery cap
SHORTENED = ' [... shortened]'
MAC_DOMAIN = 'handoff-note\0'
IDENT_LIMIT = 200
_UNSAFE_IDENT = re.compile(r'[^A-Za-z0-9._:@-]')


def _ident(value):
    """Session/terminal labels: one line of safe characters, never markup."""
    return _UNSAFE_IDENT.sub('_', str(value or ''))[:IDENT_LIMIT]


def sidecar_path(path):
    return str(path) + '.notes.jsonl'


def _digest(entry):
    payload = {k: v for k, v in entry.items() if k != 'mac'}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _sealed(entry, key):
    entry = dict(entry)
    entry['mac'] = record._mac(key, MAC_DOMAIN + entry['checkpoint_id'], _digest(entry)) if key else ''
    return entry


def _valid(entry, checkpoint_id, key):
    import hmac
    return bool(key and isinstance(entry, dict) and isinstance(entry.get('mac'), str)
                and entry.get('checkpoint_id') == checkpoint_id
                and hmac.compare_digest(entry['mac'], record._mac(key, MAC_DOMAIN + checkpoint_id, _digest(entry))))


def read(path, checkpoint_id):
    """Return dict(notes=[valid open notes oldest-first], closed=None|successor,
    retired=None|first valid retired entry, problems=int, next_seq=int).
    next_seq follows the last valid seq, so an invalid line never makes the
    next genuine note look out of order."""
    result = dict(notes=[], closed=None, retired=None, problems=0, next_seq=0)
    try:
        with open(sidecar_path(path), encoding='utf-8', errors='replace') as f:
            lines = f.read().splitlines()
    except OSError:
        return result
    key = record._receipt_key()
    expected = 0
    for line in lines:
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            result['problems'] += 1
            continue
        if not _valid(entry, checkpoint_id, key) or entry.get('seq') != expected:
            result['problems'] += 1
            continue
        expected += 1
        result['next_seq'] = expected
        if entry.get('closed'):
            result['closed'] = entry.get('by') or ''
            continue
        if entry.get('retired') is True:
            if result['retired'] is None:
                result['retired'] = entry
            continue
        if isinstance(entry.get('text'), str) and entry['text'].strip():
            result['notes'].append(entry)
        else:
            result['problems'] += 1
    return result


def _append(path, entry):
    fd = os.open(sidecar_path(path), os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, 'a+b') as f:
        lead = b''
        if f.seek(0, os.SEEK_END):
            f.seek(-1, os.SEEK_END)
            lead = b'' if f.read(1) == b'\n' else b'\n'  # a hand-appended line without newline
        f.write(lead + (json.dumps(entry, sort_keys=True, ensure_ascii=False) + '\n').encode())
        f.flush()
        os.fsync(f.fileno())


def add(path, checkpoint_id, text, session, terminal, locked):
    """Append one sealed note. `locked` is ledger.locked; the checkpoint itself is never written."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError('Note text is empty.')
    if len(text.encode()) > NOTE_TEXT_LIMIT:
        raise ValueError('Note exceeds %d bytes; keep it to the new fact and its evidence.' % NOTE_TEXT_LIMIT)
    key = record._receipt_key(create=True)
    if not key:
        raise ValueError('Local receipt key unavailable; a note cannot be sealed.')
    with locked(sidecar_path(path)):
        state = read(path, checkpoint_id)
        if state['closed'] is not None:
            return dict(outcome='closed', successor=state['closed'])
        entry = _sealed(dict(seq=state['next_seq'], created=datetime.now().isoformat(timespec='seconds'),
                             session=_ident(session), terminal=_ident(terminal), text=text.strip('\n'),
                             checkpoint_id=checkpoint_id), key)
        _append(path, entry)
    return dict(outcome='noted', seq=entry['seq'], notes=len(state['notes']) + 1)


def close(path, checkpoint_id, successor, locked=None):
    """Seal the sidecar closed once a successor carries its notes. Idempotent."""
    key = record._receipt_key(create=True)
    if not key:
        return False
    from contextlib import nullcontext
    with (locked(sidecar_path(path)) if locked else nullcontext()):  # None: caller holds the lock
        state = read(path, checkpoint_id)
        if state['closed'] is not None:
            return True
        _append(path, _sealed(dict(seq=state['next_seq'], closed=True, by=str(successor),
                                   created=datetime.now().isoformat(timespec='seconds'),
                                   checkpoint_id=checkpoint_id), key))
    return True


def retire(path, checkpoint_id, evidence, locked):
    """Seal the checkpoint retired (ticket done, commits landed). Idempotent:
    returns 'retired', 'already' or 'unsealed' (no local key: nothing written)."""
    key = record._receipt_key(create=True)
    if not key:
        return 'unsealed'
    with locked(sidecar_path(path)):
        state = read(path, checkpoint_id)
        if state['retired'] is not None:
            return 'already'
        _append(path, _sealed(dict(seq=state['next_seq'], retired=True, reason='ticket-done',
                                   evidence=evidence, created=datetime.now().isoformat(timespec='seconds'),
                                   checkpoint_id=checkpoint_id), key))
    return 'retired'


def _clip(text, limit):
    data = text.encode()
    if len(data) <= limit:
        return text
    return data[:max(0, limit - len(SHORTENED.encode()))].decode('utf-8', 'ignore') + SHORTENED


def render(notes):
    """Render notes as a quoted section; note text cannot inject a heading.
    Returns (section_text or '', omitted_count)."""
    if not notes:
        return '', 0
    parts, used, omitted = [], 0, 0
    for entry in notes:
        text = _clip(entry['text'], NOTE_RENDER_BYTES)
        block = '- %s %s:\n%s\n' % (_ident(entry.get('created')), _ident(entry.get('session'))[:8] or 'unknown',
                                     '\n'.join('  > ' + line for line in text.splitlines()))
        if used + len(block.encode()) > NOTES_RENDER_BYTES:
            omitted += 1
            continue
        parts.append(block)
        used += len(block.encode())
    if omitted:
        parts.append('- %d later note(s) omitted for size; read %s.\n' % (omitted, 'the .notes.jsonl sidecar'))
    return '\n' + NOTES_HEADING + '\n' + ''.join(parts), omitted


def digest(state):
    """Stable digest of the delivered notes; binds delivery-receipt reuse."""
    return hashlib.sha256(json.dumps([n['mac'] for n in state['notes']] + [state['closed']]).encode()).hexdigest()
