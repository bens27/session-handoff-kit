#!/usr/bin/env python3
"""Versioned project pointer index for v3 restarts (SPEC 7.3d, slice 2).

A pointer is a derived, body-free index entry at
``<root>/.handoffs/pointers/<binding-sha256>.json``. It is never authority on its
own: eligibility requires the per-user HOME locator, the authoritative intent,
a publication sealed by this machine's existing receipt key, no Git-tracked
pointer/intent/checkpoint and user-scope continuation authorization.
"""
import json
import fcntl
import secrets
from contextlib import contextmanager
import os
import re
import stat
import time

import handoff_contract as contract
import handoff_identity
import handoff_intents as intents
import handoff_ledger as ledger
import handoff_workspace as workspace
from handoff_record import Record
import handoff_protocol as protocol

VERSION = 1
MAX_BYTES = 16384
FIELDS = ('version', 'checkpoint_id', 'checkpoint_path', 'root', 'workspace', 'session',
          'terminal', 'request_id', 'binding_sha256', 'handle', 'created_at')
# `ticket` binds the checkpoint's ticket header. `task_id` is read-only: pointers
# written before #155 carry it, and they must still adopt.
OPTIONAL = ('ticket', 'task_id')
RECOVER = ('Do not resume automatically; continue explicitly with the restart handle '
           '(restart_preflight.py adopt) after confirming the checkpoint.')


class PointerConflict(ValueError):
    pass


def pointer_dir(root):
    return os.path.join(os.path.realpath(root), '.handoffs', 'pointers')


def _safe_dir(fd, private):
    """Refuse a symlinked or foreign-owned index directory. ``.handoffs`` follows the
    umask (0775 under user-private groups), so only other-write is refused there;
    ``.handoffs/pointers`` is the trust anchor and must be private."""
    info = os.fstat(fd)
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & (0o077 if private else 0o002)):
        raise ValueError('unsafe-pointer-dir')


