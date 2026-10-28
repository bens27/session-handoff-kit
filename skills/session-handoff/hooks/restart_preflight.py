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
from pathlib import Path
import sys
import time
import hmac
import math
import stat
import shlex

from handoff_contract import RESTART_PROMPT_PREFIX, emit_output, exit_status, project_output
import handoff_holds as holds_store
import handoff_identity
import handoff_intents as intents
import handoff_ledger as ledger
from handoff_record import Record, live as _checkpoint_live  # noqa: F401  (tests read it here)
import handoff_pointers as pointers
import handoff_protocol as protocol


# The intent store owns layout, TTLs, states, validation and the adopt command;
# these names stay importable from here for existing callers.
_location = intents.location
status = intents.status
prune_settled = intents.prune_settled
intent_expired = intents.expired
INTENT_MAX_AGE = intents.TTL['pending']
SETTLED_RETENTION = intents.TTL['settled']
_accepted_event = intents.accepted_event
_acceptance_valid = intents.acceptance_valid
_confined_transcript = intents.confined_transcript
_transcript_roots = intents.transcript_roots
_worker_record_valid = intents.worker_record_valid
_in_flight_mailbox = intents.in_flight_mailbox


def adopt_command(root, request_id):
    """The one successor command a restart prompt or dispatcher hands over."""
    return intents.adopt_command(root, request_id, __file__)


BINDING_KEYS = intents.BINDING_KEYS
binding_digest = intents.binding_digest
_handle_location = intents.handle_location
_read_locator = intents.read_locator


def binding_command(binding):
    """Publish a hash-bound locator, independent of cwd and the hooks installation."""
    return intents.handle_command(intents.publish_handle(binding), __file__)


def adopt_handle(handle):
    """Resolve a handle to the ledger, then enforce the existing adopt lifecycle."""
    try:
        locator = _read_locator(_handle_location(handle))
        record = status(locator['root'], locator['request_id'])
        digest = binding_digest(record)
        if (locator['binding_sha256'] != digest or handle != 'h-' + digest[:24]
                or record.get('binding_sha256') != digest):
            raise ValueError('binding-does-not-match-recorded-intent')
        binding = {k: record[k] for k in BINDING_KEYS}
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return dict(outcome='blocked', stage='binding', reason=str(exc)[:500], host_confirmed=False,
                    action='Preserve state and report; never restart, dispatch another intent or pick another checkpoint.')
    return adopt(binding['root'], binding['request_id'], binding)


HOOK_DRIFT_POLICIES = ('warn', 'block')


def hook_drift_policy(root):
    """Effective hook-drift policy: 'warn' by default, 'block' if either the
    user or the project scope asks for it, None when a present value is invalid
    or an existing config file cannot be read or parsed.

    A project file cannot relax a user-scope block: whoever can rewrite the
    project's hook files can also rewrite its .context-watch.json."""
    policies = []
    for path in (os.path.join(os.path.expanduser('~'), '.context-watch', 'thresholds.json'),
                 os.path.join(root, '.context-watch.json')):
        try:
            data = protocol.load_state_json(path)
        except FileNotFoundError:
            continue  # an absent scope expresses no policy
        except (OSError, ValueError):
            return None  # an unreadable or corrupt scope could be hiding a block
        if not isinstance(data, dict):
            return None  # valid JSON that is not an object is an invalid file, never "no policy"
        if 'hook_drift' in data:
            if type(data['hook_drift']) is not str or data['hook_drift'] not in HOOK_DRIFT_POLICIES:
                return None
            policies.append(data['hook_drift'])
    return 'block' if 'block' in policies else 'warn'


