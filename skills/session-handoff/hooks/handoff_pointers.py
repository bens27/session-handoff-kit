#!/usr/bin/env python3
"""Versioned project pointer index for v3 restarts (SPEC 7.3d, slice 2).

A pointer is a derived, body-free index entry at
``<root>/.handoffs/pointers/<binding-sha256>.json``. It is never authority on its
own: eligibility requires the per-user HOME locator, the authoritative intent,
a publication sealed by this machine's existing receipt key, no Git-tracked
pointer/intent/checkpoint and user-scope continuation authorization.
"""
import json
import os
import re
import stat
import time
from pathlib import Path

import handoff_ledger as ledger
import handoff_workspace as workspace
from handoff_record import Record
import handoff_protocol as protocol

VERSION = 1
MAX_BYTES = 16384
FIELDS = ('version', 'checkpoint_id', 'checkpoint_path', 'root', 'workspace', 'session',
          'terminal', 'request_id', 'binding_sha256', 'handle', 'created_at')
OPTIONAL = ('task_id',)
RECOVER = ('Do not resume automatically; continue explicitly with the restart handle '
           '(restart_preflight.py adopt) after confirming the checkpoint.')


class PointerConflict(ValueError):
    pass


def pointer_dir(root):
    return os.path.join(os.path.realpath(root), '.handoffs', 'pointers')


def _safe_dir(path, private):
    """Refuse a symlinked or foreign-owned index directory. ``.handoffs`` follows the
    umask (0775 under user-private groups), so only other-write is refused there;
    ``.handoffs/pointers`` is the trust anchor and must be private."""
    info = os.lstat(path)
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & (0o077 if private else 0o002)):
        raise ValueError('unsafe-pointer-dir')


def _check_dirs(root, create=False):
    handoffs = os.path.join(os.path.realpath(root), '.handoffs')
    directory = pointer_dir(root)
    _safe_dir(handoffs, private=False)
    if create and not os.path.lexists(directory):
        os.mkdir(directory, 0o700)
    _safe_dir(directory, private=True)
    return directory


def _workspace_exists(path):
    return os.path.isdir(path)


def pointer_path(root, digest):
    if not isinstance(digest, str) or re.fullmatch(r'[0-9a-f]{64}', digest) is None:
        raise ValueError('invalid-binding-digest')
    return os.path.join(pointer_dir(root), digest + '.json')


def build(record, workspace=None, task_id=None):
    """Pointer content for a validated intent record (binding fields equal the intent)."""
    data = dict(version=VERSION, checkpoint_id=record['checkpoint_id'],
                checkpoint_path=record['path'], root=record['root'],
                workspace=os.path.realpath(workspace or record['root']),
                session=record['session'], terminal=record['terminal'],
                request_id=record['request_id'], binding_sha256=record['binding_sha256'],
                handle='h-' + record['binding_sha256'][:24], created_at=time.time())
    if isinstance(task_id, str) and task_id.strip():
        data['task_id'] = task_id
    return data


def _same(a, b):
    return {k: v for k, v in a.items() if k != 'created_at'} == {
        k: v for k, v in b.items() if k != 'created_at'}


def write_pointer(root, data):
    """Atomically write (fsync) a private pointer; identical retries are no-ops, conflicts refuse."""
    location = pointer_path(root, data['binding_sha256'])
    _check_dirs(root, create=True)
    with ledger.locked(location):
        if os.path.lexists(location):
            if not _same(read_pointer(location), data):
                raise PointerConflict('pointer-conflict')
            return location
        protocol.write_json(location, data)  # mkstemp: 0600
    return location


