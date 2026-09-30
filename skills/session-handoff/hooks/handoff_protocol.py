"""Bounded public handoff workflow. Stdlib only; legacy ledger is the file adapter."""
import argparse
import json
import os
import shlex
import sys
import hashlib
import tempfile
from datetime import datetime

import handoff_ledger as ledger


def lookup(root='.', max_age=14, offset=0, topic=None):
    stats = {}
    items = ledger.scan(root, max_age, stats)
    if topic is not None:
        items = [h for h in items if h['topic'] == topic or os.path.realpath(h['path']) == os.path.realpath(topic)]
    outcome = ('error' if stats['errors'] else 'incomplete' if stats['incomplete'] else 'available' if items else
               'claimed' if stats['claimed'] else 'stale' if stats['stale'] else 'none')
    actions = {
        'incomplete': 'A checkpoint publication is incomplete or changed. Keep the draft/session; retry the original save request, or inspect history to repair it. Do not clear.',
        'none': 'No open handoff found. Continue the current task; for an explicit retrieval request report this result. Use history to inspect closed work.',
        'claimed': 'The handoff is held by another session. Continue unrelated work or wait for release; do not adopt it.',
        'stale': 'Only stale handoffs remain. Run lookup --max-age-days 9999 to select one explicitly.',
        'error': 'Handoff lookup failed. Report the unreadable locations; do not treat this as absence or adopt a possibly stale record.',
        'available': 'Select a handoff, then prepare it. Retrieval alone does not authorize executing its next steps.',
    }
    return dict(outcome=outcome, action=actions[outcome], items=items[max(0, offset):max(0, offset)+5], total=len(items),
                stale=stats['stale'], claimed=stats['claimed'], incomplete=stats['incomplete'], errors=stats['errors'][:5])


MAX_DOCUMENT_BYTES = 24_000


def publish(path, text):
    """Exclusive atomic publication; a crash never leaves a partial .md file."""
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.checkpoint-', dir=directory)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.link(temporary, path)
        dfd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    finally:
        os.unlink(temporary)