def preflight(root, path, session, terminal, request_id, workers=None,
              host_capable=False, hold=False, observations=None):
    """Persist intent before returning dispatch=True exactly once per request.

    workers=None means inventory unknown. Each worker needs a durable report
    {path, sha256}, or settled: true with no report, or {state: in_flight, via:
    mailbox}; any other active worker without a report blocks. Host mailbox adapters
    must materialize retrieved reports locally before calling. A duplicate intent
    is uncertain after a crash: reconcile with the host, never blindly dispatch.
    """
    def blocked(reason):
        return dict(outcome='blocked', dispatch=False, reason=reason,
                    host_confirmed=False)

    def invalid_intent(exc, entry):
        diagnostic = (exc.diagnostic if isinstance(exc, intents.InvalidIntent) else
                      dict(path=os.path.realpath(entry), field='file', detail=str(exc)[:200]))
        return dict(blocked('invalid-persisted-intent'), invalid_intent=diagnostic,
                    action='Do not call agents_restart. Keep the checkpoint and offending intent unchanged. '
                           'Inspect invalid_intent.path and invalid_intent.field; reconcile that intent with '
                           'host history before an operator restores or quarantines it. Retry this exact '
                           'checkpoint after reconciliation, or continue manually in a fresh session.')
    # Native UUIDs, AgentsRoom terminal IDs and deterministic request IDs fit
    # this ASCII token format; arbitrary prose is not a first-message identifier.
    if any(not isinstance(value, str) or re.fullmatch(r'[A-Za-z0-9._:@-]{1,128}', value) is None
           for value in (session, terminal, request_id)):
        return blocked('invalid-identifier')
    root, path = os.path.realpath(root), os.path.realpath(path)
    try:
        location = _location(root, request_id, create=True)
    except handoff_identity.UntrustedStateDir:
        return blocked(handoff_identity.UntrustedStateDir.reason)
    binding = dict(root=root, path=path, session=session, terminal=terminal,
                   request_id=request_id)
    # The durable registry lock also covers different checkpoints and callers
    # with different TMPDIRs; the publisher lock alone is temp-directory scoped.
    registry = intents.registry(location)
    with ledger.locked(handoff_identity.publisher_lock_path(root)), ledger.locked(registry), ledger.locked(path), intents.locked(location):
        try:
            pointers.prune(root, prune_settled(root, held=(location,)))
            previous = status(root, request_id)
        except ValueError as exc:
            return invalid_intent(exc, location)
        reentry = None
        if previous is not None:
            if any(previous.get(k) != v for k, v in binding.items()):
                return blocked('request-identity-conflict')
            # A v3 intent (recorded pointer_version, pointer written first) that crashed
            # before its dispatch marker may reserve dispatch once, after every gate is
            # rechecked below. A pointer file alone never grants this: legacy intents
            # keep reconcile-only behavior even when a matching pointer is planted.
            if (previous['state'] == 'pending' and 'dispatch_started_at' not in previous
                    and pointers.matches_intent(root, previous)
                    and not intent_expired(previous)):
                reentry = previous
            else:
                result = dict({k: v for k, v in previous.items() if k != 'restart_prompt'},
                              outcome=previous['state'], dispatch=False)
                if 'dispatch_started_at' in previous:
                    result.update(reason='dispatch-reserved',
                                  action='Dispatch was already reserved; reconcile host history, never re-emit the restart call.')
                return result
        if not session or not terminal:
            return blocked('missing-identity')
        try:
            ledger._check_owner(path, session)
        except ledger.ConflictError:
            return blocked('checkpoint-owned-by-another-session')
        checkpoint = Record(path, Path(path).read_text())
        if (os.path.realpath(checkpoint.get('project', '')) != root or checkpoint.session_id != session
                or (checkpoint.terminal and checkpoint.terminal != terminal)):
            return blocked('stale-identity')
        if hold or checkpoint.held:
            return blocked('human-hold')
        if not checkpoint.open_strict or not checkpoint.published():
            return blocked('publication-not-ready')
        warnings = []
        if checkpoint.get('project_hooks'):
            workspace = checkpoint.get('workspace') or root
            policy = hook_drift_policy(root)
            if policy is None:
                return dict(blocked('invalid-hook-drift-policy'),
                            action='Make .context-watch.json and ~/.context-watch/thresholds.json '
                                   'readable JSON, with hook_drift absent, "warn" or "block".')
            try:
                baseline = json.loads(checkpoint.get('project_hooks'))
                current = protocol.project_hook_snapshot(workspace)
                if not isinstance(baseline, dict) or set(baseline) != set(current):
                    return blocked('invalid-hook-baseline')
                changed = [dict(path=os.path.join(workspace, name), saved_sha256=baseline[name],
                                current_sha256=digest)
                           for name, digest in sorted(current.items()) if baseline[name] != digest]
            except (OSError, ValueError, TypeError):
                return blocked('hook-config-unreadable')
            if changed:
                names = ', '.join(os.path.relpath(c['path'], workspace) for c in changed)
                if policy == 'block':
                    return dict(blocked('hooks-changed'), path=changed[0]['path'], changed=changed,
                                policy=policy,
                                action='Project hook files changed since save (%s). Review the change, '
                                       'then save again to record it as the new baseline before a new '
                                       'restart.' % names)
                warnings.append(dict(reason='hooks-changed', policy=policy, changed=changed,
                                     action='Project hook files changed since save (%s); the restart '
                                            'proceeds. Review the change if it was not expected; set '
                                            'hook_drift to "block" to stop restarts on drift.' % names))
        if ledger.resolve(path, root).get('authoritative') != path:
            return blocked('stale-checkpoint')
        # Validate every persisted intent, including legacy request IDs. A
        # different request name must not turn uncertain dispatch into permission.
        entry = intents.directory(root, create=False)
        try:
            for entry in intents.entries(root):
                if reentry is not None and os.path.realpath(entry) == location:
                    continue
                other = intents.canonical(root, entry)
                same_checkpoint = (other['path'] == path or
                                   other['checkpoint_id'] == checkpoint.checkpoint_id)
                # An expired or stale-marked intent is never auto-adopted: it only still guards its own checkpoint
                if ((other['state'] == 'pending' and (other['path'] == path or (
                        other['terminal'] == terminal and not intent_expired(other)
                        and not intents.stale_marked(other))))
                        or (other['state'] in intents.SETTLED and same_checkpoint)):
                    return blocked('conflicting-restart-intent')
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            return invalid_intent(exc, entry)
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
            report = worker.get('report')
            if report is None:
                report = {}
            if not isinstance(report, dict):
                return blocked('worker-report-not-durable')
            if not report:
                # No report file: a settled worker, or a mailbox delegation whose reply
                # lands in the AgentsRoom inbox the restarted agent reads. Any other
                # in-flight worker (a native subagent) would lose its report.
                if _in_flight_mailbox(worker) or worker.get('settled') is True:
                    continue
                if worker.get('state') == 'in_flight':
                    return blocked('worker-in-flight')
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
        if reentry is not None:
            if (reentry.get('fingerprint') != checkpoint.fingerprint
                    or reentry.get('checkpoint_id') != checkpoint.checkpoint_id
                    or reentry.get('workers') != workers
                    or reentry.get('binding_sha256') != binding_digest(reentry)
                    or not isinstance(reentry.get('restart_prompt'), str)):
                return blocked('request-identity-conflict')
            record = dict(reentry)
        else:
            record = dict(binding, checkpoint_id=checkpoint.checkpoint_id,
                          fingerprint=checkpoint.fingerprint, state='pending',
                          host_confirmed=False, workers=workers, intent_at=time.time())
            # Bind the compact successor handle to this exact intent.
            record['binding_sha256'] = binding_digest(record)
            record['pointer_version'] = pointers.VERSION  # v3 discriminator, never inferred from files
        # Pointer before any pending intent: a failed write leaves nothing to block.
        try:
            pointers.write_pointer(root, pointers.build(
                record, checkpoint.get('workspace') or root, pointers.checkpoint_ticket(checkpoint)))
        except pointers.PointerConflict:
            return dict(blocked('pointer-conflict'), stage='pointer',
                        action='Preserve the checkpoint; a different pointer already holds this binding.')
        except (OSError, ValueError, ledger.ConflictError) as exc:
            return dict(blocked('pointer-write-failed'), stage='pointer', detail=str(exc)[:200],
                        action='Preserve the checkpoint; retry the same request after restoring the pointer index.')
        try:
            prompt = RESTART_PROMPT_PREFIX + binding_command(
                dict(binding, checkpoint_id=record['checkpoint_id']))
        except (OSError, ValueError, ledger.ConflictError) as exc:
            return dict(blocked('restart-handle-unavailable'), stage='restart-handle-unavailable',
                        detail=str(exc)[:200],
                        action='Preserve the checkpoint; restore access to the locator store before requesting a restart.')
        if len(prompt) > 20000:
            return blocked('restart-prompt-too-large')
        if reentry is not None and prompt != reentry['restart_prompt']:
            return blocked('request-identity-conflict')
        record['restart_prompt'] = prompt
        # 3575c3c9: the drift audit trail lives in the intent, so duplicates and
        # status() show it; a re-entry replaces it with the warnings just computed.
        if warnings:
            record['warnings'] = warnings
        else:
            record.pop('warnings', None)
        if observations is not None and reentry is None:
            # Under the intent lock and only with a new intent, so the evidence
            # always describes the attempt that produced the dispatchable prompt.
            intents.write(intents.observations(location), observations)
        if reentry is None:
            intents.write(location, record)
        # Durable single-attempt reservation, written before any host call is returned.
        record['dispatch_started_at'] = time.time()
        intents.write(location, record)
        return dict(record, outcome='ready', dispatch=True)