def read_pointer(location):
    """Bounded regular private file, no symlink/FIFO following, schema-validated."""
    fd = os.open(location, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
            raise ValueError('pointer-not-regular')
        if info.st_mode & 0o077 or info.st_uid != os.getuid():
            raise ValueError('pointer-not-private')
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError('pointer-too-large')
    data = protocol.decode_state_json(raw.decode('utf-8'))
    if (not isinstance(data, dict) or data.get('version') != VERSION
            or not set(FIELDS) <= set(data) or set(data) - set(FIELDS) - set(OPTIONAL)):
        raise ValueError('pointer-schema')
    if not all(isinstance(data[k], str) and data[k] for k in FIELDS if k not in ('version', 'created_at')):
        raise ValueError('pointer-schema')
    if type(data['created_at']) not in (int, float):
        raise ValueError('pointer-schema')
    if (os.path.basename(location) != data['binding_sha256'] + '.json'
            or data['handle'] != 'h-' + data['binding_sha256'][:24]):
        raise ValueError('pointer-schema')
    return data


def git_tracked(path):
    """True/False; raises RuntimeError when Git cannot decide (fail closed)."""
    return workspace.tracked(path)


def user_continuation_authorized():
    """User scope only: environment or ~/.context-watch/thresholds.json, never project config."""
    for name in ('CONTEXT_WATCH_AUTORESUME', 'AUTORESUME'):
        if os.environ.get(name, '').strip().lower() in ('1', 'true', 'yes', 'on'):
            return True
    return protocol.project_setting(os.path.expanduser('~'), 'autoresume', bool, user_only=True) is True


def evaluate(root, location, session=None):
    """One pointer's eligibility: dict(status=candidate|ineligible|incomplete|settled, ...)."""
    import restart_preflight as restart

    def out(status, reason=None, **extra):
        result = dict(status=status, pointer=str(location), **extra)
        if reason:
            result.update(reason=reason, action=RECOVER)
        return result
    try:
        data = read_pointer(location)
    except (OSError, ValueError, UnicodeDecodeError):
        return out('ineligible', 'invalid-pointer')
    if data['root'] != os.path.realpath(root):
        return out('ineligible', 'foreign-root')
    try:
        record = restart.status(root, data['request_id'])
    except ValueError:
        return out('ineligible', 'invalid-intent')
    if record is None:
        return out('incomplete', 'incomplete-index')
    if record['state'] == 'refused':
        return out('settled')
    if record['state'] == 'retrieved' and record.get('successor_session') != session:
        return out('settled')
    try:
        locator = restart._read_locator(restart._handle_location(data['handle']))
    except (OSError, ValueError, UnicodeDecodeError):
        return out('ineligible', 'locator-unresolved')
    digest = restart.binding_digest(record)
    if (locator != dict(root=record['root'], request_id=record['request_id'], binding_sha256=digest)
            or record.get('binding_sha256') != digest or data['binding_sha256'] != digest
            or any(data[k] != record[k] for k in ('checkpoint_id', 'session', 'terminal', 'request_id'))
            or data['checkpoint_path'] != record['path']):
        return out('ineligible', 'binding-mismatch')
    if restart.intent_expired(record):
        return out('ineligible', 'intent-expired')
    try:
        if any(git_tracked(p) for p in (location, restart._location(root, record['request_id']),
                                        record['path'])):
            return out('ineligible', 'git-tracked')
    except RuntimeError:
        return out('ineligible', 'git-check-indeterminate')
    # Read the checkpoint once: the trust check and the task/workspace binding
    # must describe the same bytes.
    try:
        checkpoint = Record.load(record['path'])
    except (OSError, ValueError):
        return out('ineligible', 'publication-untrusted')
    if not checkpoint.trusted():
        return out('ineligible', 'publication-untrusted')
    workspace = os.path.realpath(checkpoint.get('workspace') or record['root'])
    if data['workspace'] != workspace or data.get('task_id') != (checkpoint.get('task_id') or None):
        return out('ineligible', 'binding-mismatch')
    if not _workspace_exists(workspace):
        return out('ineligible', 'workspace-missing')
    if not user_continuation_authorized():
        return out('ineligible', 'continuation-not-authorized')
    mode = 'replay' if record['state'] == 'retrieved' else 'adopt'
    return out('candidate', request_id=record['request_id'], handle=data['handle'],
               terminal=record['terminal'], session=record['session'], mode=mode,
               created_at=data['created_at'], task_id=data.get('task_id'))


def scan(root, session=None):
    """Candidates plus diagnostics; settled pointers are skipped silently."""
    candidates, diagnostics = [], []
    directory = Path(pointer_dir(root))
    if not os.path.lexists(directory):
        return dict(candidates=candidates, diagnostics=diagnostics)
    try:
        _check_dirs(root)
        entries = sorted(directory.glob('*.json'))
    except (OSError, ValueError):
        return dict(candidates=[], diagnostics=[dict(
            status='ineligible', pointer=str(directory), reason='unsafe-pointer-dir', action=RECOVER)])
    for entry in entries:
        result = evaluate(root, str(entry), session)
        if result['status'] == 'candidate':
            candidates.append(result)
        elif result['status'] != 'settled':
            diagnostics.append(result)
    return dict(candidates=candidates, diagnostics=diagnostics)


def prune(root, request_ids):
    """Remove pointers of pruned settled intents."""
    for entry in Path(pointer_dir(root)).glob('*.json'):
        try:
            data = read_pointer(str(entry))
        except (OSError, ValueError, UnicodeDecodeError):
            continue
        if data['request_id'] in request_ids:
            try:
                entry.unlink()
            except OSError:
                pass


def matches_intent(root, record):
    """True when a v3 intent (pointer_version 1) has a readable pointer for its exact binding."""
    try:
        if record.get('pointer_version') != VERSION:
            return False
        _check_dirs(root)
        data = read_pointer(pointer_path(root, record['binding_sha256']))
    except (OSError, ValueError, KeyError, TypeError, UnicodeDecodeError):
        return False
    return (data['root'] == record['root'] and data['checkpoint_path'] == record['path']
            and all(data[k] == record[k] for k in ('checkpoint_id', 'session', 'terminal',
                                                   'request_id', 'binding_sha256')))