def save(root, session, request_id, document):
    root = ledger.project_root(root)
    if not isinstance(document, dict):
        raise ValueError('Draft must be a JSON object.')
    topic = document.get('topic', '')
    import re
    if not isinstance(topic, str) or len(topic) > 80 or not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', topic):
        return dict(outcome='invalid', action='Supply a short kebab-case topic.')
    body = document.get('body', '')
    if not isinstance(body, str):
        return dict(outcome='invalid', action='Supply a Markdown body.')
    problems = ledger._problems('---\nstatus: open\n---\n' + body, {'status':'open'})
    if not body.strip() or problems or len(body.encode()) > MAX_DOCUMENT_BYTES:
        return dict(outcome='invalid', problems=problems[:8], action='Keep the previous checkpoint and draft. Preserve constraints, current state and next steps in a smaller core; put background in optional references and retry.')
    identity = hashlib.sha256((os.path.realpath(root) + '\0' + session + '\0' + request_id).encode()).hexdigest()
    fingerprint = hashlib.sha256(json.dumps(document, sort_keys=True).encode()).hexdigest()
    from contextlib import ExitStack
    with ExitStack() as locks:
        locks.enter_context(ledger.locked(ledger.session_note_path(root, 'publisher')))
        errors = []
        records = ledger._records(root, errors)
        if errors:
            return dict(outcome='blocked', action='Restore read access before saving; a prior checkpoint or retry may be hidden.', errors=errors[:5])
        for record in records:
            if record['fm'].get('checkpoint_id') == identity:
                if record['fm'].get('content_hash') != fingerprint:
                    return dict(outcome='conflict', action='This request ID was already saved with different content. Use a new request ID.')
                if os.path.exists(record['path'] + '.published') and not ledger.publication_valid(record['path'], record['text'], record['fm']):
                    return dict(outcome='conflict', action='Published checkpoint changed; reconcile it and use a new request ID.')
                # Reconstruct expected content before repairing an interrupted receipt.
                if record['text'].split('\n---\n', 1)[-1] != body:
                    return dict(outcome='conflict', action='Saved body changed; preserve it and use a new request ID after reconciliation.')
                write_json(record['path'] + '.published', dict(checkpoint_id=identity, fingerprint=snapshot(record['path'])))
                return dict(outcome='saved', path=record['path'], checkpoint_id=identity, action='Checkpoint is published and discoverable; stop when handing off.')
        predecessor = document.get('predecessor')
        current = ledger.resolve(topic, root)['authoritative']
        if current and (not predecessor or os.path.realpath(os.path.expanduser(predecessor)) != os.path.realpath(current)):
            return dict(outcome='conflict', path=current, action='This topic already has a checkpoint. Supply its authoritative path as predecessor to continue that lineage, or use a distinct topic for independent work.')
        if predecessor:
            predecessor = os.path.realpath(os.path.expanduser(predecessor))
            record = next((r for r in records if os.path.realpath(r['path']) == predecessor), None)
            if not record or ledger.resolve(record['topic'], root)['authoritative'] != record['path']:
                return dict(outcome='conflict', action='Predecessor is not authoritative in this project. Refresh lookup and select the intended lineage.')
            locks.enter_context(ledger.locked(predecessor))
            ledger._check_owner(predecessor, session)
        note = ledger.read_session_note(root, session=session)
        now = datetime.now()
        fields = dict(topic=topic, created=now.isoformat(timespec='microseconds'), status='open',
                      description=str(document.get('description', ''))[:240],
                      project=root, git=ledger._git_position(root), session_id=session,
                      checkpoint_id=identity, content_hash=fingerprint,
                      trigger_id=note.get('trigger_id', '') if note.get('fired') else '',
                      runner_id=os.environ.get('HANDOFF_RUN_ID', ''),
                      reason='context-pressure' if note.get('fired') else 'user-parked')
        if document.get('reason') == 'user-parked':
            fields['reason'] = 'user-parked'
        for key in ('skills', 'references', 'optional_references', 'verify'):
            value = document.get(key, '')
            if isinstance(value, list):
                value = ', '.join(dict.fromkeys(value))
            fields[key] = value
        if predecessor:
            fields['predecessor'] = predecessor
        if any(not isinstance(v, str) or '\n' in v or '\r' in v for v in fields.values()):
            return dict(outcome='invalid', action='Metadata values must be single-line strings or lists of strings.')
        text = '---\n' + ''.join('%s: %s\n' % pair for pair in fields.items()) + '---\n' + body
        if len(text.encode()) > MAX_DOCUMENT_BYTES:
            return dict(outcome='invalid', action='Checkpoint including metadata exceeds 24000 bytes; preserve the draft and shorten the mandatory core.')
        filename = now.strftime('%Y%m%d-%H%M%S-') + topic + '-' + identity[:12] + '.md'
        destinations = list(dict.fromkeys([ledger.save_path(root), ledger._fallback_dir(root), os.path.join(root, '.handoffs')]))
        failures = []
        for directory in destinations:
            path = os.path.join(directory, filename)
            try:
                publish(path, text)
                write_json(path + '.published', dict(checkpoint_id=identity, fingerprint=ledger.content_fingerprint(text)))
                # The successor's predecessor field is the committed lineage record.
                # Old-file cleanup is optional and never makes a saved successor vanish.
                return dict(outcome='saved', path=path, checkpoint_id=identity,
                            action='Checkpoint is published and discoverable; stop when handing off.')
            except OSError as exc:
                if os.path.exists(path):
                    return dict(outcome='blocked', path=path, action='Publication was interrupted after writing the document. Keep the draft and retry the same request ID to finish its receipt; do not clear.')
                failures.append(dict(path=directory, error=type(exc).__name__))
        return dict(outcome='blocked', errors=failures, action='Keep this session and the draft. Both supported locations failed; report access/storage failure. Do not clear or claim the handoff was saved.')


PACKAGE_BYTES = 32_000


def read_bounded(path, limit=MAX_DOCUMENT_BYTES):
    with open(path, 'rb') as f:
        raw = f.read(limit + 1)
    if len(raw) > limit:
        raise ValueError('Required file exceeds the context budget: ' + str(path))
    return raw.decode('utf-8')