TRANSCRIPT_MAX_BYTES = 64 * 1024 * 1024
TRANSCRIPT_MAX_LINE = 2 * 1024 * 1024


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
    location = _location(root, request_id, create=True)
    with intents.locked(location):
        record = status(root, request_id)
        if not record or record['state'] == 'refused' or intent_expired(record):
            raise ValueError('No unexpired pending or retrieved restart intent.')
        evidence = _transcript_evidence(record, transcript)
        if evidence is None:
            raise ValueError('No matching native host acceptance; preserve the intent and reconcile without redispatch.')
        record.update(host_confirmed=True, host_acceptance=evidence)
        intents.write(location, record)
        return record


def record_refusal(root, request_id, reason):
    """Record a returned host refusal/error. Never closes the checkpoint."""
    location = _location(root, request_id, create=True)
    with intents.locked(location):
        record = status(root, request_id)
        if not record:
            raise ValueError('No restart intent.')
        if record['state'] == 'retrieved':
            raise ValueError('Successor receipt already recorded.')
        if record['host_confirmed']:
            raise ValueError('Host acceptance already recorded; reconcile without marking a refusal.')
        intents.settle(record, 'refused', refusal=str(reason), host_confirmed=False)
        intents.write(location, record)
        return record


def confirm_successor(root, request_id, session, terminal=None, delivery_receipt=None):
    """Validate a real local preparation receipt, not an agent's claim to read.

    This proves kit retrieval only. Host process replacement and agent semantic
    understanding remain unproven; only native acceptance evidence sets host_confirmed.
    Call after read-only prepare; this never claims or acknowledges work.
    """
    location = _location(root, request_id, create=True)
    with intents.locked(location):
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
        receipt = protocol.load_state_json(handoff_identity.delivery_receipt_path(record['root'], session))
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
                or created > now or now - created > intents.TTL['delivery_receipt']):
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
        intents.settle(record, 'retrieved', successor_session=session,
                      retrieval_fingerprint=receipt['fingerprint'],
                      retrieval_receipt=dict(token_sha256=hashlib.sha256(delivery_receipt.encode()).hexdigest(),
                                             path=record['path'], fingerprint=receipt['fingerprint'],
                                             created=created, references=dict(references), confirmed_at=now))
        intents.write(location, record)
        return record