@contextmanager
def _check_dirs(root, create=False):
    """Pin both validated directories; never resolve their names again during I/O."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    handoffs = os.path.join(os.path.realpath(root), '.handoffs')
    hfd = os.open(handoffs, flags)
    try:
        _safe_dir(hfd, private=False)
        # 887c009c: at an exact temp base, handoffs_dir_usable's 0o022 rule, on the pinned fd.
        if os.path.realpath(root) in handoff_identity.temp_bases() and os.fstat(hfd).st_mode & 0o022:
            raise ValueError(handoff_identity.UntrustedStateDir.reason)
        if create:
            try:
                os.mkdir('pointers', 0o700, dir_fd=hfd)
            except FileExistsError:
                pass
        pfd = os.open('pointers', flags, dir_fd=hfd)
        try:
            _safe_dir(pfd, private=True)
            yield pfd
        finally:
            os.close(pfd)
    finally:
        os.close(hfd)


@contextmanager
def _locked(directory_fd, name):
    """Stable sidecar lock in the pinned directory, with the ledger's timeout."""
    fd = os.open(name + '.lock', os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                 0o600, dir_fd=directory_fd)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise ValueError('unsafe-pointer-lock')
        deadline = time.monotonic() + 3
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ledger.ConflictError('busy; retry after the current operation finishes')
                time.sleep(0.02)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _write_json(directory_fd, name, data):
    """Atomic private publication without reopening the checked directory path."""
    temporary = '.state-' + secrets.token_hex(16)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                 0o600, dir_fd=directory_fd)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(data, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        os.fsync(directory_fd)
    finally:
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except FileNotFoundError:
            pass


def _workspace_exists(path):
    return os.path.isdir(path)


def pointer_path(root, digest):
    if not isinstance(digest, str) or re.fullmatch(r'[0-9a-f]{64}', digest) is None:
        raise ValueError('invalid-binding-digest')
    return os.path.join(pointer_dir(root), digest + '.json')


def build(record, workspace=None, ticket=None):
    """Pointer content for a validated intent record (binding fields equal the intent)."""
    data = dict(version=VERSION, checkpoint_id=record['checkpoint_id'],
                checkpoint_path=record['path'], root=record['root'],
                workspace=os.path.realpath(workspace or record['root']),
                session=record['session'], terminal=record['terminal'],
                request_id=record['request_id'], binding_sha256=record['binding_sha256'],
                handle='h-' + record['binding_sha256'][:24], created_at=time.time())
    if isinstance(ticket, str) and ticket.strip():
        data['ticket'] = ticket
    return data


_UNBOUND = object()


def checkpoint_ticket(checkpoint):
    """The ticket a checkpoint binds: a well-formed `ticket:` (#95), else the legacy
    `task_id:` header. A malformed `ticket:` reads as absent, as in checkpoint_metadata."""
    ticket = checkpoint.get('ticket')
    if isinstance(ticket, str) and contract.METADATA_FORMATS['ticket'].fullmatch(ticket):
        return ticket
    return checkpoint.get('task_id') or None


def bound_ticket(data):
    """The pointer's ticket (legacy `task_id` as fallback), None when the pointer
    names none, or _UNBOUND when both keys are present and disagree (tampered)."""
    values = [data[k] for k in OPTIONAL if k in data]
    if any(v != values[0] for v in values):
        return _UNBOUND
    return values[0] if values else None


def _same(a, b):
    return {k: v for k, v in a.items() if k != 'created_at'} == {
        k: v for k, v in b.items() if k != 'created_at'}


def write_pointer(root, data):
    """Atomically write (fsync) a private pointer; identical retries are no-ops, conflicts refuse."""
    location = pointer_path(root, data['binding_sha256'])
    name = os.path.basename(location)
    with _check_dirs(root, create=True) as directory_fd, _locked(directory_fd, name):
        try:
            existing = read_pointer(location, dir_fd=directory_fd)
        except FileNotFoundError:
            _write_json(directory_fd, name, data)
        else:
            if not _same(existing, data):
                raise PointerConflict('pointer-conflict')
    return location


def read_pointer(location, *, dir_fd=None):
    """Bounded regular private file, no symlink/FIFO following, schema-validated."""
    if dir_fd is None:
        directory = os.path.dirname(os.path.abspath(location))
        handoffs = os.path.dirname(directory)
        if os.path.basename(directory) != 'pointers' or os.path.basename(handoffs) != '.handoffs':
            raise ValueError('unsafe-pointer-dir')
        with _check_dirs(os.path.dirname(handoffs)) as directory_fd:
            return read_pointer(location, dir_fd=directory_fd)
    fd = os.open(os.path.basename(location), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                 dir_fd=dir_fd)
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


def evaluate(root, location, session=None, *, dir_fd=None):
    """One pointer's eligibility: dict(status=candidate|ineligible|incomplete|settled, ...)."""

    def out(status, reason=None, **extra):
        result = dict(status=status, pointer=str(location), **extra)
        if reason:
            result.update(reason=reason, action=RECOVER)
        return result
    try:
        data = read_pointer(location, dir_fd=dir_fd)
    except (OSError, ValueError, UnicodeDecodeError):
        return out('ineligible', 'invalid-pointer')
    if data['root'] != os.path.realpath(root):
        return out('ineligible', 'foreign-root')
    try:
        record = intents.status(root, data['request_id'])
    except ValueError:
        return out('ineligible', 'invalid-intent')
    if record is None:
        return out('incomplete', 'incomplete-index')
    if record['state'] == 'refused':
        return out('settled')
    if record['state'] == 'retrieved' and record.get('successor_session') != session:
        return out('settled')
    try:
        locator = intents.read_locator(intents.handle_location(data['handle']))
    except (OSError, ValueError, UnicodeDecodeError):
        return out('ineligible', 'locator-unresolved')
    digest = intents.binding_digest(record)
    if (locator != dict(root=record['root'], request_id=record['request_id'], binding_sha256=digest)
            or record.get('binding_sha256') != digest or data['binding_sha256'] != digest
            or any(data[k] != record[k] for k in ('checkpoint_id', 'session', 'terminal', 'request_id'))
            or data['checkpoint_path'] != record['path']):
        return out('ineligible', 'binding-mismatch')
    if intents.expired(record):
        return out('ineligible', 'intent-expired')
    try:
        if any(git_tracked(p) for p in (location, intents.location(root, record['request_id']),
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
    # A pointer that names a ticket must name the sealed checkpoint's. One that names
    # none (written by 5201875 and earlier, which bound only a `task_id:` header) adopts
    # with the checkpoint's ticket: the checkpoint is sealed, the pointer only indexes it.
    named, ticket = bound_ticket(data), checkpoint_ticket(checkpoint)
    if data['workspace'] != workspace or named is _UNBOUND or named not in (None, ticket):
        return out('ineligible', 'binding-mismatch')
    if not _workspace_exists(workspace):
        return out('ineligible', 'workspace-missing')
    if not user_continuation_authorized():
        return out('ineligible', 'continuation-not-authorized')
    mode = 'replay' if record['state'] == 'retrieved' else 'adopt'
    return out('candidate', request_id=record['request_id'], handle=data['handle'],
               terminal=record['terminal'], session=record['session'], mode=mode,
               created_at=data['created_at'], ticket=ticket)


def scan(root, session=None):
    """Candidates plus diagnostics; settled pointers are skipped silently."""
    candidates, diagnostics = [], []
    directory = pointer_dir(root)
    try:
        with _check_dirs(root) as directory_fd:
            for name in sorted(os.listdir(directory_fd)):
                if not name.endswith('.json'):
                    continue
                result = evaluate(root, os.path.join(directory, name), session, dir_fd=directory_fd)
                if result['status'] == 'candidate':
                    candidates.append(result)
                elif result['status'] != 'settled':
                    diagnostics.append(result)
    except FileNotFoundError:
        return dict(candidates=[], diagnostics=[])
    except (OSError, ValueError) as exc:
        untrusted = handoff_identity.UntrustedStateDir.reason
        reason = untrusted if str(exc) == untrusted else 'unsafe-pointer-dir'
        return dict(candidates=[], diagnostics=[dict(
            status='ineligible', pointer=directory, reason=reason, action=RECOVER)])
    return dict(candidates=candidates, diagnostics=diagnostics)


def prune(root, request_ids, *, now=None, grace_seconds=24 * 60 * 60):
    """Remove pruned intents' pointers and stale unreadable/orphaned index entries.

    Call with the intent registry lock held, as for ``intents.prune_settled``.
    ``grace_seconds`` configures the 24-hour default repair window; age uses lstat
    mtime, never untrusted pointer content. Existing (even invalid) intents stay.
    Unsafe directories and foreign-owned entries are never cleaned up. Unreadable
    pointers whose filename digest belongs to an intent on disk are preserved.
    """
    if (type(grace_seconds) not in (int, float) or not 0 <= grace_seconds < float('inf')):
        raise ValueError('invalid-pointer-grace')
    now = time.time() if now is None else now
    try:
        protected = set()
        for location in intents.entries(root):
            record = protocol.load_state_json(location)
            if not isinstance(record, dict):
                return  # Unknown intent state cannot justify deleting its index.
            digest = record.get('binding_sha256')
            if digest is not None and not isinstance(digest, str):
                return
            protected.add(digest)
        with _check_dirs(root) as directory_fd:
            for name in os.listdir(directory_fd):
                if not name.endswith('.json'):
                    continue
                try:
                    info = os.lstat(name, dir_fd=directory_fd)
                    if info.st_uid != os.getuid() or stat.S_ISDIR(info.st_mode):
                        continue
                    stale = now - info.st_mtime > grace_seconds
                    try:
                        data = read_pointer(name, dir_fd=directory_fd)
                    except (OSError, ValueError, UnicodeDecodeError):
                        if not stale or name[:-len('.json')] in protected:
                            continue
                    else:
                        if data['root'] != os.path.realpath(root):
                            continue
                        if data['request_id'] not in request_ids:
                            if not stale or intents.status(root, data['request_id']) is not None:
                                continue
                    os.unlink(name, dir_fd=directory_fd)
                except (OSError, ValueError):
                    pass  # Best-effort cleanup; preserve unreadable intent state.
    except (OSError, ValueError):
        pass


def matches_intent(root, record):
    """True when a v3 intent (pointer_version 1) has a readable pointer for its exact binding."""
    try:
        if record.get('pointer_version') != VERSION:
            return False
        with _check_dirs(root) as directory_fd:
            data = read_pointer(pointer_path(root, record['binding_sha256']), dir_fd=directory_fd)
    except (OSError, ValueError, KeyError, TypeError, UnicodeDecodeError):
        return False
    return (data['root'] == record['root'] and data['checkpoint_path'] == record['path']
            and all(data[k] == record[k] for k in ('checkpoint_id', 'session', 'terminal',
                                                   'request_id', 'binding_sha256')))