def snapshot(path):
    return ledger.content_fingerprint(read_bounded(path))


def receipt_path(root, session):
    return ledger.session_note_path(root, session) + '.prepared'


def write_json(path, value):
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.state-', dir=directory)
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(value, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        dfd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def reference(root, spec):
    import re
    match = re.fullmatch(r'(.+)#L([1-9][0-9]*)-L([1-9][0-9]*)', spec)
    name = match[1] if match else spec
    path = os.path.realpath(os.path.join(root, os.path.expanduser(name)))
    if not match:
        return path, read_bounded(path, PACKAGE_BYTES)
    first, last = int(match[2]), int(match[3])
    if last < first or last - first > 2000 or last > 100000:
        raise ValueError('Invalid reference line range: ' + spec)
    parts, size, count = [], 0, 0
    with open(path, 'rb') as f:
        for count in range(1, last + 1):
            line = f.readline(PACKAGE_BYTES + 1)
            if not line:
                raise ValueError('Reference line range extends past EOF: ' + spec)
            if len(line) > PACKAGE_BYTES:
                raise ValueError('Reference line exceeds the package budget: ' + spec)
            if count >= first:
                parts.append(line)
                size += len(line)
                if size > PACKAGE_BYTES:
                    raise ValueError('Reference excerpt exceeds package budget: ' + spec)
    return path + '#L%d-L%d' % (first, last), b''.join(parts).decode('utf-8')


def prepare(path, root, session, execute=False, catalog=None, budget=PACKAGE_BYTES, reuse_receipt=None):
    import time
    root = ledger.project_root(root)
    errors = []
    ledger._records(root, errors)
    if errors:
        return dict(outcome='blocked', errors=errors[:5], action='Restore read access before preparing; authoritative state may be hidden.')
    resolved = ledger.resolve(path, root)
    path = resolved['authoritative']
    if not path:
        return lookup(root)
    if resolved['chain'][-1]['status'] != 'open':
        return dict(outcome='closed', action='This topic is already transferred or closed. Use history for inspection; do not adopt it.')
    catalog = catalog or {}
    if not isinstance(catalog, dict) or not isinstance(catalog.get('skills', {}), dict) or not isinstance(catalog.get('loaded', []), list):
        raise ValueError('Catalog must contain a skills mapping and a loaded-path list.')
    if not all(isinstance(x, str) for x in list(catalog.get('skills', {}).values()) + catalog.get('loaded', [])):
        raise ValueError('Installed skill paths must be strings.')
    try:
        fingerprint = snapshot(path)
        text = read_bounded(path)
        fm = ledger.parse_front_matter(text)
        problems = ledger._problems(text, fm)
        if problems or not fm:
            raise ValueError('Repair the checkpoint before preparation: ' + '; '.join(problems))
        ledger._check_owner(path, session)
        body = text.split('\n---\n', 1)[-1]
        refs, skills, seen, dependencies = [], [], set(), {}
        required = ledger._split_csv(fm.get('references', ''))
        if len(required) > ledger.MAX_REFS:
            raise ValueError('Too many required references; consolidate constraints and make background optional.')
        for spec in required:
            canonical, content = reference(root, spec)
            if canonical in seen:
                continue
            seen.add(canonical)
            dependencies[canonical] = hashlib.sha256(content.encode()).hexdigest()
            refs.append(dict(path=canonical, text=content))
        # A server-stored receipt plus possession of its opaque token binds reuse
        # to a previous delivery, this session, and unchanged content.
        reference_fingerprints = dict(dependencies)
        delivery_file = receipt_path(root, session) + '.delivery'
        reused_bytes = 0
        if reuse_receipt:
            try:
                delivered = json.loads(read_bounded(delivery_file))
                if (delivered['token'] == reuse_receipt and delivered['path'] == path
                        and delivered['fingerprint'] == fingerprint
                        and delivered['references'] == reference_fingerprints
                        and 0 <= time.time() - delivered['created'] < 7200):
                    reused_bytes = len(body.encode()) + sum(len(r['text'].encode()) for r in refs)
            except (OSError, ValueError, KeyError, TypeError):
                pass  # Missing, expired or changed delivery safely reloads in full.
        loaded = {os.path.realpath(p) for p in catalog.get('loaded', [])}
        # This workflow's loaded instruction file is part of the resume context budget.
        policy_path = os.path.realpath(os.path.join(os.path.dirname(__file__), '..', 'SKILL.md'))
        policy_bytes = len(read_bounded(policy_path, PACKAGE_BYTES).encode())
        if execute:
            policy_bytes += len(read_bounded(os.path.join(os.path.dirname(policy_path), 'continuation.md'), PACKAGE_BYTES).encode())
        loaded.add(policy_path)
        omitted_skills = 0
        if execute:
            names = ledger._split_csv(fm.get('skills', ''))
            if len(names) > 16:
                raise ValueError('Too many required skills; declare only execution dependencies.')
            for name in names:
                source = catalog.get('skills', {}).get(name)
                if not source:
                    raise ValueError('Required skill unavailable in the supplied installed catalog: ' + name)
                canonical = os.path.realpath(source)
                if canonical in seen:
                    omitted_skills += 1
                    continue
                seen.add(canonical)
                content = read_bounded(canonical, PACKAGE_BYTES)
                dependencies[canonical] = hashlib.sha256(content.encode()).hexdigest()
                if canonical in loaded:
                    omitted_skills += 1
                    continue
                skills.append(dict(name=name, path=canonical, text=content))
        result = dict(outcome='prepared' if execute else 'retrieved', path=path, body=body,
                      references=refs, skills=skills,
                      action=('Run verify if requested, reconcile reported workspace changes, then acknowledge before executing next steps.' if execute else
                              'Report the objective and next step. Retrieval is complete; do not claim, verify, load execution skills or execute next steps without authorization.'),
                      verify_required=bool(fm.get('verify')) if execute else False,
                      workspace=ledger.claim_report(path) if execute else [],
                      optional_references=ledger._split_csv(fm.get('optional_references', ''))[:8])
        import uuid
        delivery_token = uuid.uuid4().hex
        result['delivery_receipt'] = delivery_token
        if reused_bytes:
            del result['body']
            result['references'] = []
            result['reused_from'] = reuse_receipt
        # JSON overhead counts too. Loaded skills still consume context even though not re-emitted.
        existing_bytes = sum(len(read_bounded(p, PACKAGE_BYTES).encode()) for p in (loaded & seen) - {policy_path})
        result['metrics'] = dict(body_bytes=len(body.encode()), reference_bytes=sum(len(r['text'].encode()) for r in refs),
                                 skill_bytes=sum(len(s['text'].encode()) for s in skills),
                                 already_loaded_bytes=existing_bytes, reused_bytes=reused_bytes, deduplicated_skills=omitted_skills,
                                 token_estimate_method='UTF-8 bytes / 4; estimate, not tokenizer measurement', budget_bytes=budget)
        result['metrics']['workflow_bytes'] = policy_bytes
        size = len(json.dumps(result, ensure_ascii=False).encode()) + existing_bytes + reused_bytes + policy_bytes + 128
        result['metrics'].update(package_bytes=size, estimated_tokens=(size + 3) // 4)
        if size > budget:
            raise ValueError('Complete resume package exceeds %d bytes; use required excerpts and optional background or an explicitly approved larger --budget-bytes.' % budget)
        if execute:
            with ledger.locked(path):
                ledger._check_owner(path, session)
                if snapshot(path) != fingerprint:
                    raise ValueError('Checkpoint changed during preparation; retry prepare.')
                ledger._set_fields(path, [('claimed', datetime.now().strftime(ledger.STAMP)), ('claim_owner', session)], ('claimed:', 'claim_owner:'))
                try:
                    write_json(receipt_path(root, session), dict(path=path, fingerprint=fingerprint, dependencies=dependencies,
                               prepared_at=time.time(), verified=not bool(fm.get('verify')), metrics=result['metrics']))
                except OSError:
                    ledger._set_fields(path, [], ('claimed:', 'claim_owner:'))
                    raise
        # Cache delivery metadata only; retrieval never mutates checkpoint state.
        # Failure to cache cannot invalidate an otherwise successful preparation.
        try:
            write_json(delivery_file, dict(token=delivery_token, path=path, fingerprint=fingerprint,
                       references=reference_fingerprints, created=time.time()))
        except OSError:
            result.pop('delivery_receipt', None)
        return result
    except (OSError, ValueError) as exc:
        return dict(outcome='needs-context', action=str(exc)[:700] + ' Preserve the checkpoint. Resolve the named dependency or explicitly authorize a budget exception; do not acknowledge or search unrelated history.')


def prepared_receipt(path, root, session):
    import time
    with open(receipt_path(root, session)) as f:
        receipt = json.load(f)
    if os.path.realpath(receipt['path']) != os.path.realpath(path) or time.time() - receipt['prepared_at'] > 7200:
        raise ValueError('Preparation absent or expired; prepare again.')
    resolved = ledger.resolve(path, root)
    if not resolved['chain'] or resolved['chain'][-1]['status'] != 'open' or os.path.realpath(resolved['authoritative']) != os.path.realpath(path):
        raise ValueError('Checkpoint is no longer authoritative open work; refresh lookup.')
    ledger._check_owner(path, session)
    fm = ledger._read_fm(path)
    if fm.get('claim_owner') != session or not ledger._claimed_recently(fm, datetime.now()):
        raise ValueError('Preparation no longer holds a live claim; prepare again.')
    if snapshot(path) != receipt['fingerprint']:
        raise ValueError('Checkpoint changed after preparation; prepare again.')
    for spec, expected in receipt['dependencies'].items():
        _, content = reference(root, spec)
        if hashlib.sha256(content.encode()).hexdigest() != expected:
            raise ValueError('Required dependency changed; prepare again: ' + spec)
    return receipt


def acknowledge(path, root, session):
    root = ledger.project_root(root)
    with ledger.locked(path):
        fm = ledger._read_fm(path)
        if fm.get('status') == 'resumed' and fm.get('resumed_by') == session:
            return dict(outcome='resumed', path=path, action='This session already acknowledged the transfer.')
        receipt = prepared_receipt(path, root, session)
        if not receipt['verified']:
            return dict(outcome='blocked', action='Verification has not passed. Run verify; keep the claim recoverable until preparation succeeds.')
        ledger._set_fields(path, [('status', 'resumed'), ('resumed', datetime.now().strftime(ledger.STAMP)), ('resumed_by', session)],
                           ('status:', 'resumed:', 'resumed_by:', 'claimed:', 'claim_owner:'))
    return dict(outcome='resumed', path=path, action='Transfer acknowledged. Continue the authorized next step.')


def verify(path, root, session, timeout=60):
    import subprocess
    import signal
    root = ledger.project_root(root)
    receipt = prepared_receipt(path, root, session)
    command = ledger._read_fm(path).get('verify')
    if not command:
        return dict(outcome='verified', exit_code=0, action='No recorded verification command; no speculative suite is required.')
    log_dir = os.path.join(os.path.dirname(path), '.verification')
    os.makedirs(log_dir, exist_ok=True)
    fd, log = tempfile.mkstemp(prefix='verify-', suffix='.log', dir=log_dir)
    timed_out = False
    # Bash pipefail preserves failure from any pipeline stage; no output flows into the prompt live.
    with os.fdopen(fd, 'wb') as output:
        process = subprocess.Popen(['bash', '-o', 'pipefail', '-c', command], cwd=root,
                                   stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            code = process.wait(timeout=max(0.05, min(timeout, 3600)))
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(process.pid, signal.SIGKILL)
            code = process.wait()
    outcome = 'verification-timeout' if timed_out else 'verified' if code == 0 else 'verification-failed'
    # Recheck content and ownership after the command; never bless a changed checkpoint.
    receipt = prepared_receipt(path, root, session)
    receipt['verified'] = code == 0 and not timed_out
    write_json(receipt_path(root, session), receipt)
    excerpt = ''
    if code or timed_out:
        with open(log, 'rb') as f:
            f.seek(max(0, os.path.getsize(log) - 2000))
            excerpt = f.read(2000).decode('utf-8', errors='replace')
    return dict(outcome=outcome, exit_code=code, log=log, excerpt=excerpt,
                action='Acknowledge the transfer.' if receipt['verified'] else 'Inspect the bounded failure excerpt or log; fix and retry, or release the claim. The handoff remains recoverable.')


RETRIEVE_COMMANDS = frozenset(('retrieve', 'retrieve your handoff', 'retrieve handoff', 'show handoff', 'show the handoff'))
RESUME_COMMANDS = frozenset(('resume', 'resume handoff', 'resume the handoff', 'continue the handoff'))


def generic_intent(prompt):
    normalized = prompt.strip().lower().rstrip('.!')
    return 'retrieve' if normalized in RETRIEVE_COMMANDS else 'resume' if normalized in RESUME_COMMANDS else None


def opening_action(prompt, result, session, auto=False, newest=False):
    """One authorization policy shared by startup and explicit opening commands."""
    import re
    normalized = prompt.strip().lower().rstrip('.!')
    intent = generic_intent(prompt)
    retrieve, resume = intent == 'retrieve', intent == 'resume'
    selected = None
    for item in result['items']:
        if normalized in ('resume ' + item['topic'].lower(), 'retrieve ' + item['topic'].lower()) or prompt.strip() in ('resume ' + item['path'], 'retrieve ' + item['path']):
            selected = item
            resume = normalized.startswith('resume ')
            retrieve = not resume
            break
    if prompt and not retrieve and not resume:
        return None
    if result['outcome'] != 'available':
        return result['action']
    items = result['items']
    if not prompt and not auto:
        return '%d open handoff(s) available. Continue the current request; retrieve or resume explicitly when relevant.' % result['total']
    if not selected and result['total'] > 1 and not newest:
        return '%d open handoffs. Choose one (or none): ' % result['total'] + '; '.join('%d) %s (ended %s, %.0fd old): %s' % (i+1,h['topic'][:80],h['ended'][:32],h['age_days'],h['description'][:180]) for i,h in enumerate(items)) + ('. and %d more; run lookup --offset 5' % (result['total']-5) if result['total']>5 else '') + ('. %d open handoff(s) older than 14 days; lookup --max-age-days 9999' % result['stale'] if result['stale'] else '') + '. Selection retrieves; explicit resume authorizes execution.'
    selected = selected or items[0]
    if not session:
        return 'An open handoff is available. Supply a unique session identity to prepare; never borrow another session identity.'
    command = 'python3 %s prepare %s --session %s' % (
        shlex.quote(os.path.join(os.path.dirname(__file__), 'handoff_ledger.py')),
        shlex.quote(selected['path']), shlex.quote(session))
    execute = resume or (auto and not retrieve)
    if execute:
        command += ' --execute'
    suffix = ' Prepare and verify, then acknowledge before continuing the authorized work.' if execute else ' Retrieve only; leave execution skills, verification and next steps until continuation is authorized.'
    return "Handoff '%s' (ended %s, %.0fd old) — %s. " % (selected['topic'][:80],selected['ended'][:32],selected['age_days'],selected['description'][:240]) + ('Fully automatic: newest of %d; proceed without asking. ' % result['total'] if newest else 'Proceed without asking. ' if auto and execute else '') + 'Run ' + command + '.' + suffix


def history(root, topic=None, offset=0, limit=10):
    records = ledger._records(ledger.project_root(root))
    records = sorted((r for r in records if topic is None or r['topic'] == topic),
                     key=lambda r: (r['ended'], r['path']), reverse=True)
    limit = max(1, min(20, limit))
    offset = max(0, offset)
    items = [dict(path=r['path'], topic=r['topic'][:80], status=r['fm'].get('status', 'open')[:32],
                  description=r['fm'].get('description', '')[:240], created=r['ended'][:64])
             for r in records[offset:offset + limit]]
    return dict(outcome='history', items=items, total=len(records),
                next_offset=offset + limit if offset + limit < len(records) else None,
                action='History is inspection only. Closed records are not automatically adopted.')


def protocol_version():
    import re
    path = os.path.join(os.path.dirname(__file__), '..', 'SKILL.md')
    match = re.search(r'version:\s*"([^"\n]+)"', read_bounded(path))
    return match[1] if match else 'unknown'


def telemetry_report(path):
    """Bounded aggregate; legacy evidence is explicitly unclassified."""
    from collections import defaultdict
    groups = defaultdict(lambda: dict(count=0, package_bytes=0, reused_bytes=0))
    excluded = unknown = live = invalid = 0
    with open(path, 'rb') as f:
        size = os.fstat(f.fileno()).st_size
        truncated = size > 2_000_000
        if truncated:
            f.seek(-2_000_000, os.SEEK_END)
            f.readline()
        for line in f:
            try:
                r = json.loads(line)
                if not isinstance(r, dict):
                    raise ValueError('invalid record')
                sid = str(r.get('session_id') or '')
                if r.get('origin') == 'test' or sid.startswith('verify-') or sid == 'runner':
                    excluded += 1
                    continue
                if r.get('origin') != 'live':
                    unknown += 1
                    continue
                key = tuple(str(r.get(k, 'unknown'))[:64] for k in ('version', 'operation', 'outcome'))
                if key not in groups and len(groups) >= 40:
                    key = ('other', 'other', 'other')
                metrics = r.get('metrics') or {}
                values = {k: max(0, int(metrics.get(k, 0))) for k in ('package_bytes', 'reused_bytes')}
                live += 1
                groups[key]['count'] += 1
                for k, value in values.items():
                    groups[key][k] += value
            except (ValueError, TypeError, AttributeError):
                invalid += 1
    return dict(outcome='report', live_records=live, excluded_tests=excluded,
                unclassified=unknown, malformed=invalid, tail_only=truncated,
                groups=[dict(version=k[0], operation=k[1], outcome=k[2], **v) for k, v in sorted(groups.items())],
                note='Bytes are not billed tokens. Legacy origin is unknown; absent preparation records cannot establish savings.')


def telemetry(operation, root, session, result):
    """Only component sizes and outcomes; never copy handoff or verification content."""
    import time
    raw = os.environ.get('CONTEXT_WATCH_RESUME_LOG')
    if raw == '0' or os.environ.get('CONTEXT_WATCH_LOG') == '0':
        return
    path = os.path.expanduser(raw or '~/.context-watch/resumes.jsonl')
    note = ledger.read_session_note(root, session=session)
    record = dict(operation=operation, outcome=result['outcome'], timestamp=time.time(),
                  origin=os.environ.get('CONTEXT_WATCH_ORIGIN', 'live'), version=protocol_version(),
                  project_id=hashlib.sha256(ledger.project_root(root).encode()).hexdigest()[:16],
                  session_id=session, metrics=result.get('metrics', {}),
                  startup_input_tokens=note.get('startup_input_tokens'),
                  observed_input_tokens=note.get('observed_input_tokens'))
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, 'a') as f:
            f.write(json.dumps(record) + '\n')
    except OSError:
        pass  # optional analytics never makes a completed checkpoint fail


def cli(argv):
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='command', required=True)
    report_p = sub.add_parser('report', description='Summarize live resume metrics, excluding tests and separating legacy origin.')
    report_p.add_argument('--log', default=os.path.expanduser('~/.context-watch/resumes.jsonl'))
    lookup_p = sub.add_parser('lookup')
    lookup_p.add_argument('root', nargs='?', default='.')
    lookup_p.add_argument('--max-age-days', type=int, default=14)
    lookup_p.add_argument('--offset', type=int, default=0)
    lookup_p.add_argument('--topic')
    save_p = sub.add_parser('save', description='Publish a JSON draft: topic (kebab-case), description, body (Markdown with Objective, Current state and Next steps headings). Optional: predecessor, skills, references, optional_references, verify, reason. Metadata is generated. Use --template for a valid draft; edit its facts before saving.')
    save_p.add_argument('root', nargs='?', default='.')
    save_p.add_argument('--session')
    save_p.add_argument('--request-id')
    save_p.add_argument('--template', action='store_true', help='Print a valid JSON draft without writing a checkpoint')
    save_p.add_argument('--input', help='JSON draft path; default stdin')
    prepare_p = sub.add_parser('prepare')
    prepare_p.add_argument('path')
    prepare_p.add_argument('--root', default='.')
    prepare_p.add_argument('--session', required=True)
    prepare_p.add_argument('--execute', action='store_true')
    prepare_p.add_argument('--reuse-receipt', help='Receipt from content still in this session context; omit after context loss')
    prepare_p.add_argument('--catalog', help='JSON installed skills mapping plus already-loaded canonical paths')
    prepare_p.add_argument('--budget-bytes', type=int, default=PACKAGE_BYTES)
    ack_p = sub.add_parser('acknowledge')
    ack_p.add_argument('path')
    ack_p.add_argument('--root', default='.')
    ack_p.add_argument('--session', required=True)
    verify_p = sub.add_parser('verify')
    verify_p.add_argument('path')
    verify_p.add_argument('--root', default='.')
    verify_p.add_argument('--session', required=True)
    verify_p.add_argument('--timeout', type=float, default=60)
    history_p = sub.add_parser('history')
    history_p.add_argument('root', nargs='?', default='.')
    history_p.add_argument('--topic')
    history_p.add_argument('--offset', type=int, default=0)
    history_p.add_argument('--limit', type=int, default=10)
    a = p.parse_args(argv)
    if a.command == 'report':
        try:
            result = telemetry_report(a.log)
        except FileNotFoundError:
            result = dict(outcome='no-data', action='No telemetry log exists; no delivery measurements are available.')
        except OSError as exc:
            result = dict(outcome='unavailable', action='Cannot read telemetry: ' + str(exc)[:300])
        print(json.dumps(result))
        return 1 if result['outcome'] == 'unavailable' else 0
    if a.command == 'save':
        if a.template:
            print(json.dumps(dict(topic='task-checkpoint', description='Replace with the actual task state.',
                body='## Objective\nState the authorized goal and constraints.\n## Current state\nRecord verified evidence, pending work and decisions.\n## Next steps\nName the exact next action.\n')))
            return 0
        if not a.session or not a.request_id:
            p.error('save requires --session and --request-id; use save --template for the JSON draft')
    try:
        if a.command == 'history':
            result = history(a.root, a.topic, a.offset, a.limit)
        elif a.command == 'verify':
            result = verify(a.path, a.root, a.session, a.timeout)
        elif a.command == 'prepare':
            catalog = json.loads(read_bounded(a.catalog)) if a.catalog else None
            result = prepare(a.path, a.root, a.session, a.execute, catalog, a.budget_bytes, a.reuse_receipt)
        elif a.command == 'acknowledge':
            result = acknowledge(a.path, a.root, a.session)
        elif a.command == 'save':
            if a.input:
                with open(a.input, encoding='utf-8') as f:
                    raw = f.read(MAX_DOCUMENT_BYTES * 4 + 1)
            else:
                raw = sys.stdin.read(MAX_DOCUMENT_BYTES * 4 + 1)
            if len(raw) > MAX_DOCUMENT_BYTES * 4:
                raise ValueError('Draft exceeds input limit; keep it and reduce the mandatory core.')
            result = save(a.root, a.session, a.request_id, json.loads(raw))
        else:
            result = lookup(a.root, a.max_age_days, a.offset, a.topic)
    except (OSError, ledger.ConflictError, ValueError, TypeError, KeyError) as exc:
        result = dict(outcome='blocked', action=str(exc)[:500] + '; preserve the draft/checkpoint and restore access or correct the input before retrying. Do not clear or adopt.')
    result.setdefault('project', ledger.project_root(getattr(a, 'root', '.')))  # shows where a 'none' looked
    telemetry(a.command, a.root, getattr(a, 'session', None), result)
    output = json.dumps(result, ensure_ascii=False)
    limit = getattr(a, 'budget_bytes', PACKAGE_BYTES)
    if len(output.encode()) > max(1024, limit):
        result = dict(outcome='needs-context', action='Response metadata exceeds the output budget. Use a narrower topic or history page; inspect and repair oversized metadata locally before preparing.')
        output = json.dumps(result)
    print(output)
    return 1 if result['outcome'] in ('error', 'blocked', 'invalid', 'conflict', 'needs-context', 'verification-failed', 'verification-timeout') else 0