def successor_roots(binding_root):
    """Where a successor's identity may be recorded: its own working directory's
    root first, then the binding root (deduplicated by canonical root)."""
    roots, seen = [], set()
    try:
        here = [os.path.realpath(os.getcwd())]
    except OSError:
        here = []
    for where in here + [binding_root]:
        try:
            key = handoff_identity.canonical_root(where)
        except (OSError, ValueError):
            continue
        if key not in seen:
            seen.add(key)
            roots.append(where)
    return roots or [binding_root]


def pointer_ts(where, terminal):
    """Write time of the hook pointer for this terminal at `where`, or None."""
    try:
        value = handoff_identity._read_terminal_pointer(
            handoff_identity.terminal_pointer_path(where, terminal)).get('ts')
    except (OSError, ValueError):
        return None
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def adopt(root, request_id, binding=None, _deliver_body=False):
    """Successor's single atomic command: identity, retrieval, receipt, resume.

    Identity comes only from hook/host facts and the environment terminal, never
    from caller claims. The body is emitted once, by resume; the internal
    prepare only produces the delivery receipt on success. A blocked prepare
    returns retrieval context without confirming a successor or authorizing it.
    """
    def blocked(stage, reason):
        return dict(outcome='blocked', stage=stage, reason=str(reason)[:500], host_confirmed=False,
                    action='Preserve state and report; never restart, dispatch another intent or pick another checkpoint.')
    root = os.path.realpath(root)
    # The successor's hooks record it under its OWN root (where it was launched),
    # which differs from the binding root when the predecessor restarted from a
    # linked worktree (9fe919eb). Look there first; the binding root is the fallback.
    observed, roots_checked = [], successor_roots(root)
    for where in roots_checked:
        info = protocol.session_info(where)
        if (info['identity_source'] in ('hook', 'host') and info['session_id']
                and info['host_session_id'] in ('', info['session_id'])):
            observed.append((where, info))
    if not observed:
        return blocked('identity', 'identity-not-recorded-by-hook-or-host')
    terminal = protocol.terminal_id()
    try:
        record = status(root, request_id)
    except (ValueError, handoff_identity.UntrustedStateDir) as exc:
        return blocked('intent', exc)
    if record is None or record['state'] == 'refused':
        return blocked('intent', 'no-pending-restart-intent')
    if binding is not None and any(binding.get(k) != record.get(k) for k in BINDING_KEYS):
        return blocked('binding', 'binding-does-not-match-recorded-intent')
    # A hook pointer naming another session counts only if written after dispatch:
    # an older one is some earlier session of this terminal, not the successor.
    # The predecessor is excluded by session, not time (its own late PostToolUse
    # of the restart call post-dates dispatch).
    # No dispatch marker (an intent that crashed before it): fail closed.
    dispatched, stale = record.get('dispatch_started_at'), []
    if not isinstance(dispatched, (int, float)) or isinstance(dispatched, bool):
        dispatched = None
    fresh = []
    for where, other in observed:
        if other['identity_source'] == 'hook' and other['session_id'] != record['session']:
            written = pointer_ts(where, terminal) if terminal else None
            if dispatched is None or written is None or written <= dispatched:
                stale.append(where)
                continue
        fresh.append((where, other))
    observed = fresh
    # Explicit rule, never a silent pick: the own-root observation wins; another
    # root counts only when it names a session other than the predecessor.
    identity_root, info = next(((w, o) for w, o in observed if o['session_id'] != record['session']),
                               observed[0] if observed else (None, None))
    session = info['session_id'] if info else ''
    identity_facts = dict(
        expected=dict(predecessor_session=record['session'], terminal=record['terminal'],
                      successor_session='different from predecessor'),
        observed=dict(session=session, terminal=terminal,
                      host_session=info['host_session_id'] if info else '',
                      source=info['identity_source'] if info else 'none', root=identity_root or '',
                      roots_checked=list(roots_checked), stale_roots=stale))

    def prepare_blocked(reason, prepared=None):
        command = adopt_command(root, request_id)
        result = dict(blocked('prepare', reason), path=record['path'], identity=identity_facts,
                      command=command)
        if prepared is not None:
            # Retrieval context survives the refusal; a receipt must not suggest
            # that this different or incomplete preparation confirmed adoption.
            result['preparation'] = {k: v for k, v in prepared.items() if k != 'delivery_receipt'}
        detail = {
            'prepare-path-alias': 'Preparation returned an equivalent canonical path under an alias; reconcile path spelling before retrying. ',
            'prepare-target-mismatch': 'Preparation selected a different checkpoint; this intent cannot adopt it. ',
        }.get(reason, '')
        result['action'] = (
            'Inspect the recorded checkpoint at %s for its objective and next step. '
            'Any preparation below is retrieval-only and describes its returned path, not an adopted checkpoint. '
            '%sResolve the reported block first. Explicitly running this exact command authorizes continuation '
            'of the recorded checkpoint only, subject to identity, trust, ownership and hold checks: %s. '
            'It does not authorize a different checkpoint; ask the user before switching work. '
            'Preserve this intent; do not restart or dispatch another intent.' % (record['path'], detail, command))
        return result

    try:
        fm = Record.load(record['path']).fm
    except FileNotFoundError:
        fm = {}  # Let prepare/resume report a missing checkpoint at its established stage.
    except (OSError, ValueError) as exc:
        return prepare_blocked(exc)
    workspace = fm.get('workspace') or fm.get('project') or root
    missing = not os.path.isdir(workspace)
    workspace_facts = dict(path=workspace, exists=not missing,
                           git=fm.get('workspace_git') or fm.get('git', ''), recovery_root=root)
    if not terminal or terminal != record['terminal'] or not session or session == record['session']:
        result = dict(blocked('identity', 'successor-identity-mismatch'), identity=identity_facts,
                      workspace=workspace_facts)
        if terminal and terminal == record['terminal']:
            # The predecessor's own late hooks (e.g. PostToolUse of the restart
            # call) can refresh its pointer after dispatch, so a timestamp cannot
            # tell the two apart; the session can.
            parts = ['no successor observation at %s' % ', '.join(roots_checked)]
            if session:
                parts.append('pointer still names the predecessor')
            parts += ['pointer at %s predates dispatch' % where for where in stale]
            result['detail'] = '; '.join(parts)
        return result
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
            return prepare_blocked(exc)
        if prepared.get('outcome') != 'retrieved':
            return prepare_blocked('prepare-' + str(prepared.get('outcome', 'unknown')), prepared)
        path = prepared.get('path')
        if path != record['path']:
            reason = ('prepare-path-alias' if isinstance(path, str) and os.path.realpath(path) == record['path']
                      else 'prepare-target-mismatch')
            return prepare_blocked(reason, prepared)
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
    if not _deliver_body and record['state'] == 'retrieved' and os.path.exists(
            startup_delivery_path(root, request_id, session)):
        # SessionStart already delivered this body to this successor: reuse that
        # receipt and never emit the body or skills a second time.
        return dict(outcome='already-delivered', stage='startup', path=record['path'], restart=identity,
                    report=report, host_confirmed=record['host_confirmed'],
                    action='SessionStart already delivered this checkpoint to this session. Continue from '
                           'its ## Next steps in your context; do not rerun adopt or restart.')
    # Dispatcher holds gate execution, not retrieval: the receipt above stays recorded.
    try:
        topic = Record.load(record['path']).topic
    except (OSError, ValueError):
        topic = None
    try:
        holds = holds_store.applicable_holds(holds_store.read_holds(root), topic)
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
    if result['outcome'] == 'resumed':
        # Repair the observed successor note from the original transfer clock.
        try:
            protocol.record_resume_note(identity_root or root, session, Record.load(record['path']).fm)
        except (OSError, ValueError):
            pass  # Optional note repair must not fail an already successful transfer.
        # Account for the final adopt envelope and its ASCII-escaped wire format.
        protocol.delivery_metrics(result, identity_root or root, session, profile='raw')
    return result


# ---------------------------------------------------------------- startup adoption (v3-3)

RESUME_POINTER_POLICIES = ('ask', 'latest-owned', 'manual')
STARTUP_PREFIX = 'startup-adoption: '
# Diagnostics that only mean "not mine to continue automatically"; the manual
# restart prompt remains the path, so startup stays quiet about them.
QUIET_REASONS = ('incomplete-index', 'continuation-not-authorized')


HOST_WINDOW = getattr(intents, 'HOST_WINDOW', 1800)


def _stale_marked(record, now=None):
    """Pending, dispatch marked over HOST_WINDOW ago, no host evidence: never auto-adopted.

    Same rule as intents.stale_marked (#87); this fallback serves until that lands."""
    if hasattr(intents, 'stale_marked'):
        return intents.stale_marked(record, now)
    started = record.get('dispatch_started_at')
    if record.get('state') != 'pending' or record.get('host_confirmed') is True:
        return False
    if not isinstance(started, (int, float)) or isinstance(started, bool):
        return False
    return (time.time() if now is None else now) - started > HOST_WINDOW


def mark_startup_delivered(root, request_id, session):
    """Called by the hook only after the delivery was emitted and flushed."""
    try:
        os.close(os.open(startup_delivery_path(root, request_id, session),
                         os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    except FileExistsError:
        pass


def startup_delivery_path(root, request_id, session):
    """Once-only marker beside the intent: SessionStart delivered this body to this session."""
    digest = hashlib.sha256(('%s\0%s' % (request_id, session)).encode()).hexdigest()[:24]
    return intents.location(root, request_id)[:-len('.json')] + '.startup-' + digest


def resume_pointer_policy(root):
    """Effective (policy, source, error): project .context-watch.json over user thresholds.json.

    Strict: a present but malformed value names the key and disables automatic selection."""
    policy, source = 'ask', 'default'
    for scope, path in (('global', os.path.join(os.path.expanduser('~'), '.context-watch', 'thresholds.json')),
                        ('project', os.path.join(root, '.context-watch.json'))):
        try:
            data = protocol.load_state_json(path)
        except FileNotFoundError:
            continue
        except (OSError, ValueError):
            return None, scope, 'unreadable-config:' + path
        if not isinstance(data, dict) or 'resume_pointer_policy' not in data:
            continue
        value = data['resume_pointer_policy']
        if value not in RESUME_POINTER_POLICIES:
            return None, scope, 'invalid-resume-pointer-policy'
        policy, source = value, scope
    return policy, source, None


def startup(cwd, session):
    """SessionStart side of v3 restart: select at most one pointer and adopt it once.

    Returns None for the fast no-op (no candidate: the existing announcer runs),
    otherwise a dict for the hook to deliver as context. Identity is the
    hook-recorded session and the environment terminal, never a caller claim."""
    terminal = protocol.terminal_id()
    if not session or not terminal:
        return None
    if not pointers.user_continuation_authorized():
        # Not authorized: the manual restart prompt stays the only path. Silent to
        # the model; the hook logs it for stats.
        return dict(outcome='unauthorized', quiet=True)
    root = os.path.realpath(handoff_identity.project_root(cwd))
    if not os.path.isdir(pointers.pointer_dir(root)):
        return None
    found = pointers.scan(root, session)
    def fresh(candidate):
        try:
            return not _stale_marked(status(root, candidate['request_id']) or {})
        except (ValueError, handoff_identity.UntrustedStateDir):
            return False
    owned = [c for c in found['candidates']
             if c['terminal'] == terminal and c['session'] != session and c['mode'] == 'adopt' and fresh(c)]
    diagnostics = [d for d in found['diagnostics'] if d.get('reason') not in QUIET_REASONS]
    requested = os.environ.get('HANDOFF_RESTART_REQUEST_ID', '').strip()
    if requested:
        try:
            settled = (status(root, requested) or {}).get('state') in intents.SETTLED
        except (ValueError, handoff_identity.UntrustedStateDir):
            settled = False
        if settled:
            requested = ''  # A settled request is history, not a selection.

    def note(outcome, **extra):
        return dict(outcome=outcome, root=root, session=session, terminal=terminal, **extra)
    if diagnostics:
        return note('blocked', reason='invalid-relevant-pointer', diagnostics=diagnostics,
                    action='Automatic selection is off while a pointer is invalid. Report the diagnostics; '
                           'the exact restart prompt remains the recovery.')
    if requested:
        # An exact host request selects before policy and never falls back.
        owned = [c for c in owned if c['request_id'] == requested]
        if not owned:
            return note('blocked', reason='requested-intent-not-eligible', request_id=requested,
                        action='The requested restart intent is not an eligible candidate here; '
                               'do not pick another checkpoint.')
        chosen, policy, source = owned[0], 'exact-request', 'host'
    elif not owned:
        return None
    elif len(owned) == 1:
        chosen, policy, source = owned[0], 'single', 'pointer'
    else:
        policy, source, error = resume_pointer_policy(root)
        if error:
            return note('blocked', reason=error, policy_source=source,
                        action='Fix resume_pointer_policy (ask, latest-owned or manual); '
                               'automatic selection stays off until then.')
        if policy != 'latest-owned':
            listed = [dict(request_id=c['request_id'], ticket=c.get('ticket'), created_at=c['created_at'],
                           command=adopt_command(root, c['request_id']))
                      for c in sorted(owned, key=lambda c: (c['created_at'], c['pointer']))]
            return note('choose', policy=policy, policy_source=source, candidates=listed,
                        action='Several restart checkpoints are pending for this terminal. Ask the user which '
                               'to continue, then run exactly that command; adopt nothing on your own.'
                               if policy == 'ask' else
                               'Several restart checkpoints are pending (manual policy). Report them; '
                               'run one command only on the user\'s instruction.')
        chosen = max(owned, key=lambda c: (c['created_at'], c['pointer']))
    if os.path.exists(startup_delivery_path(root, chosen['request_id'], session)):
        return None  # Already delivered to this session: never twice.
    # No marker yet: the hook writes it only after emitting this result, so a
    # killed hook or lost output leaves the exact prompt able to deliver the body.
    result = adopt(root, chosen['request_id'], _deliver_body=True)
    result['startup'] = dict(policy=policy, policy_source=source, request_id=chosen['request_id'],
                             root=root, session=session)
    return result


def request_id_for(terminal, checkpoint_id):
    """Deterministic, reusable request ID: never mint a new one to evade state."""
    return 'agentsroom:' + hashlib.sha256((terminal + '\0' + checkpoint_id).encode()).hexdigest()


def restart_block_path(session):
    """Beside the context-watch latch: the watcher re-offers or reminds from it (9452e1fe)."""
    return handoff_identity.latch_path(session) + '.restart-blocked'


def record_restart_block(session, terminal, root, path, checkpoint, result, caller_hold, holds):
    """A blocked restart leaves a marker for context-watch; any other outcome removes it."""
    marker = restart_block_path(session)
    try:
        if result['outcome'] != 'blocked':
            os.remove(marker)
            return
        protocol.write_json(marker, dict(
            session=session, terminal=terminal, root=root, path=path, topic=checkpoint.topic or '',
            reason=result.get('reason') or '', caller_hold=caller_hold,
            durable_holds=[h['scope'] for h in holds], checkpoint_held=checkpoint.held,
            blocked_at=time.time(), turns=0, reoffered=False))
    except OSError:
        pass  # advisory: the restart result itself is authoritative


def restart(root, workers=None, host_capable=False, hold=False, path=None):
    """Predecessor's single command: identity, checkpoint, holds, observations, preflight.

    The caller still supplies what only the host can observe: the complete worker
    inventory (None = unknown, blocks) and the restart capability. Holds are the
    union of the caller's observation and durable dispatcher holds.
    """
    def blocked(reason):
        if session:  # the marker describes the latest attempt, never an earlier one
            try:
                os.remove(restart_block_path(session))
            except OSError:
                pass
        return dict(outcome='blocked', dispatch=False, reason=reason, host_confirmed=False,
                    action='Do not call agents_restart. Keep the checkpoint open and report; '
                           'continue manually in a fresh session if needed.')
    session = ''
    root = os.path.realpath(root)
    info = protocol.session_info(root)
    session, terminal = info['session_id'], protocol.terminal_id()
    if (info['identity_source'] not in ('hook', 'host') or not session or not terminal
            or info['host_session_id'] not in ('', session)):
        return blocked('identity-not-recorded-by-hook-or-host')
    if path is None:
        errors = []
        own = [r for r in ledger._records(root, errors) if r['record'].is_open
               and r['record'].session_id == session
               and r['record'].get('terminal', terminal) == terminal]
        if errors:
            return blocked('checkpoints-unreadable')
        if not own:
            return blocked('no-open-checkpoint')
        if len(own) > 1:
            candidates = []
            for item in sorted(own, key=lambda r: r['path'])[:10]:
                checkpoint_id = item['record'].checkpoint_id or ''
                argv = ['python3', os.path.realpath(__file__), 'restart', '--root', root,
                        '--path', item['path']]
                if workers is not None:
                    argv += ['--workers', json.dumps(workers)]
                if host_capable is True:
                    argv.append('--host-capable')
                if hold:
                    argv.append('--hold')
                candidates.append(dict(path=item['path'], checkpoint_id=checkpoint_id,
                                       command=shlex.join(argv)))
            return dict(blocked('ambiguous-checkpoint'), candidates=candidates, total=len(own),
                        action='Do not call agents_restart or select by recency. Match the successful save path '
                               'to a candidate and run its command; otherwise ask which checkpoint to continue. '
                               'Each command rechecks identity, ownership, publication, workers and holds. '
                               'Candidates are capped at ten; an exact known save path can be passed with --path.')
        path = own[0]['path']
    path = os.path.realpath(path)
    try:
        checkpoint = Record.load(path)
    except OSError:
        return blocked('checkpoint-unreadable')
    checkpoint_id = checkpoint.checkpoint_id or ''
    if not checkpoint_id:
        return blocked('publication-not-ready')
    request_id = request_id_for(terminal, checkpoint_id)
    try:
        holds = holds_store.applicable_holds(holds_store.read_holds(root), checkpoint.topic)
    except OSError as exc:
        return dict(blocked('hold'), stage='hold', detail=str(exc))
    caller_hold = bool(hold)
    hold = caller_hold or bool(holds)
    observations = dict(root=root, path=path, session=session, terminal=terminal,
                        request_id=request_id, workers=workers, host_capable=host_capable is True,
                        hold=hold, holds=holds, identity_source=info['identity_source'],
                        host_session_id=info['host_session_id'], observed_at=time.time())
    result = preflight(root, path, session, terminal, request_id, workers=workers,
                       host_capable=host_capable is True, hold=hold, observations=observations)
    out = dict(outcome=result['outcome'], dispatch=result['dispatch'], request_id=request_id,
               path=path, host_confirmed=False)
    record_restart_block(session, terminal, root, path, checkpoint, result, caller_hold, holds)
    if result['outcome'] == 'ready':
        out.update(call='agents_restart', agent=terminal, restart_prompt=result['restart_prompt'],
                   action='Write the session status, then call agents_restart(prompt=restart_prompt) as the '
                          'final tool action and end the turn with one short line.')
        if result.get('warnings'):
            out['warnings'] = result['warnings']
    else:
        out.update({k: result[k] for k in ('reason', 'state', 'changed', 'policy', 'warnings', 'invalid_intent') if k in result})
        if result.get('action'):
            out['detail'] = result['action']
        out.update(
                   action='Do not call agents_restart. Pending means reconcile with host history; '
                          'refused means the host declined this checkpoint\'s deterministic request, so a '
                          'retry needs a new save once the cause is resolved; blocked means keep the '
                          'checkpoint open and report.')
        if result.get('invalid_intent'):
            out['action'] = result['action']
    return out


def cli():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['preflight', 'status', 'refusal', 'successor', 'adopt', 'restart', 'acceptance', 'startup'])
    parser.add_argument('binding', nargs='?', help='adopt: short restart handle (legacy binding JSON accepted for one release)')
    parser.add_argument('--input', help='JSON argument file or - for stdin; defaults to stdin')
    parser.add_argument('--root', help='adopt: project root')
    parser.add_argument('--request-id', help='adopt: restart request ID from the binding')
    parser.add_argument('--workers', help='restart: complete worker inventory JSON list; omit when unknown')
    parser.add_argument('--host-capable', action='store_true', help='restart: agents_restart with prompt is available')
    parser.add_argument('--hold', action='store_true', help='restart: a live human hold or needsInput is observed')
    parser.add_argument('--path', help='restart: exact checkpoint when this session has several open')
    parser.add_argument('--cwd', help='startup: SessionStart working directory')
    parser.add_argument('--session', help='startup: hook-recorded session id')
    args = parser.parse_args()
    try:
        if args.operation == 'startup':
            if args.request_id:
                mark_startup_delivered(os.path.realpath(args.root), args.request_id, args.session or '')
                return 0
            result = startup(args.cwd or os.getcwd(), args.session or '')
            if result is not None:
                emit_output(project_output(result) if result.get('outcome') == 'resumed' else result)
            return 0
        if args.operation == 'adopt':
            if args.binding is not None and not args.binding.lstrip().startswith('{'):
                result = adopt_handle(args.binding)
            elif args.binding is not None:
                binding = json.loads(args.binding)
                if not isinstance(binding, dict) or set(binding) != set(BINDING_KEYS):
                    raise ValueError('adopt binding must hold exactly: ' + ', '.join(BINDING_KEYS))
                result = adopt(binding['root'], binding['request_id'], binding)
            elif not args.root or not args.request_id:
                raise ValueError('adopt requires a short handle, legacy binding JSON, or --root and --request-id.')
            else:
                result = adopt(args.root, args.request_id)
            emit_output(project_output(result))
            return exit_status('preflight', result.get('outcome'), 'adopt')
        if args.operation == 'restart':
            if not args.root:
                raise ValueError('restart requires --root.')
            workers = None if args.workers is None else json.loads(args.workers)
            result = restart(args.root, workers, args.host_capable, args.hold, args.path)
            emit_output(result)
            return exit_status('preflight', result['outcome'], 'restart')
        data = protocol.decode_state_json(protocol.read_cli_input(args.input or '-'))
        function = {'preflight': preflight, 'status': status,
                    'refusal': record_refusal, 'successor': confirm_successor,
                    'acceptance': record_acceptance}[args.operation]
        result = function(**data)
        if args.operation == 'successor':
            result = {k: v for k, v in result.items() if k != 'restart_prompt'}
        emit_output(result)
    except (OSError, ValueError, TypeError, KeyError, ledger.ConflictError) as exc:
        emit_output(dict(outcome='blocked', dispatch=False, reason=str(exc),
                         host_confirmed=False))
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(cli())
