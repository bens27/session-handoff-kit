"""Public handoff workflow with advisory size targets. Stdlib only; legacy ledger is the file adapter."""
from handoff_contract import project_output, continuity_items, continuation_document

import argparse
import errno
import json
import os
import re
import shlex
import sys
import hashlib
import tempfile
import time
from datetime import datetime

import handoff_identity
import handoff_ledger as ledger
import handoff_workspace as workspace
from handoff_record import Record
from handoff_identity import default_session, terminal_id, terminal_session, truthy as _truthy  # noqa: F401

# A native CLI can parse a bare "resume" positional as its own subcommand.
AGENTSROOM_RESTART_PROMPT = 'continue the handoff'


def decode_state_json(raw):
    """Shared state is an object; malformed/deep JSON is recoverable corruption."""
    try:
        value = json.loads(raw)
    except (ValueError, RecursionError):
        raise ValueError('Malformed or excessively nested handoff state JSON.') from None
    if not isinstance(value, dict):
        raise ValueError('Handoff state must be a JSON object.')
    return value


def read_cli_input(path='-'):
    """Read strict UTF-8 from a CLI input file, or stdin for the conventional dash."""
    if path == '-':
        return sys.stdin.buffer.read().decode('utf-8')
    with open(path, encoding='utf-8') as stream:
        return stream.read()


def load_state_json(path, limit=None):
    with open(path, encoding='utf-8') as stream:
        return decode_state_json(stream.read() if limit is None else stream.read(limit))


def hooks_dir():
    """The one hooks directory every emitted command names: the installed
    ~/.agents/skills path when it resolves to these hooks, else their real path
    (same rule as context_watch.canonical_ledger)."""
    here = os.path.realpath(os.path.dirname(os.path.abspath(__file__)))
    installed = os.path.join(os.path.expanduser('~'), '.agents', 'skills', 'session-handoff', 'hooks')
    return installed if os.path.isdir(installed) and os.path.realpath(installed) == here else here


def session_info(root):
    """Read live hook facts without scanning transcripts or changing identity."""
    recorded = terminal_session(root)
    session = default_session(root)
    note = ledger.read_session_note(root, session=session)
    return dict(outcome='recorded' if recorded else 'unrecorded',
                project=handoff_identity.project_root(root), terminal=terminal_id(),
                session_id=session, identity_source='hook' if recorded else ('host' if handoff_identity.host_session() else ('environment' if session else 'none')),
                host_session_id=handoff_identity.host_session(),
                transcript_path=note.get('transcript_path') or '',
                pressure_fired=bool(note.get('fired')),
                action=('Use the recorded session; omit --session for save/resume.' if recorded else
                        'Use the current host session; omit --session for save/resume.' if handoff_identity.host_session() else
                        'No hook-recorded identity. Use --session with this session\'s actual id; '
                        'generate one UUID only if the host supplies none. Transcript location is unavailable unless recorded by a hook.'))


NO_SESSION = dict(outcome='blocked', action='No session identity found: pass --session <this session id> (or set HANDOFF_SESSION_ID) and rerun. Never borrow another session identity.')


def paused_reasons(value, limit=120):
    """Guard-reason tokens only: front matter is file content and never reaches the model raw."""
    tokens = [t for t in ledger._split_csv(str(value)) if re.fullmatch(r'[a-z0-9-]+', t)]
    text = ', '.join(tokens)
    return text if len(text) <= limit else text[:limit].rsplit(',', 1)[0]


def hook_opening_note(event):
    """Read-only selection for the bounded hook worker; never adopt foreign work."""
    root = handoff_identity.project_root(event.get('cwd') or os.getcwd())
    session = str(event.get('session_id') or '')
    terminal = terminal_id()
    errors = []
    records = ledger._records(root, errors)
    if errors:
        return ''
    def owned(record):
        fm = record['fm']
        if fm.get('terminal'):
            return bool(terminal) and fm['terminal'] == terminal
        return bool(session) and fm.get('session_id') == session
    own = [r for r in records if owned(r) and
           (r['record'].is_open or r['record'].status == 'unpublished'
            or pending_delivery(root, r['path'], r['fm'], session))]
    if event.get('hook_event_name') == 'SessionStart':
        # Ordinary open topics must not hide publication repair for this terminal.
        own = [r for r in own if r['record'].status == 'unpublished']
        own.sort(key=lambda r: r['mtime'], reverse=True)
        own = own[:1]
    if not own:
        return ''
    pointer = 'handoff-status: Owned handoff state needs selection or trust review; load the session-handoff skill and ask before executing untrusted verification.'
    if len(own) != 1:
        return '' if event.get('hook_event_name') == 'SessionStart' else pointer
    record = own[0]
    checkpoint, path = record['record'], record['path']
    if checkpoint.status == 'unpublished':
        recovery = publication_recovery(root, path)
        return 'handoff-status: ' + recovery['text']
    if event.get('hook_event_name') == 'SessionStart':
        return ''
    # resolve() follows a topic's newest successor, even when a path was supplied.
    # Only advertise the shortcut when that authoritative record is also owned.
    authoritative = ledger.resolve(path, root)['authoritative']
    if checkpoint.paused and not record['problems'] and checkpoint.trusted():
        # A host-injected restart prompt reads like a typed one, so paused state never gets the lean command
        return ('handoff-status: Owned handoff %s is paused (%s); load the session-handoff skill and ask the user '
                'before continuing it.' % (json.dumps(path), paused_reasons(checkpoint.paused) or 'guard'))
    if (record['problems'] or checkpoint.paused
            or ledger._claimed_recently(checkpoint.fm, datetime.now())
            or authoritative != path or not checkpoint.trusted()):
        return pointer
    command = 'python3 %s resume %s --session %s --fields path,body' % (
        shlex.quote(os.path.join(hooks_dir(), 'handoff_ledger.py')),
        shlex.quote(path), shlex.quote(session))
    if not session:
        return pointer
    return ('The session-handoff skill does not need to load. Run %s; follow its outcome/action, '
            'then continue with ## Next steps in the returned body. Run it exactly as given, with no head, '
            'pipe or redirect: its output is the complete required package.' % command)


def publication_recovery(root, path):
    """Describe a concrete recovery without blessing changed or legacy content."""
    repairable = False
    fm = {}
    try:
        text = read_text(path)
        checkpoint = Record(path, text)
        fm = checkpoint.fm
        intent = load_state_json(recovery_path(root, path))
        key = hashlib.sha256((os.path.realpath(ledger.project_root(root)) + '\0' + os.path.realpath(path)).encode()).hexdigest()
        repairable = (checkpoint.open_strict and intent.get('path') == os.path.realpath(path)
                      and os.path.realpath(intent.get('root', '')) == os.path.realpath(ledger.project_root(root))
                      and ledger.valid_seal(intent.get('seal'), key, hashlib.sha256(text.encode()).hexdigest()))
    except (OSError, ValueError, TypeError):
        pass
    cli = 'python3 ' + shlex.quote(os.path.join(hooks_dir(), 'handoff_ledger.py'))
    abandon = cli + ' abandon ' + shlex.quote(path)
    command = (cli + ' repair ' + shlex.quote(path) + ' --root ' + shlex.quote(ledger.project_root(root))
               if repairable else abandon)
    text = ('Incomplete checkpoint %s. ' % json.dumps(path))
    if repairable:
        text += 'In the publishing terminal, complete the unchanged publication with: %s. ' % command
    else:
        original = '%s save --session %s --request-id <original-request-id> --input <original-draft.json>' % (
            cli, shlex.quote(fm.get('session_id') or ''))
        text += ('No authenticated intent matches the current checkpoint. Preserve it for inspection; '
                 'if available, retry the original save request: %s. '
                 'Otherwise ask the user before explicitly abandoning it with: %s. '
                 'Abandonment closes the record without trusting or inheriting its content. ' % (original, abandon))
    text += 'Keep the session; do not clear. An unchanged resume retry cannot repair publication.'
    return dict(path=path, kind='repair' if repairable else 'abandon', command=command, text=text)


def lookup(root='.', max_age=14, offset=0, topic=None, terminal=None, session=None):
    stats = {}
    items = ledger.scan(root, max_age, stats)
    hidden = 0
    if terminal:
        hidden = sum(1 for h in items if h.get('terminal') != terminal)
        items = [h for h in items if h.get('terminal') == terminal]
    if topic is not None:
        items = [h for h in items if h['topic'] == topic or os.path.realpath(h['path']) == os.path.realpath(topic)]
    current = terminal_id()
    session = session or default_session(root)
    for h in items:
        # A recorded terminal takes precedence; legacy untagged ownership can
        # be established only by the actual current session, never by absence.
        h['mine'] = (bool(current) and h['terminal'] == current if h.get('terminal')
                     else bool(session) and h.get('session_id') == session)
    items.sort(key=lambda h: not h['mine'])  # stable: newest-first within each group
    mine = [h for h in items if h['mine']]
    # A missing own checkpoint must not make foreign or unowned work
    # become the implicit recovery target. Explicit path selection still works.
    sole = items[0] if len(items) == 1 else None
    selected = (mine[0]['path'] if len(mine) == 1 else sole['path']
                if sole and not current else None)
    outcome = ('error' if stats['errors'] else 'incomplete' if stats['incomplete'] else 'available' if items else
               'claimed' if stats['claimed'] else 'stale' if stats['stale'] else 'none')
    actions = {
        'incomplete': 'Select an incomplete publication for recovery.',
        'none': 'No open handoff found. Continue the current task; for an explicit retrieval request report this result. Use history to inspect closed work.',
        'claimed': 'The handoff is held by another session. Continue unrelated work or wait for release; do not adopt it.',
        'stale': 'Only stale handoffs remain. Run lookup --max-age-days 9999 to select one explicitly.',
        'error': 'Handoff lookup failed. Report the unreadable locations; do not treat this as absence or adopt a possibly stale record.',
        'available': 'Select a handoff, then prepare it. Retrieval alone does not authorize executing its next steps.',
    }
    result = dict(outcome=outcome, action=actions[outcome], items=items[max(0, offset):max(0, offset)+5], total=len(items),
                  stale=stats['stale'], claimed=stats['claimed'], incomplete=stats['incomplete'], errors=stats['errors'][:5])
    if outcome == 'incomplete':
        result['recovery'] = [publication_recovery(root, p) for p in stats['incomplete_paths'][:5]]
        result['action'] = result['recovery'][0]['text']
    if selected:
        result['selected'] = selected
    if hidden:
        result['other_sessions'] = hidden
        if outcome == 'none':
            result['action'] = ('No handoff for this terminal; %d open handoff(s) belong to other sessions '
                                '(run lookup to list them). Continue the current task; do not adopt them automatically.' % hidden)
    return result


RECOMMENDED_DOCUMENT_BYTES = 24_000


def project_setting(root, key, kind, user_only=False):
    """A typed key from ~/.context-watch/thresholds.json overridden by <root>/.context-watch.json."""
    value = None
    paths = (os.path.join(os.path.expanduser('~'), '.context-watch', 'thresholds.json'),
             os.path.join(root, '.context-watch.json'))
    for path in paths[:1] if user_only else paths:
        try:
            data = load_state_json(path)
            if isinstance(data, dict) and type(data.get(key)) is kind:
                value = data[key]
        except (OSError, ValueError):
            continue
    return value


def workspace_changes_hash(root):
    """Bounded Git working-file evidence; unknown evidence never proves progress."""
    listed = workspace.changed_files(root)
    if listed is None:
        return ''
    try:
        if len(listed) > 200_000:
            return ''
        digest = hashlib.sha256()
        total = 0
        for raw in sorted(set(listed.split(b'\0')) - {b''}):
            name = os.fsdecode(raw)
            if name.endswith('/') or name.split('/')[0] in ('.handoffs', '.agentsroom', '.git'):
                continue
            path = os.path.join(root, name)
            if os.path.islink(path):
                data = os.fsencode(os.readlink(path))
            elif not os.path.exists(path):
                data = b'<deleted>'
            else:
                with open(path, 'rb') as f:
                    data = f.read(2_000_001)
            total += len(data)
            if total > 2_000_000:
                return ''
            digest.update(raw + b'\0' + hashlib.sha256(data).digest())
        return digest.hexdigest()
    except OSError:
        return ''


def save_progress(root, current, continuity, previous, baseline=False):
    """Changed draft hash AND (changed register, forward commits, or working files).

    The caller supplies a trusted same-topic pressure predecessor, or with baseline=True
    the checkpoint this session resumed under a valid transfer receipt.
    Missing legacy evidence is unknown, not progress. Register ordering is immaterial.
    """
    # baseline: previous is the checkpoint this session resumed; topic/reason need not match.
    eligible = bool(previous) and (baseline or (previous.get('topic') == current.get('topic')
                and previous.get('reason') == current.get('reason') == 'context-pressure'))
    changed = eligible and bool(previous.get('content_hash')) and previous['content_hash'] != current['content_hash']
    try:
        register_changed = eligible and sorted(continuity_items(continuity), key=lambda x: x['id']) != sorted(
            continuity_items(json.loads(previous.get('continuity', '[]'))), key=lambda x: x['id'])
    except (ValueError, TypeError, RecursionError):
        register_changed = False  # Malformed evidence is unknown, never progress.
    before, after = previous.get('workspace_changes_hash'), current.get('workspace_changes_hash')
    files_changed = eligible and bool(before) and bool(after) and before != after
    commits = False
    if eligible:
        old = previous.get('git', '').rsplit('@', 1)[-1]
        new = current.get('git', '').rsplit('@', 1)[-1]
        if old != new and re.fullmatch(r'[0-9a-f]{7,64}', old) and re.fullmatch(r'[0-9a-f]{7,64}', new):
            commits = workspace.is_ancestor(root, old, new)
    return dict(present=bool(changed and (register_changed or commits or files_changed)),
                predecessor_known=bool(eligible), content_changed=bool(changed),
                continuity_changed=bool(register_changed), new_commits=commits,
                files_changed=bool(files_changed))


def predecessor_progress(root, fields, continuity):
    predecessor = fields.get('predecessor')
    prior = Record.load_trusted(predecessor) if predecessor else None
    previous = prior.fm if prior else {}
    return save_progress(root, fields, continuity, previous)


def restart_guard(root, session, topic=None, records=None, pressure=True, progress=None):
    """Conservative restart-intent budget, isolated to the current terminal.

    Saved intents are observable; the host's actual restart counter is not.
    Never claim these counts include restarts performed outside this kit.
    """
    import time
    now = time.time()
    note = ledger.read_session_note(root, session=session)
    terminal = terminal_id()
    rows = records if records is not None else ledger._records(root)
    own = [r for r in rows if (r['fm'].get('terminal') == terminal if terminal else
                              r['fm'].get('session_id') == session)
           and r['record'].trusted()]
    def age(value):
        try:
            return now - datetime.fromisoformat(value).timestamp()
        except (ValueError, TypeError):
            return float('inf')
    recent = [r for r in own if 0 <= age(r['fm'].get('created')) < 3600]
    intents = [r for r in recent if r['fm'].get('restart_intent') == 'agents_restart'
               or ('restart_intent' not in r['fm'] and r['fm'].get('reason') == 'context-pressure'
                   and not r['record'].paused)]
    transfers = [r for r in rows if r['fm'].get('resumed_by') == session
                 and r['record'].trusted()]
    last_transfer = min(transfers, key=lambda r: age(r['fm'].get('resumed')), default=None)
    latest_pressure = min((r for r in recent if r['fm'].get('reason') == 'context-pressure'),
                          key=lambda r: age(r['fm'].get('created')), default=None)
    topic = (topic or note.get('resumed_topic') or (last_transfer['topic'] if last_transfer else None)
             or (latest_pressure['topic'] if latest_pressure else None))
    same = [r for r in recent if r['fm'].get('reason') == 'context-pressure' and topic and r['fm'].get('topic') == topic
            and age(r['fm'].get('created')) < 1800]
    resumed_at = note.get('resumed_at')
    elapsed = now - resumed_at if type(resumed_at) in (int, float) else None
    if elapsed is None and last_transfer:
        elapsed = age(last_transfer['fm'].get('resumed'))
    reasons = []
    if pressure and elapsed is not None and 0 <= elapsed < 600:
        reasons.append('rapid-resume')
    if pressure and len(same) >= 2:
        reasons.append('repeated-topic')
    if os.environ.get('AGENTSROOM_AGENT_ID') and len(intents) >= 5:
        reasons.append('restart-budget')
    if os.environ.get('AGENTSROOM_AGENT_ID') and any(
            r['fm'].get('session_id') != session and age(r['fm'].get('created')) < 120 for r in intents):
        reasons.append('restart-cooldown')
    # An orchestrator topic resumes itself by design: the soft reasons never pause it.
    # The hard restart limits are not affected.
    topics = project_setting(root, 'continuous_topics', list) or []
    continuous = bool(topic) and topic in [t for t in topics if isinstance(t, str)]
    pause_reasons = [r for r in reasons if r in ('restart-budget', 'restart-cooldown')
                     or (progress is not None and not progress['present'] and not continuous)]
    return dict(reasons=reasons, pause_reasons=pause_reasons, continuous_topic=continuous, progress=progress, seconds_since_resume=round(elapsed) if elapsed is not None else None,
                topic_saves_30m=len(same), restart_intents_1h=len(intents),
                startup_input_tokens=note.get('startup_input_tokens'),
                observed_input_tokens=note.get('observed_input_tokens'),
                resume_delivery_tokens=note.get('resume_delivery_tokens'))


def _transition(root, guard=None):
    """What ends this session after a save, deduced from the environment and config."""
    if (os.environ.get('HANDOFF_AUTO') or '').strip():
        auto = _truthy(os.environ.get('HANDOFF_AUTO'))
    else:
        # Unset means on inside AgentsRoom (its restart is safe and rate-limited by
        # the app), off elsewhere; an explicit "auto": false always wins.
        setting = project_setting(root, 'auto', bool)
        auto = setting if setting is not None else bool(os.environ.get('AGENTSROOM_AGENT_ID'))
    if auto and guard and guard['pause_reasons']:
        return dict(kind='paused', diagnostics=guard, action=(
            'Automatic handoff paused: %s. The saved checkpoint remains open. '
            'Stop and report session thrashing with these diagnostics. After reducing re-entry context '
            'or waiting for the restart budget, start a fresh session and explicitly ask to '
            'continue the handoff. If a host restart is refused, keep this checkpoint open; '
            'report the refusal and use the same manual continuation, without retrying the restart.'
            % ', '.join(guard['pause_reasons'])))
    if auto and os.environ.get('AGENTSROOM_AGENT_ID'):
        return dict(kind='agents_restart', requires_preflight=True, action=(
            'Settle and inventory every worker you delegated (agents_spawned_list includeClosed:true, mailbox '
            'and continuity delegations; each as {id, report:{path,sha256}}), check live holds/needsInput, then run '
            'python3 %s restart --root %s --workers \'<JSON list>\' --host-capable [--hold]. Pass '
            '--workers \'[]\' when nothing is outstanding; a settled worker with no report file is '
            '{id, settled: true, report: null}; a mailbox delegation still in flight is '
            '{"id": "<name>", "state": "in_flight", "via": "mailbox"}. Omitting --workers blocks with worker-inventory-unknown. Only outcome: ready and dispatch: true permit calling AgentsRoom '
            'agents_restart for agent %s as the final tool action with the returned restart_prompt, never bare '
            '"%s"; then end the turn. Blocked means manual fresh-session continuation; pending intent requires '
            'reconciliation, never redispatch. Preserve human holds and worker results.')
            % (shlex.quote(os.path.join(hooks_dir(), 'restart_preflight.py')),
               shlex.quote(os.path.realpath(handoff_identity.project_root(root))),
               os.environ['AGENTSROOM_AGENT_ID'].strip(), AGENTSROOM_RESTART_PROMPT))
    if auto and os.environ.get('HANDOFF_RUN_ID'):
        return dict(kind='runner', action='Stop now; the auto runner starts the next session and resumes.')
    if auto and os.environ.get('TMUX_PANE'):
        return dict(kind='tmux', action='Stop now; the Stop hook types /clear and resume into this pane.')
    off = '' if auto else (' Automatic handoff is off: set "auto": true in ~/.context-watch/thresholds.json'
                           ' (or HANDOFF_AUTO=1) to make the next save restart the session itself.')
    if auto or _truthy(os.environ.get('AUTORESUME')) or _truthy(os.environ.get('CONTEXT_WATCH_AUTORESUME')):
        return dict(kind='clear', action='Tell the user to type /clear; the cleared session resumes this handoff.' + off)
    return dict(kind='new-session', action='Tell the user to start a new session in this project and ask it to resume.' + off)


def transition(root, guard=None):
    result = _transition(root, guard)
    if guard and guard['reasons']:
        result['diagnostics'] = guard
        if not guard['pause_reasons']:
            result['warning'] = 'Thrash warning: %s; checkpoint progress permits automatic continuation.' % ', '.join(guard['reasons'])
            result['action'] = result['warning'] + ' ' + result['action']
    return result


def predecessor_project(predecessor):
    """The project recorded in a predecessor checkpoint, or ''."""
    try:
        text = read_text(os.path.expanduser(predecessor))
    except (OSError, ValueError):
        return ''
    return Record(predecessor, text).get('project', '')


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


def _listed(value):
    if isinstance(value, list):
        value = ', '.join(dict.fromkeys(value))
    return value if isinstance(value, str) else None


def _front_matter_matches(root, document, record):
    """True when the stored metadata is what save would write for this document.
    A verify the document omitted may only be the user-level default or the trusted predecessor's."""
    fm = record['fm']
    held = Record.load_trusted(fm['predecessor']) if fm.get('predecessor') else None
    prior = held.fm if held else {}
    effective = continuation_document(document, prior)
    for key in ('skills', 'references', 'optional_references', 'verify_baseline'):
        if fm.get(key, '') != _listed(effective.get(key, '')):
            return False
    if json.loads(fm.get('continuity', '[]')) != effective['continuity']:
        return False
    allowed = {_listed(document.get('verify', '')) or None}
    if not document.get('verify'):
        allowed = {'', project_setting(root, 'verify', str, user_only=True) or ''}
        predecessor = fm.get('predecessor')
        if predecessor:
            prior = next((r for r in ledger._records(root, []) if os.path.realpath(r['path']) == os.path.realpath(predecessor)), None)
            if prior and prior['record'].trusted():
                allowed.add(prior['fm'].get('verify', ''))
    return fm.get('verify', '') in allowed


def resumed_holder(root, path, fm):
    """Local receipt proves who performed the latest transfer, not who saved it."""
    try:
        receipt = read_receipt(root, fm.get('resumed_by', ''), path)
        if (valid_transfer_receipt(root, fm.get('resumed_by', ''), path, receipt)
                and os.path.realpath(receipt['path']) == os.path.realpath(path)
                and receipt['fingerprint'] == snapshot(path)
                and receipt.get('acknowledged_at') == fm.get('resumed')
                and receipt.get('verified')):
            return receipt
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return {}


def owns_transfer(root, path, fm, session):
    holder = resumed_holder(root, path, fm)
    # A terminal is the stronger identity: a shared runner cannot override a
    # different terminal's transfer.
    if holder.get('terminal'):
        return bool(terminal_id()) and holder['terminal'] == terminal_id()
    runner = os.environ.get('HANDOFF_RUN_ID', '')
    if holder.get('runner_id'):
        return bool(runner) and holder['runner_id'] == runner
    if not holder and fm.get('transfer_receipt'):
        # This kit wrote a signed receipt at acknowledgement; without a usable
        # one the holder is untraceable, never inferred from a session ID.
        return False
    return fm.get('resumed_by') == session  # true legacy transfer: no receipt was ever written


def original_retry_session(root, session, request_id):
    """True when SESSION/REQUEST_ID name an interrupted save published from this terminal.

    The hook pointer normally wins over an explicit --session; only a retry of the exact
    unpublished checkpoint the same terminal wrote may keep its original saving identity.
    """
    terminal = terminal_id()
    if not session or not request_id or not terminal:
        return False
    try:
        root = handoff_identity.project_root(root)
        identity = hashlib.sha256((os.path.realpath(root) + '\0' + session + '\0' + request_id).encode()).hexdigest()
        return any(r['fm'].get('checkpoint_id') == identity and r['fm'].get('status') == 'unpublished'
                   and r['fm'].get('session_id') == session and r['fm'].get('terminal') == terminal
                   for r in ledger._records(root, []))
    except (OSError, ValueError):
        return False  # Unreadable state keeps hook identity; save itself reports the failure.


def project_hook_snapshot(workspace):
    """Hash project-scope config bytes; missing files are part of the baseline."""
    result = {}
    for name in ('.claude/settings.json', '.claude/settings.local.json'):
        path = os.path.join(workspace, name)
        try:
            with open(path, 'rb') as stream:
                result[name] = hashlib.sha256(stream.read()).hexdigest()
        except FileNotFoundError:
            result[name] = None
    return result


INBOX_FIRST = ('Read your AgentsRoom inbox with agents_read_inbox and handle relevant pending mail '
               'before continuing the saved work.')


def inbox_document(root, document):
    """Persist a mail-check obligation, without inventing an unread count."""
    terminal = terminal_id()
    agentsroom = bool(os.environ.get('AGENTSROOM_AGENT_ID') or (
        terminal and os.path.isfile(os.path.join(root, '.agentsroom', 'sessions', terminal + '.json'))))
    if not agentsroom:
        return document
    updated = dict(document)
    items = continuity_items(document.get('continuity', []))
    updated['continuity'] = [item for item in items if item['id'] != 'agentsroom-inbox'] + [dict(
        id='agentsroom-inbox', status='open',
        text='Pending mail status unknown; read agents_read_inbox first on successor continuation.')]
    body = document.get('body')
    if isinstance(body, str):
        import re
        heading = re.search(r'^## Next steps(?:[ \t][^\n]*)?$\n', body, re.M)
        if heading:
            before, after = body[:heading.end()], body[heading.end():]
            if not after.lstrip().startswith(INBOX_FIRST):
                updated['body'] = before + INBOX_FIRST + '\n' + after.lstrip('\n')
    return updated


def save(root, session, request_id, document):
    root = handoff_identity.project_root(root)
    prune_publication_intents(root)
    if not isinstance(document, dict):
        raise ValueError('Draft must be a JSON object.')
    document = inbox_document(root, document)
    continuity_items(document.get('continuity', []))
    topic = document.get('topic', '')
    slug_warning = []
    if isinstance(topic, str) and topic and not (len(topic) <= 80 and ledger.TOPIC_RE.fullmatch(topic)):
        slug = ledger.slugify_topic(topic)
        if slug:
            slug_warning = ['Topic %r is not kebab-case; saved as slug %r.' % (topic[:80], slug)]
            topic = slug
            document = dict(document, topic=slug)
    if not isinstance(topic, str) or len(topic) > 80 or not ledger.TOPIC_RE.fullmatch(topic):
        return dict(outcome='invalid', action='Supply a short kebab-case topic (lowercase letters, digits and hyphens, for example fix-login-bug).')
    body = document.get('body', '')
    if not isinstance(body, str):
        return dict(outcome='invalid', action='Supply a Markdown body.')
    problems = ledger._problems('---\nstatus: open\n---\n' + body, {'status':'open'})
    if not body.strip() or problems:
        return dict(outcome='invalid', problems=problems[:8], action='Keep the previous checkpoint and draft. Repair the named document problems and retry.')
    identity = hashlib.sha256((os.path.realpath(root) + '\0' + session + '\0' + request_id).encode()).hexdigest()
    fingerprint = hashlib.sha256(json.dumps(document, sort_keys=True).encode()).hexdigest()
    from contextlib import ExitStack
    with ExitStack() as locks:
        locks.enter_context(ledger.locked(handoff_identity.publisher_lock_path(root)))
        errors = []
        records = ledger._records(root, errors)
        if errors:
            return dict(outcome='blocked', action='Restore read access before saving; a prior checkpoint or retry may be hidden.', errors=errors[:5])
        for record in records:
            if record['fm'].get('checkpoint_id') == identity:
                if record['fm'].get('content_hash') != fingerprint:
                    return dict(outcome='conflict', action='This request ID was already saved with different content. Use a new request ID.')
                if record['record'].has_receipt() and not record['record'].published():
                    return dict(outcome='conflict', action='Published checkpoint changed; reconcile it and use a new request ID.')
                # Reconstruct expected content before repairing an interrupted receipt.
                if record['text'].split('\n---\n', 1)[-1] != body:
                    return dict(outcome='conflict', action='Saved body changed; preserve it and use a new request ID after reconciliation.')
                # Sealing marks the file trusted: its metadata must match what was submitted, not just hash and body.
                if not _front_matter_matches(root, document, record):
                    return dict(outcome='conflict', path=record['path'], action='Saved front matter differs from the submitted checkpoint; it was not sealed. Inspect it, then use a new request ID.')
                write_json(record['path'] + '.published', ledger.seal(identity, snapshot(record['path'])))
                guard = restart_guard(root, session, topic, [r for r in records if r['path'] != record['path']],
                                      pressure=record['fm'].get('reason') == 'context-pressure',
                                      progress=predecessor_progress(root, record['fm'], json.loads(record['fm'].get('continuity', '[]'))))
                if record['record'].paused:
                    guard['pause_reasons'] = ledger._split_csv(record['record'].paused)
                    guard['reasons'] = list(dict.fromkeys(guard['reasons'] + guard['pause_reasons']))
                next_transition = (dict(kind='none', action='Checkpoint is already transferred or closed; continue current work without restarting.')
                                   if not record['record'].open_strict else transition(root, guard))
                return dict(outcome='saved', path=record['path'], checkpoint_id=identity,
                            transition=next_transition, action='Checkpoint publication is confirmed; follow transition.action.')
        predecessor = document.get('predecessor')
        resolved = ledger.resolve(topic, root)
        current = resolved['authoritative']
        unfinished = current if resolved['chain'] and resolved['chain'][-1]['status'] == 'unpublished' else None
        if not unfinished and predecessor:
            chain = ledger.resolve(predecessor, root)['chain']
            if chain and chain[-1]['status'] == 'unpublished':
                unfinished = ledger.resolve(predecessor, root)['authoritative']
        if unfinished:
            # A successor of unpublished work would be untrusted lineage that drops its
            # dependencies and register; the interrupted save must finish first.
            recovery = publication_recovery(root, unfinished)
            return dict(outcome='incomplete', path=unfinished, recovery=recovery,
                        action=recovery['text'] + ' Keep this draft, or save independent work under a distinct topic. '
                               'Do not publish a successor of unpublished work.')
        if current and not predecessor:
            # Continuing your own lineage needs no lookup: same session or terminal.
            fm = next((r['fm'] for r in records if r['path'] == current), {})
            if (fm.get('session_id') == session or (terminal_id() and fm.get('terminal') == terminal_id())
                    or (fm.get('status') == 'resumed' and (owns_transfer(root, current, fm, session)
                                                           # an untraceable own transfer gets holder/recovery guidance below
                                                           or fm.get('resumed_by') == session))):
                predecessor = current
        if current and (not predecessor or os.path.realpath(os.path.expanduser(predecessor)) != os.path.realpath(current)):
            return dict(outcome='conflict', path=current, action='This topic already has a checkpoint. Supply its authoritative path as predecessor to continue that lineage, or use a distinct topic for independent work.')
        if predecessor:
            predecessor = os.path.realpath(os.path.expanduser(predecessor))
            record = next((r for r in records if os.path.realpath(r['path']) == predecessor), None)
            if not record or ledger.resolve(record['topic'], root)['authoritative'] != record['path']:
                return dict(outcome='conflict', action='Predecessor is not authoritative in this project. Refresh lookup and select the intended lineage.')
            locks.enter_context(ledger.locked(predecessor))
            ledger._check_owner(predecessor, session)
            # Re-read under the lock: another session may have resumed it; only the resumer may continue it.
            held = Record.load(predecessor).fm
            if held.get('status') == 'resumed' and not owns_transfer(root, predecessor, held, session):
                holder = resumed_holder(root, predecessor, held)
                ledger_cli = shlex.quote(os.path.join(hooks_dir(), 'handoff_ledger.py'))
                untraceable = not holder.get('terminal') and not holder.get('runner_id')
                recovery = (dict(command='python3 %s recover-transfer %s --previous-session %s --resumed-at %s --fingerprint %s' % (
                    ledger_cli, shlex.quote(predecessor), shlex.quote(held.get('resumed_by', '')),
                    shlex.quote(held.get('resumed', '')), snapshot(predecessor)),
                    requires='Explicit user confirmation that this terminal performed this exact transfer; then add --confirm-holder.')
                    if untraceable else None)
                if recovery:
                    # No terminal/runner is recorded for the holder, so this terminal cannot be told apart from it and save is refused here.
                    why = ('its transfer receipt is missing or unusable' if not holder
                           else 'its transfer receipt is not tied to any terminal or runner')
                    action = ('Handoff resumed by %s at %s, but %s, so the holder cannot be verified and save is refused. '
                              'If the user confirms this terminal performed that transfer, run the `recovery` command (recover-transfer ... --confirm-holder) in this terminal, then retry the save; '
                              'otherwise do not supersede it.' % (held.get('resumed_by'), held.get('resumed'), why))
                else:
                    action = ('Handoff resumed by %s in terminal %s at %s; do not supersede a different or untraceable holder. Continue in the holding terminal with: python3 %s save --input <original-draft.json>.'
                              % (held.get('resumed_by'), holder.get('terminal') or 'unknown', held.get('resumed'), ledger_cli))
                return dict(outcome='conflict', path=predecessor,
                            holder=dict(resumed_by=held.get('resumed_by'), terminal=holder.get('terminal'),
                                        runner_id=holder.get('runner_id'), resumed=held.get('resumed')),
                            recovery=recovery, action=action)
            # Only a command this machine saved is inherited; an untrusted predecessor's would be laundered into a trusted successor.
            if not document.get('verify') and record['record'].trusted():
                document = dict(document, verify=record['record'].get('verify', ''))
        inherited = Record.load_trusted(predecessor) if predecessor else None
        prior = inherited.fm if inherited else {}
        lineage_warning = ([] if not predecessor or prior else
                           ['Predecessor is untrusted (for example abandoned before publication); no skills, references, verify or continuity were inherited. Supply any still-required ones explicitly.'])
        document = continuation_document(document, prior)
        if not document.get('verify'):
            # User-level config only: a repo's .context-watch.json must not choose a command this machine will trust.
            document = dict(document, verify=project_setting(root, 'verify', str, user_only=True) or '')
        note = ledger.read_session_note(root, session=session)
        now = datetime.now()
        fields = dict(topic=topic, created=now.isoformat(timespec='microseconds'), status='open',
                      description=str(document.get('description', ''))[:240],
                      project=root, git=ledger._git_position(root), session_id=session,
                      checkpoint_id=identity, content_hash=fingerprint,
                      trigger_id=note.get('trigger_id', '') if note.get('fired') else '',
                      runner_id=os.environ.get('HANDOFF_RUN_ID', ''),
                      terminal=terminal_id(),
                      reason='context-pressure' if note.get('fired') else 'user-parked')
        if document.get('reason') == 'user-parked':
            fields['reason'] = 'user-parked'
        fields['workspace_changes_hash'] = workspace_changes_hash(root)
        workspace = os.path.realpath(note.get('cwd') or root)
        fields['workspace'] = workspace
        fields['workspace_git'] = ledger._git_position(workspace)
        fields['project_hooks'] = json.dumps(project_hook_snapshot(workspace), sort_keys=True)

        if predecessor:
            fields['predecessor'] = predecessor
        progress = save_progress(root, fields, document['continuity'], prior)
        if not progress['predecessor_known']:
            # No same-topic pressure predecessor: measure against the checkpoint this session resumed.
            # resumed_by/resumed are outside the MAC, so only a transfer with a valid receipt counts.
            resumed = sorted((r for r in records if r['record'].trusted() and r['fm'].get('resumed_by') == session
                              and r['fm'].get('resumed') and resumed_holder(root, r['path'], r['fm'])
                              and owns_transfer(root, r['path'], r['fm'], session)), key=lambda r: r['fm']['resumed'])
            if resumed:
                progress = save_progress(root, fields, document['continuity'], resumed[-1]['fm'], baseline=True)
        guard = restart_guard(root, session, topic, records, pressure=fields['reason'] == 'context-pressure', progress=progress)
        next_transition = transition(root, guard)
        fields['restart_intent'] = next_transition['kind']
        if next_transition['kind'] == 'paused':
            fields['automatic_paused'] = ', '.join(guard['pause_reasons'])
        if guard['reasons']:
            fields['thrash_warning'] = json.dumps(guard, sort_keys=True)
        for key in ('skills', 'references', 'optional_references', 'verify', 'verify_baseline'):
            value = document.get(key, '')
            if isinstance(value, list):
                value = ', '.join(dict.fromkeys(value))
            fields[key] = value
        if document['continuity']:
            fields['continuity'] = json.dumps(document['continuity'], sort_keys=True)
        if predecessor:
            fields['predecessor'] = predecessor
        if any(not isinstance(v, str) or '\n' in v or '\r' in v for v in fields.values()):
            return dict(outcome='invalid', action='Metadata values must be single-line strings or lists of strings.')
        text = '---\n' + ''.join('%s: %s\n' % pair for pair in fields.items()) + '---\n' + body
        filename = now.strftime('%Y%m%d-%H%M%S-') + topic + '-' + identity[:12] + '.md'
        destinations = list(dict.fromkeys([ledger.save_path(root), ledger._fallback_dir(root), os.path.join(root, '.handoffs')]))
        failures = []
        for directory in destinations:
            path = os.path.join(directory, filename)
            try:
                # Durable intent precedes the document. Recovery does not depend
                # on a remembered request id or a temporary author draft.
                recovery_id = hashlib.sha256((os.path.realpath(root) + '\0' + os.path.realpath(path)).encode()).hexdigest()
                intent_warning = []
                digest = hashlib.sha256(text.encode()).hexdigest()
                try:
                    write_json(recovery_path(root, path), dict(path=os.path.realpath(path), root=root,
                               created_at=time.time(), content_hash=digest,
                               seal=ledger.seal(recovery_id, digest)))
                except OSError:
                    intent_warning.append('Publication recovery intent is unavailable; retain the original request ID and draft for legacy retry.')
                publish(path, text)
                write_json(path + '.published', ledger.seal(identity, ledger.content_fingerprint(text)))
                # The successor's predecessor field is the committed lineage record.
                # Old-file cleanup is optional and never makes a saved successor vanish.
                warnings = intent_warning + lineage_warning + slug_warning
                if len(body.split()) > ledger.RECOMMENDED_WORDS or len(text.encode()) > RECOMMENDED_DOCUMENT_BYTES:
                    warnings.append('Checkpoint exceeds the recommended 1500 words / 24000 bytes; keep essential context and consider optional background references.')
                if re.search(r'\b(?:re[- ]?read|read\s+the\s+(?:whole|entire)|load\s+(?:the\s+)?skill)\b', body, re.I):
                    warnings.append('Potential expensive re-entry read: state when it is needed, carry the minimal commands, and defer background docs/skills until the action needs them.')
                for spec in ledger._split_csv(fields.get('references', '')):
                    excerpt = re.search(r'#L(\d+)-L(\d+)$', spec)
                    if excerpt and int(excerpt[2]) - int(excerpt[1]) + 1 > 80:
                        warnings.append('Required excerpt exceeds the recommended 80 lines: %s; keep necessary content and defer background to optional_references.' % spec)
                return dict(outcome='saved', path=path, checkpoint_id=identity, warnings=warnings, transition=next_transition,
                            action='Checkpoint is published and discoverable; follow transition.action, then stop.')
            except OSError as exc:
                if os.path.exists(path):
                    return dict(outcome='blocked', path=path, action='Publication was interrupted after writing the document. Keep the draft and retry the same request ID to finish its receipt; do not clear.')
                try:
                    os.unlink(recovery_path(root, path))
                except OSError:
                    pass
                failures.append(dict(path=directory, error=type(exc).__name__))
        return dict(outcome='blocked', errors=failures, action='Keep this session and the draft. Both supported locations failed; report access/storage failure. Do not clear or claim the handoff was saved.')


PACKAGE_BYTES = 32_000


def read_text(path):
    with open(path, 'rb') as f:
        return f.read().decode('utf-8')


def snapshot(path):
    return ledger.content_fingerprint(read_text(path))


def recover_transfer(path, root, session, previous_session, resumed_at, fingerprint, confirm=False):
    root, path = handoff_identity.project_root(root), os.path.realpath(path)
    if not confirm:
        return dict(outcome='needs-confirmation', path=path,
                    action='Ask the user to confirm that this terminal actually performed the legacy transfer, using its transcript and exact holder/time. Only then rerun with --confirm-holder. The saver terminal is not evidence.')
    if not terminal_id():
        raise ValueError('Legacy recovery requires the current stable terminal identity.')
    with ledger.locked(handoff_identity.publisher_lock_path(root)), ledger.locked(path):
        fm = Record.load(path).fm
        ledger._check_owner(path, session)
        if (ledger.resolve(path, root)['authoritative'] != path or fm.get('status') != 'resumed'
                or fm.get('resumed_by') != previous_session or fm.get('resumed') != resumed_at
                or snapshot(path) != fingerprint):
            raise ValueError('Legacy holder/time changed or checkpoint is no longer authoritative; inspect history.')
        holder = resumed_holder(root, path, fm)
        if (holder.get('provenance') == 'user-attested-legacy-holder'
                and holder.get('terminal') == terminal_id()):
            return dict(outcome='recovered', path=path, action='This exact legacy transfer was already recovered for this terminal. Retry the preserved continuation draft.')
        if holder.get('terminal') or holder.get('runner_id'):
            raise ValueError('This transfer already has holder provenance; continue in its holding terminal.')
        receipt = dict(path=path, fingerprint=snapshot(path), acknowledged_at=resumed_at,
                       verified=True, terminal=terminal_id(), runner_id='', dependencies={},
                       provenance='user-attested-legacy-holder', recovered_by=session,
                       recovered_at=datetime.now().isoformat(timespec='microseconds'))
        write_receipt(root, previous_session, path, receipt)
        return dict(outcome='recovered', path=path,
                    action='Recorded the user-attested legacy holder without changing lineage. Retry the preserved continuation draft with save; do not execute historical next steps again.')


INTENT_TTL = 30 * 86400


def prune_publication_intents(root):
    from pathlib import Path
    with ledger.locked(handoff_identity.publisher_lock_path(root)):
        directory = Path(os.path.expanduser('~/.local/state/session-handoff/publications'))
        for item in directory.glob('*.json'):
            try:
                intent = load_state_json(str(item))
                if (os.path.realpath(intent.get('root', '')) == os.path.realpath(root)
                        and time.time() - float(intent.get('created_at', 0)) > INTENT_TTL):
                    item.unlink()
            except (OSError, ValueError, TypeError):
                pass

def recovery_path(root, path):
    key = hashlib.sha256((os.path.realpath(handoff_identity.project_root(root)) + '\0' + os.path.realpath(path)).encode()).hexdigest()
    return os.path.expanduser('~/.local/state/session-handoff/publications/' + key + '.json')


def repair(path, root, session):
    root, path = handoff_identity.project_root(root), os.path.realpath(path)
    if not os.path.exists(path):
        return dict(outcome='none', path=path, action='No checkpoint exists at this path; nothing to repair. Use lookup to find the intended checkpoint. If a save was interrupted before the document was written, retry the original save request ID and draft.')
    with ledger.locked(handoff_identity.publisher_lock_path(root)), ledger.locked(path):
        try:
            recovery = load_state_json(recovery_path(root, path))
        except FileNotFoundError:
            recovery = publication_recovery(root, path)
            return dict(outcome='needs-original-draft', path=path, recovery=recovery, action=recovery['text'])
        key = hashlib.sha256((os.path.realpath(root) + '\0' + path).encode()).hexdigest()
        text = read_text(path)
        if not ledger.valid_seal(recovery['seal'], key, hashlib.sha256(text.encode()).hexdigest()):
            raise ValueError('Publication recovery intent is not authenticated; keep the checkpoint and original draft.')
        checkpoint = Record(path, text)
        fm = checkpoint.fm
        if (recovery['path'] != path or os.path.realpath(recovery['root']) != os.path.realpath(root)
                or not checkpoint.open_strict):
            raise ValueError('Checkpoint changed since publication intent; preserve it and reconcile before repair.')
        if fm.get('terminal'):
            owned = terminal_id() == fm['terminal']
        else:
            owned = session == fm.get('session_id')
        if not owned:
            raise ValueError('Repair belongs to another terminal/session; run it in the publishing terminal.')
        ledger._check_owner(path, session)
        if ledger.resolve(path, root)['authoritative'] != path:
            raise ValueError('Checkpoint is no longer authoritative; inspect history before repair.')
        write_json(path + '.published', ledger.seal(fm['checkpoint_id'], ledger.content_fingerprint(text)))
        return dict(outcome='saved', path=path, checkpoint_id=fm['checkpoint_id'],
                    action='Publication repaired. Run lookup and follow the current continuation policy; do not restart from stale instructions.')


def receipt_path(root, session, path=None):
    if path is None:
        return handoff_identity.prepared_receipt_path(root, session)
    key = hashlib.sha256((os.path.realpath(handoff_identity.project_root(root)) + '\0' + session + '\0'
                          + os.path.realpath(path)).encode()).hexdigest()
    return os.path.expanduser('~/.local/state/session-handoff/transfers/' + key + '.json')


def transfer_seal_fields(root, session, path, receipt):
    identity = hashlib.sha256((os.path.realpath(handoff_identity.project_root(root)) + '\0' + session
                               + '\0' + os.path.realpath(path)).encode()).hexdigest()
    payload = {k: v for k, v in receipt.items() if k != 'seal'}
    fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return identity, fingerprint


def valid_transfer_receipt(root, session, path, receipt):
    if not isinstance(receipt, dict):
        return False
    try:
        identity, fingerprint = transfer_seal_fields(root, session, path, receipt)
        return ledger.valid_seal(receipt.get('seal'), identity, fingerprint)
    except (RecursionError, ValueError, TypeError):
        return False


def write_receipt(root, session, path, receipt):
    receipt.update(session_id=session, project=os.path.realpath(handoff_identity.project_root(root)))
    identity, fingerprint = transfer_seal_fields(root, session, path, receipt)
    receipt['seal'] = ledger.seal(identity, fingerprint)
    write_json(receipt_path(root, session, path), receipt)


def read_receipt(root, session, path):
    try:
        return load_state_json(receipt_path(root, session, path))
    except FileNotFoundError:
        # Existing same-session receipts remain usable; never invent provenance.
        return load_state_json(receipt_path(root, session))


def write_json(path, value):
    directory = os.path.dirname(path)
    os.makedirs(directory, mode=0o700, exist_ok=True)
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
        return path, read_text(path)
    first, last = int(match[2]), int(match[3])
    if last < first:
        raise ValueError('Invalid reference line range: ' + spec)
    parts = []
    with open(path, 'rb') as f:
        for count in range(1, last + 1):
            line = f.readline()
            if not line:
                raise ValueError('Reference line range extends past EOF: ' + spec)
            if count >= first:
                parts.append(line)
    return path + '#L%d-L%d' % (first, last), b''.join(parts).decode('utf-8')


def _inside(real_root, spec, root):
    name = re.sub(r'#L[0-9]+-L[0-9]+$', '', spec)
    target = os.path.realpath(os.path.join(root, os.path.expanduser(name)))
    return target == real_root or target.startswith(real_root + os.sep)


def skill_roots():
    """Installed skill roots in precedence order; the first root providing a name wins."""
    roots = [r for r in os.environ.get('HANDOFF_SKILL_ROOTS', '').split(os.pathsep) if r]
    codex = os.environ.get('CODEX_HOME') or '~/.codex'
    return [os.path.expanduser(r) for r in roots + ['~/.agents/skills', '~/.claude/skills', os.path.join(codex, 'skills')]]


def installed_catalog():
    skills = {}
    for root in skill_roots():
        try:
            names = sorted(os.listdir(root))
        except OSError:
            continue
        for name in names:
            path = os.path.join(root, name, 'SKILL.md')
            if name not in skills and os.path.isfile(path):
                skills[name] = path
    return dict(skills=skills, loaded=[])


def prepare(path, root, session, execute=False, catalog=None, budget=PACKAGE_BYTES, reuse_receipt=None):
    import time
    root = handoff_identity.project_root(root)
    errors = []
    ledger._records(root, errors)
    if errors:
        return dict(outcome='blocked', errors=errors[:5], action='Restore read access before preparing; authoritative state may be hidden.')
    requested = path
    resolved = ledger.resolve(path, root)
    path = resolved['authoritative']
    if not path:
        return dict(outcome='none', action='No saved handoff matches the requested target %r. Use lookup only to select different saved work explicitly; native conversation recovery is independent.' % str(requested)[:200])
    if resolved['chain'][-1]['status'] == 'unpublished':
        recovery = publication_recovery(root, path)
        return dict(outcome='incomplete', path=path, recovery=recovery, action=recovery['text'])
    if resolved['chain'][-1]['status'] != 'open':
        return dict(outcome='closed', action='This topic is already transferred or closed. Use history for inspection; do not adopt it.')
    supplied = catalog is not None
    catalog = catalog if supplied else installed_catalog()
    if not isinstance(catalog, dict) or not isinstance(catalog.get('skills', {}), dict) or not isinstance(catalog.get('loaded', []), list):
        raise ValueError('Catalog must contain a skills mapping and a loaded-path list.')
    if not all(isinstance(x, str) for x in list(catalog.get('skills', {}).values()) + catalog.get('loaded', [])):
        raise ValueError('Installed skill paths must be strings.')
    try:
        fingerprint = snapshot(path)
        text = read_text(path)
        checkpoint = Record(path, text)
        fm = checkpoint.fm
        problems = ledger._problems(text, fm)
        if problems or not fm:
            raise ValueError('Repair the checkpoint before preparation: ' + '; '.join(problems))
        ledger._check_owner(path, session)
        body = text.split('\n---\n', 1)[-1]
        refs, skills, all_skills, seen, dependencies = [], [], [], set(), {}
        required = ledger._split_csv(fm.get('references', ''))
        own = checkpoint.trusted()
        real_root = os.path.realpath(root)
        withheld = []
        for spec in required:
            if not own and not _inside(real_root, spec, root):
                withheld.append(spec)  # untrusted checkpoints cannot pull files from outside the project
                continue
            canonical, content = reference(root, spec)
            if canonical in seen:
                continue
            seen.add(canonical)
            dependencies[canonical] = hashlib.sha256(content.encode()).hexdigest()
            refs.append(dict(path=canonical, text=content))
        # A server-stored receipt plus possession of its opaque token binds reuse
        # to a previous delivery, this session, and unchanged content.
        reference_fingerprints = dict(dependencies)
        delivery_file = handoff_identity.delivery_receipt_path(root, session)
        reused_bytes = 0
        if reuse_receipt:
            try:
                delivered = load_state_json(delivery_file)
                if (delivered['token'] == reuse_receipt and delivered['path'] == path
                        and delivered['fingerprint'] == fingerprint
                        and delivered['references'] == reference_fingerprints
                        and 0 <= time.time() - delivered['created'] < 7200):
                    reused_bytes = len(body.encode()) + sum(len(r['text'].encode()) for r in refs)
            except (OSError, ValueError, KeyError, TypeError):
                pass  # Missing, expired or changed delivery safely reloads in full.
        loaded = {os.path.realpath(p) for p in catalog.get('loaded', [])}
        # Include this workflow's loaded instructions in the context-size estimate.
        policy_path = os.path.realpath(os.path.join(os.path.dirname(__file__), '..', 'SKILL.md'))
        policy_bytes = len(read_text(policy_path).encode())
        if execute:
            policy_bytes += len(read_text(os.path.join(os.path.dirname(policy_path), 'continuation.md')).encode())
        loaded.add(policy_path)
        omitted_skills = 0
        if execute:
            names = ledger._split_csv(fm.get('skills', ''))
            for name in names:
                source = catalog.get('skills', {}).get(name)
                if not source:
                    raise ValueError('Required skill unavailable in the %s installed catalog: %s' % ('supplied' if supplied else 'scanned (' + os.pathsep.join(skill_roots()) + ')', name))
                canonical = os.path.realpath(source)
                if canonical in seen:
                    omitted_skills += 1
                    continue
                seen.add(canonical)
                content = read_text(canonical)
                dependencies[canonical] = hashlib.sha256(content.encode()).hexdigest()
                all_skills.append(dict(name=name, text=content))
                if canonical in loaded:
                    omitted_skills += 1
                    continue
                skills.append(dict(name=name, path=canonical, text=content))
        result = dict(outcome='prepared' if execute else 'retrieved', path=path, body=body,
                      references=refs, skills=skills,
                      action=('Run verify if requested, reconcile reported workspace changes, then acknowledge before executing next steps.' if execute else
                              'Report the objective and next step. Retrieval is complete; do not claim, verify, load execution skills or execute next steps without authorization.'),
                      verify_required=bool(fm.get('verify')) if execute else False,
                      trusted=own,
                      workspace=ledger.claim_report(path) if execute else [],
                      optional_references=ledger._split_csv(fm.get('optional_references', ''))[:8])
        if fm.get('continuity'):
            result['continuity'] = continuity_items(json.loads(fm['continuity']))
        if fm.get('predecessor'):
            result['predecessor'] = fm['predecessor']
        if withheld:
            result['withheld_references'] = withheld
            result['action'] += ' References outside the project root were not loaded (untrusted checkpoint): ' + ', '.join(withheld[:8])
        import uuid
        delivery_token = uuid.uuid4().hex
        result['delivery_receipt'] = delivery_token
        if reused_bytes:
            del result['body']
            result['references'] = []
            result['reused_from'] = reuse_receipt
        # JSON overhead counts too. Loaded skills still consume context even though not re-emitted.
        existing_bytes = sum(len(read_text(p).encode()) for p in (loaded & seen) - {policy_path})
        result['metrics'] = dict(body_bytes=len(body.encode()), reference_bytes=sum(len(r['text'].encode()) for r in refs),
                                 skill_bytes=sum(len(s['text'].encode()) for s in skills),
                                 already_loaded_bytes=existing_bytes, reused_bytes=reused_bytes, deduplicated_skills=omitted_skills,
                                 token_estimate_method='UTF-8 bytes / 4; estimate, not tokenizer measurement', budget_bytes=budget)
        result['metrics']['workflow_bytes'] = policy_bytes
        size = len(json.dumps(result, ensure_ascii=False).encode()) + existing_bytes + reused_bytes + policy_bytes + 128
        result['metrics'].update(package_bytes=size, estimated_tokens=(size + 3) // 4)
        if size > budget:
            result['warnings'] = ['Complete resume package exceeds the recommended %d bytes; consider a smaller core or narrower references.' % budget]
        if execute:
            with ledger.locked(path):
                ledger._check_owner(path, session)
                if snapshot(path) != fingerprint:
                    raise ValueError('Checkpoint changed during preparation; retry prepare.')
                ledger._set_fields(path, [('claimed', datetime.now().strftime(ledger.STAMP)), ('claim_owner', session)], ('claimed:', 'claim_owner:'))
                try:
                    write_receipt(root, session, path, dict(path=path, fingerprint=fingerprint, dependencies=dependencies,
                               prepared_at=time.time(), verified=not bool(fm.get('verify')), metrics=result['metrics'],
                               terminal=terminal_id(), runner_id=os.environ.get('HANDOFF_RUN_ID', ''),
                               package=dict(body=body, references=refs, skills=all_skills)))
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
        return dict(outcome='needs-context', action=str(exc)[:700] + ' Preserve the checkpoint. Resolve the named dependency; do not acknowledge or search unrelated history.')


def prepared_receipt(path, root, session):
    import time
    receipt = read_receipt(root, session, path)
    if not valid_transfer_receipt(root, session, path, receipt):
        raise ValueError('Preparation receipt is unsigned or invalid; run prepare --execute again.')
    if receipt.get('seal') and receipt.get('terminal') and receipt['terminal'] != terminal_id():
        raise ValueError('Preparation belongs to a different terminal; do not acknowledge it.')
    if os.path.realpath(receipt['path']) != os.path.realpath(path) or time.time() - receipt['prepared_at'] > 7200:
        raise ValueError('Preparation absent or expired; prepare again.')
    resolved = ledger.resolve(path, root)
    if not resolved['chain'] or resolved['chain'][-1]['status'] != 'open' or os.path.realpath(resolved['authoritative']) != os.path.realpath(path):
        raise ValueError('Checkpoint is no longer authoritative open work; refresh lookup.')
    ledger._check_owner(path, session)
    checkpoint = Record.load(path)
    if checkpoint.claim_owner != session or not ledger._claimed_recently(checkpoint.fm, datetime.now()):
        raise ValueError('Preparation no longer holds a live claim; prepare again.')
    if snapshot(path) != receipt['fingerprint']:
        raise ValueError('Checkpoint changed after preparation; prepare again.')
    for spec, expected in receipt['dependencies'].items():
        _, content = reference(root, spec)
        if hashlib.sha256(content.encode()).hexdigest() != expected:
            raise ValueError('Required dependency changed; prepare again: ' + spec)
    return receipt


def acknowledge(path, root, session):
    root = handoff_identity.project_root(root)
    if not os.path.exists(path):
        raise FileNotFoundError(errno.ENOENT, 'No such file or directory', path)  # before locked(): no stray .lock
    with ledger.locked(path):
        fm = Record.load(path).fm
        ledger._check_owner(path, session)
        if fm.get('status') == 'resumed' and fm.get('resumed_by') == session:
            if not owns_transfer(root, path, fm, session):
                raise ValueError('Transfer belongs to a different terminal; do not acknowledge it.')
            return dict(outcome='resumed', path=path, action='This session already acknowledged the transfer.')
        receipt = prepared_receipt(path, root, session)
        if not receipt['verified']:
            return dict(outcome='blocked', action='Verification has not passed. Run verify; keep the claim recoverable until preparation succeeds.')
        resumed_at = datetime.now().isoformat(timespec='microseconds')
        receipt.update(acknowledged_at=resumed_at, terminal=terminal_id(), runner_id=os.environ.get('HANDOFF_RUN_ID', ''))
        write_receipt(root, session, path, receipt)
        ledger._set_fields(path, [('status', 'resumed'), ('resumed', resumed_at), ('resumed_by', session),
                                  ('transfer_receipt', 'signed')],
                           ('status:', 'resumed:', 'resumed_by:', 'transfer_receipt:', 'claimed:', 'claim_owner:'))
        import time
        note = ledger.read_session_note(root, session=session)
        note.update(session_id=session, ts=time.time(), resumed_at=time.time(), resumed_topic=fm.get('topic'))
        try:
            write_json(handoff_identity.session_note_path(root, session), note)
        except OSError:
            pass  # Transfer succeeded; optional diagnostics must not turn it into a failed resume.
    return dict(outcome='resumed', path=path, action='Transfer acknowledged. Continue the authorized next step.')


def needs_confirmation(path, command):
    return dict(outcome='needs-confirmation', stage='verify', path=path, verify=command[:500],
                action='This checkpoint was not saved by this machine, so its verify command was not run. Show the user the command and ask whether to run it. If they approve, rerun with --confirm-verify. To read it without running anything: prepare <path> (retrieval).')


def verify(path, root, session, timeout=60, confirm=False):
    import subprocess
    import signal
    root = handoff_identity.project_root(root)
    receipt = prepared_receipt(path, root, session)
    checkpoint = Record.load(path)
    command, baseline = checkpoint.verify, ledger._split_csv(checkpoint.get('verify_baseline', ''))
    if not command:
        return dict(outcome='verified', exit_code=0, action='No recorded verification command; no speculative suite is required.')
    if not confirm and not checkpoint.trusted():
        return needs_confirmation(path, command)
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
    failures = failed_tests(log) if code and not timed_out and baseline else []
    # A baseline match is trusted only when pytest finished its summary and reported no ERROR lines.
    known = bool(failures) and set(failures) == set(baseline) and clean_pytest_summary(log)
    # Recheck content and ownership after the command; never bless a changed checkpoint.
    receipt = prepared_receipt(path, root, session)
    receipt['verified'] = (code == 0 or known) and not timed_out
    write_receipt(root, session, path, receipt)
    excerpt = ''
    if code or timed_out:
        with open(log, 'rb') as f:
            f.seek(max(0, os.path.getsize(log) - 2000))
            excerpt = f.read(2000).decode('utf-8', errors='replace')
    result = dict(outcome=outcome, exit_code=code, log=log, excerpt=excerpt,
                  action='Acknowledge the transfer.' if receipt['verified'] else 'Inspect the bounded failure excerpt or log; fix and retry, or release the claim. The handoff remains recoverable.')
    if known:
        result.update(outcome='verified', excerpt='', baseline_failures=failures,
                      action='These failures were already recorded at save time (verify_baseline); nothing regressed. Acknowledge the transfer.')
    elif failures:
        result.update(new_failures=[f for f in failures if f not in baseline],
                      fixed=[b for b in baseline if b not in failures])
    return result


def failed_tests(log, limit=200):
    """Pytest node ids from 'FAILED <id> - <reason>' summary lines, in order, deduplicated."""
    found = {}
    with open(log, 'rb') as f:
        for raw in f:
            line = raw.decode('utf-8', errors='replace').rstrip('\r\n')
            if line.startswith('FAILED ') and len(found) < limit:
                found.setdefault(line[7:].split(' - ', 1)[0].strip(), None)
    return [f for f in found if f]


def clean_pytest_summary(log):
    """True when the log ends a pytest run ('... in 1.2s') and has no 'ERROR <id>' summary lines."""
    import re
    done = errors = False
    with open(log, 'rb') as f:
        for raw in f:
            line = raw.decode('utf-8', errors='replace').rstrip('\r\n')
            errors = errors or line.startswith('ERROR ')
            done = done or bool(re.search(r'\bin \d+(\.\d+)?s\b', line) and re.search(r'\b(failed|passed|error)', line))
    return done and not errors


def section(body, title):
    import re
    match = re.search(r'^## %s(?:[ \t][^\n]*)?$\n(.*?)(?=^## |\Z)' % re.escape(title), body, re.M | re.S)
    return match[1].strip() if match else ''


REPLAY_TTL = 7200
NEXT_STEP_BYTES = 3000
NEXT_STEP_MARKER = ' [... continues in the body under ## Next steps]'


def next_step_lead(steps, limit=NEXT_STEP_BYTES):
    """Next steps, cut so the JSON-encoded copy fits limit bytes, marked when cut."""
    def size(text):
        return len(json.dumps(text, ensure_ascii=False).encode())
    if size(steps) <= limit:
        return steps
    low, high = 0, len(steps)
    while low < high:  # longest prefix whose encoded form, plus the marker, fits
        middle = (low + high + 1) // 2
        if size(steps[:middle] + NEXT_STEP_MARKER) <= limit:
            low = middle
        else:
            high = middle - 1
    return steps[:low] + NEXT_STEP_MARKER


def replay_fresh(receipt):
    import time
    return 0 <= time.time() - float(receipt.get('prepared_at', 0)) <= REPLAY_TTL


def prune_transfer_payloads(root):
    from pathlib import Path
    directory = Path(os.path.expanduser('~/.local/state/session-handoff/transfers'))
    for item in directory.glob('*.json'):
        try:
            receipt = load_state_json(str(item))
            if (receipt.get('project') != os.path.realpath(root) or replay_fresh(receipt)
                    or not (receipt.get('delivery') or receipt.get('package'))):
                continue
            path, session = receipt['path'], receipt['session_id']
            if not valid_transfer_receipt(root, session, path, receipt):
                continue  # Untrusted metadata cannot choose a filesystem lock target.
            with ledger.locked(path):
                receipt = read_receipt(root, session, path)
                if valid_transfer_receipt(root, session, path, receipt) and not replay_fresh(receipt):
                    receipt.pop('delivery', None)
                    receipt.pop('package', None)
                    receipt['delivery_state'] = 'expired'
                    write_receipt(root, session, path, receipt)
        except (OSError, ValueError, KeyError, TypeError):
            pass  # Cache pruning never blocks current work.


def pending_delivery(root, path, fm, session):
    # Most records were flushed long ago: inspect small metadata before hashing
    # checkpoint content or resolving its lineage in the bounded hook worker.
    if fm.get('status') != 'resumed':
        return False
    try:
        receipt = read_receipt(root, fm.get('resumed_by', ''), path)
        if receipt.get('delivery_state') != 'pending' or not replay_fresh(receipt):
            return False
    except (OSError, ValueError, KeyError, TypeError):
        return False
    return (owns_transfer(root, path, fm, session)
            and ledger.resolve(path, root)['authoritative'] == path)


def delivered(root, path, session):
    # A successful flush is transport evidence only, not proof the host consumed
    # the response. Explicit holder replay remains available afterward.
    with ledger.locked(path):
        fm = Record.load(path).fm
        if owns_transfer(root, path, fm, session):
            receipt = resumed_holder(root, path, fm)
            if receipt.get('delivery'):
                receipt['delivery_state'] = 'flushed'
                write_receipt(root, fm['resumed_by'], path, receipt)


def replay_transfer(root, session, target):
    resolved = ledger.resolve(target, root)
    path = resolved.get('authoritative')
    if not path:
        return None
    with ledger.locked(path):
        if ledger.resolve(path, root)['authoritative'] != path:
            return None  # A successor may have published while waiting for this lock.
        fm = Record.load(path).fm
        if fm.get('status') != 'resumed' or not owns_transfer(root, path, fm, session):
            return None
        ledger._check_owner(path, session)
        receipt = resumed_holder(root, path, fm)
        package = receipt.get('delivery')
        if not package or not replay_fresh(receipt):
            return None
        for spec, expected in receipt['dependencies'].items():
            try:
                _, content = reference(root, spec)
            except (OSError, ValueError) as exc:
                return dict(outcome='needs-context', path=path,
                            action='Replay dependency unavailable: ' + spec + ' (' + type(exc).__name__ + '); restore it or reconcile from history.')
            if hashlib.sha256(content.encode()).hexdigest() != expected:
                return dict(outcome='needs-context', path=path,
                            action='A replay dependency changed: ' + spec + '; inspect history and reconcile before continuing.')
        # Refresh workspace findings; replay never runs verification a second time.
        package['workspace'] = ledger.claim_report(path)
        return package


def resume(root, session, target=None, budget=PACKAGE_BYTES, confirm_verify=False,
           catalog=None, reuse_receipt=None, legacy_next_step=False):
    """Select, prepare --execute, verify and acknowledge in one call; stop at the first failing stage."""
    root = handoff_identity.project_root(root)
    prune_transfer_payloads(root)
    prune_publication_intents(root)
    errors = []
    records = ledger._records(root, errors)
    if errors:
        return dict(outcome='blocked', errors=errors[:5],
                    action='Restore read access before resume or replay; newer authoritative work may be hidden.')
    if target is None:
        pending = []
        for record in records:
            if pending_delivery(root, record['path'], record['fm'], session):
                pending.append(record['path'])
        current_open = [r['path'] for r in records if r['record'].is_open
                        and ((terminal_id() and r['fm'].get('terminal') == terminal_id())
                             or r['fm'].get('session_id') == session)]
        if pending and current_open:
            return dict(outcome='choose', candidates=[dict(path=p) for p in pending + current_open],
                        action='Select pending delivery or current open work explicitly, or none.')
        if len(pending) == 1:
            target = pending[0]
        elif pending:
            return dict(outcome='choose', candidates=[dict(path=p) for p in pending],
                        action='Select a pending delivery path to replay, or none.')
    if target is None:
        found = lookup(root, session=session)
        if found['outcome'] != 'available':
            return found
        target = found.get('selected')
        if not target:
            return dict(outcome='choose', total=found['total'], action='Rerun resume with one of these paths, or none.',
                        candidates=[dict(path=h['path'], topic=h['topic'], description=h['description'], mine=h['mine'])
                                    for h in found['items']])
    elif not ledger.resolve(target, root)['authoritative']:
        return dict(outcome='none', action='No handoff matches %r. Use lookup to inspect and explicitly select other saved work; native conversation recovery is independent.' % target[:200])
    replay = replay_transfer(root, session, target)
    if replay:
        return replay
    if not confirm_verify:
        # Untrusted verify commands are refused before any claim, so nothing is left to release.
        try:
            resolved = ledger.resolve(target, root)
            if resolved['chain'] and resolved['chain'][-1]['status'] == 'open':
                checkpoint = Record.load(resolved['authoritative'])
                command = checkpoint.verify
                if command and not checkpoint.trusted():
                    return needs_confirmation(resolved['authoritative'], command)
        except OSError:
            pass  # prepare reports unreadable state
    def stage(name, run, success):
        try:
            result = run()
        except (OSError, ledger.ConflictError, ValueError, TypeError, KeyError) as exc:
            result = dict(outcome='blocked', action=str(exc)[:500] + '; the handoff stays recoverable. Resolve this, then rerun resume.')
        return None if result['outcome'] == success else dict(result, stage=name), result
    failed, prepared = stage('prepare', lambda: prepare(target, root, session, execute=True, budget=budget,
                                                       catalog=catalog, reuse_receipt=reuse_receipt), 'prepared')
    if failed:
        return failed
    path = prepared['path']
    verification = dict(outcome='not-required')
    if prepared['verify_required']:
        failed, checked = stage('verify', lambda: verify(path, root, session, confirm=confirm_verify), 'verified')
        if failed:
            return failed
        verification = {k: checked[k] for k in ('outcome', 'baseline_failures') if k in checked}
    body = prepared.get('body', '')
    result = dict(outcome='resumed', path=path, references=prepared['references'],
                  skills=[dict(name=s['name'], text=s['text']) for s in prepared['skills']],
                  next_step_section='Next steps', verification=verification,
                  workspace=prepared['workspace'], optional_references=prepared['optional_references'],
                  metrics=prepared['metrics'], action='Continue with ## Next steps in the delivered body.')
    for key in ('continuity', 'predecessor'):
        if key in prepared:
            result[key] = prepared[key]
    if 'body' in prepared:
        result['body'] = body
    else:
        result['reused_from'] = prepared['reused_from']
        result['action'] = 'Continue with ## Next steps in the body still in this session context.'
    full = body or read_text(path).split('\n---\n', 1)[-1]
    steps = section(full, 'Next steps')
    # Always lead with a bounded copy right after action, so a clipped read keeps the next step
    result['next_step'] = steps if legacy_next_step else next_step_lead(steps)
    if 'withheld_references' in prepared:
        result['withheld_references'] = prepared['withheld_references']
    if 'warnings' in prepared:
        result['warnings'] = prepared['warnings']
    output_bytes = len(json.dumps(result, ensure_ascii=False).encode())
    if output_bytes > budget:
        result.setdefault('warnings', []).append(
            'Resume output exceeds the recommended %d bytes; all required content is included.' % budget)
    # Persist the full package before acknowledging: stdout can fail after the
    # transfer. A retry by its holder replays without repeating verification.
    with ledger.locked(path):
        receipt = prepared_receipt(path, root, session)
        receipt['delivery_state'] = 'pending'
        if 'package' not in receipt:
            return dict(outcome='needs-context', stage='prepare', path=path,
                        action='Legacy preparation has no complete replay package; run prepare --execute again before acknowledgement.')
        receipt['delivery'] = dict(result, **receipt['package'])
        receipt['delivery'].pop('reused_from', None)
        receipt['delivery']['action'] = 'Continue with ## Next steps in the delivered body.'
        receipt.pop('package', None)  # Keep one full payload after preparation becomes delivery.
        write_receipt(root, session, path, receipt)
    failed, _ = stage('acknowledge', lambda: acknowledge(path, root, session), 'resumed')
    return failed or result


def wait(root, target, status, timeout=600, interval=2):
    """Poll until the newest handoff matching a topic or path reaches status (saved/open, or resumed)."""
    import time
    root = handoff_identity.project_root(root)
    deadline = time.monotonic() + max(0, timeout)
    real = os.path.realpath(os.path.expanduser(target))
    while True:
        matches = [r for r in ledger._records(root) if r['topic'] == target or os.path.realpath(r['path']) == real]
        if matches:
            newest = max(matches, key=lambda r: (r['ended'], r['path']))
            current = (newest['fm'].get('status') or 'open').lower()
            if current == ('resumed' if status == 'resumed' else 'open'):
                return dict(outcome='reached', path=newest['path'], status=status,
                            action=('Another session acknowledged this handoff; do not adopt it.' if status == 'resumed' else
                                    'The handoff is published and open; resume it with: resume ' + shlex.quote(newest['path'])))
        if time.monotonic() >= deadline:
            return dict(outcome='timeout', status=status, action='%r did not reach %s within %gs. Check the other session, then rerun wait with a longer --timeout.' % (target[:200], status, timeout))
        time.sleep(max(0.05, interval))


RETRIEVE_COMMANDS = frozenset(('retrieve', 'retrieve your handoff', 'retrieve handoff', 'show handoff', 'show the handoff'))
RESUME_COMMANDS = frozenset(('resume', 'resume handoff', 'resume the handoff', 'resume your handoff',
                             'continue the handoff', 'continue your handoff'))


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
    if not prompt and Record.load(selected['path']).paused:
        return ('Automatic handoff paused; checkpoint remains open at %s. Reduce re-entry context '
                'or wait for the restart budget, then explicitly ask to continue the handoff.' % selected['path'])
    if not session:
        return 'An open handoff is available. Supply a unique session identity to prepare; never borrow another session identity.'
    execute = resume or (auto and not retrieve)
    # With --session, `resume` is the one-shot command that claims, verifies and acknowledges.
    command = 'python3 %s %s %s --session %s' % (shlex.quote(os.path.join(hooks_dir(), 'handoff_ledger.py')),
                                                 'resume' if execute else 'prepare', shlex.quote(selected['path']), shlex.quote(session))
    suffix = ' It claims, verifies and acknowledges in one call; then continue with ## Next steps in its body.' if execute else ' Retrieve only; leave execution skills, verification and next steps until continuation is authorized.'
    return "Handoff '%s' (ended %s, %.0fd old) — %s. " % (selected['topic'][:80],selected['ended'][:32],selected['age_days'],selected['description'][:240]) + (('Fully automatic: this terminal\'s handoff; proceed without asking. ' if selected.get('terminal') else 'Fully automatic: newest of %d; proceed without asking. ' % result['total']) if newest else 'Proceed without asking. ' if auto and execute else '') + 'Run ' + command + '.' + suffix


def history(root, topic=None, offset=0, limit=10):
    records = ledger._records(handoff_identity.project_root(root))
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
    match = re.search(r'version:\s*"([^"\n]+)"', read_text(path))
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
                r = decode_state_json(line)
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
                  project_id=hashlib.sha256(handoff_identity.project_root(root).encode()).hexdigest()[:16],
                  session_id=session, metrics=result.get('metrics', {}),
                  startup_input_tokens=note.get('startup_input_tokens'),
                  observed_input_tokens=note.get('observed_input_tokens'))
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, 'a') as f:
            f.write(json.dumps(record) + '\n')
    except OSError:
        pass  # optional analytics never makes a completed checkpoint fail


def delivery_metrics(result, root, session, fields=None):
    """Exact JSON bytes, estimated tokens and headroom against a sampled occupancy.

    Only emitted bytes are added to the host sample: loaded workflow and reused
    content already live in that sample. Schema costs and future reads are unknown.
    """
    metrics = result['metrics']
    def serialized_size():
        delivered = project_output(result, fields)
        return len(json.dumps(delivered, ensure_ascii=False).encode())
    note = ledger.read_session_note(root, session=session)
    observed, threshold = note.get('observed_occupancy_tokens'), note.get('threshold')
    if type(observed) is int and type(threshold) is int:
        metrics.update(observed_occupancy_tokens=observed, handoff_threshold_tokens=threshold,
                       threshold_source=note.get('threshold_source'), window_tokens=note.get('window'),
                       window_source=note.get('window_source'),
                       occupancy_estimate_basis='host sample + UTF-8 delivery bytes / 4; excludes future schemas and reads')
        # The warning itself contributes to the delivered size.
        if threshold - observed < 60000:
            result.setdefault('warnings', []).append('Estimated working room after resume is below 60000 tokens; narrow deferred reads and inspect re-entry overhead.')
    size = -1
    while size != serialized_size():
        size = serialized_size()
        metrics.update(delivery_bytes=size, estimated_delivery_tokens=(size + 3) // 4)
        if type(observed) is int and type(threshold) is int:
            occupancy = observed + metrics['estimated_delivery_tokens']
            metrics.update(estimated_occupancy_after_resume_tokens=occupancy,
                           estimated_working_room_tokens=threshold - occupancy)
            if threshold - occupancy < 60000 and not any('working room' in w for w in result.get('warnings', [])):
                result.setdefault('warnings', []).append('Estimated working room after resume is below 60000 tokens; narrow deferred reads and inspect re-entry overhead.')
    if result['outcome'] == 'resumed':
        note.update(resume_delivery_tokens=metrics['estimated_delivery_tokens'])
        try:
            write_json(handoff_identity.session_note_path(root, session), note)
        except OSError:
            pass


def save_draft(a):
    """Build the save draft from JSON and flags, and settle a.root; a dict with outcome is a refusal."""
    if a.input or not (a.topic or a.body):
        raw = read_cli_input(a.input or '-')
        draft = decode_state_json(raw)
    else:
        draft = {}
    if not isinstance(draft, dict):
        raise ValueError('Draft must be a JSON object')
    if a.topic:
        draft['topic'] = a.topic
    if a.description:
        draft['description'] = a.description
    if a.body:
        draft['body'] = read_text(a.body)
    if a.attach:
        # path#Lstart-Lend keeps its range so prepare delivers only that excerpt
        attached = [re.fullmatch(r'(.+?)(#L[1-9][0-9]*-L[1-9][0-9]*)?', p).groups('') for p in a.attach]
        missing = [p for p, _ in attached if not os.path.isfile(p)]
        if missing:
            raise ValueError('Attachment not found: ' + ', '.join(missing))
        existing = draft.get('references') or []
        existing = ledger._split_csv(existing) if isinstance(existing, str) else list(existing)
        draft['references'] = existing + [os.path.realpath(p) + lines for p, lines in attached]
    recorded = predecessor_project(draft['predecessor']) if draft.get('predecessor') else ''
    if a.root is None:
        a.root = recorded or '.'
    elif recorded and os.path.realpath(handoff_identity.project_root(a.root)) != os.path.realpath(recorded):
        return dict(outcome='conflict', action='Predecessor belongs to project %s but save targets %s. Drop the root argument to save into the predecessor project, or use a distinct topic without predecessor.'
                    % (recorded, os.path.realpath(handoff_identity.project_root(a.root))))
    return draft


def redirect_save_root(a, explicit):
    """Settle the project a save files under when the root was not named explicitly.

    Returns (refusal, note). A shell in a temp dir (for example where the draft
    lives) must not file the checkpoint there: use the project this session's hooks
    recorded, else refuse. Nothing found in a temp dir (.handoffs/, .git) counts as
    consent, since a stray save or another local user can create it; only an
    explicit root does."""
    if explicit:
        return None, ''
    start = os.path.realpath('.')
    root = os.path.realpath(handoff_identity.project_root(a.root))
    if not (ledger.under_temp_base(root) or (a.root == '.' and ledger.under_temp_base(start))):
        return None, ''
    recorded = ledger.read_project_pointer(getattr(a, 'session', None))
    if recorded and a.root == '.':
        a.root = recorded
        return None, ('Saved under project %s recorded by this session\'s hooks, not the temporary directory %s.'
                      % (recorded, start))
    return dict(outcome='invalid', project=root, action=(
        'The save root %s is in a temporary folder and no project is recorded for this session. Run save from '
        'the project directory, or name the root explicitly (save <root>, e.g. save .) if this folder really '
        'is the project.' % root)), ''


def cli(argv):
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='command', required=True)
    session_p = sub.add_parser('session', description='Read current identity, transcript path and pressure state without modifying hook state or scanning transcripts.')
    session_p.add_argument('--root', default='.')
    report_p = sub.add_parser('report', description='Summarize live resume metrics, excluding tests and separating legacy origin.')
    report_p.add_argument('--log', default=os.path.expanduser('~/.context-watch/resumes.jsonl'))
    lookup_p = sub.add_parser('lookup')
    lookup_p.add_argument('root', nargs='?', default='.')
    lookup_p.add_argument('--max-age-days', type=int, default=14)
    lookup_p.add_argument('--offset', type=int, default=0)
    lookup_p.add_argument('--topic')
    lookup_p.add_argument('--session', help='Optional current session identity for read-only ownership hints')
    save_p = sub.add_parser('save', description='Publish a JSON draft: topic (kebab-case), description, body (Markdown with Objective, Current state and Next steps headings). Optional: predecessor, skills, references, optional_references, verify, reason, continuity (items with id/text/status; delegated needs owner; resolved/retired needs evidence). Trusted predecessor dependencies inherit when omitted; [] replaces them. Continuity merges by id; omission or [] retains prior items. Metadata is generated. Use --template for a valid draft; edit its facts before saving.')
    save_p.add_argument('root', nargs='?', help="Default: the predecessor's project, else the current directory's")
    save_p.add_argument('--session')
    save_p.add_argument('--request-id')
    save_p.add_argument('--template', action='store_true', help='Print a valid JSON draft without writing a checkpoint')
    save_p.add_argument('--input', help='JSON draft path or - for stdin; default stdin unless --topic/--body are given')
    save_p.add_argument('--topic', help='Draft topic (kebab-case); overrides the JSON draft')
    save_p.add_argument('--description', help='Draft description; overrides the JSON draft')
    save_p.add_argument('--body', help='Markdown file used as the draft body; overrides the JSON draft')
    save_p.add_argument('--attach', action='append', default=[], help='File the next session must read; repeatable')
    recover_p = sub.add_parser('recover-transfer', description='Bind an untraceable legacy transfer only after explicit user confirmation of the actual resuming terminal.')
    recover_p.add_argument('path')
    recover_p.add_argument('--root', default='.')
    recover_p.add_argument('--session')
    recover_p.add_argument('--previous-session', required=True)
    recover_p.add_argument('--resumed-at', required=True)
    recover_p.add_argument('--fingerprint', required=True, help='Exact checkpoint fingerprint from resolve --json; binds the user confirmation to unchanged content')
    recover_p.add_argument('--confirm-holder', action='store_true')
    repair_p = sub.add_parser('repair', description='Complete publication from authenticated durable intent; no original draft required.')
    repair_p.add_argument('path')
    repair_p.add_argument('--root', default='.')
    repair_p.add_argument('--session')
    prepare_p = sub.add_parser('prepare')
    prepare_p.add_argument('path')
    prepare_p.add_argument('--root', default='.')
    prepare_p.add_argument('--session', help='Default: $HANDOFF_SESSION_ID, else the session the hooks recorded for this terminal')
    prepare_p.add_argument('--execute', action='store_true')
    prepare_p.add_argument('--reuse-receipt', help='Receipt from content still in this session context; omit after context loss')
    prepare_p.add_argument('--catalog', help='JSON installed skills mapping plus already-loaded canonical paths')
    prepare_p.add_argument('--budget-bytes', type=int, default=PACKAGE_BYTES, help='Recommended package size in bytes; advisory only')
    ack_p = sub.add_parser('acknowledge')
    ack_p.add_argument('path')
    ack_p.add_argument('--root', default='.')
    ack_p.add_argument('--session', help='Default: $HANDOFF_SESSION_ID, else the session the hooks recorded for this terminal')
    verify_p = sub.add_parser('verify')
    verify_p.add_argument('path')
    verify_p.add_argument('--root', default='.')
    verify_p.add_argument('--session', help='Default: $HANDOFF_SESSION_ID, else the session the hooks recorded for this terminal')
    verify_p.add_argument('--timeout', type=float, default=60)
    verify_p.add_argument('--confirm-verify', action='store_true', help='The user approved running this untrusted checkpoint\'s verify command')
    resume_p = sub.add_parser('resume', description='Select, prepare, verify and acknowledge a handoff in one call; the result says what to do next.')
    resume_p.add_argument('target', nargs='?', help='Topic or path; default: the handoff lookup selects')
    resume_p.add_argument('--root', default='.')
    resume_p.add_argument('--session', help='Default: $HANDOFF_SESSION_ID, else the session the hooks recorded for this terminal')
    resume_p.add_argument('--budget-bytes', type=int, default=PACKAGE_BYTES, help='Recommended package size in bytes; advisory only')
    resume_p.add_argument('--confirm-verify', action='store_true', help='The user approved running the verify command of a checkpoint this machine did not save')
    resume_p.add_argument('--catalog', help='JSON installed skills mapping plus paths already loaded in this context')
    resume_p.add_argument('--reuse-receipt', help='Receipt for body/references still in this session context; omit after context loss')
    resume_p.add_argument('--legacy-next-step', action='store_true', help='Also emit the Next steps text (duplicates body) for older consumers')
    wait_p = sub.add_parser('wait', description='Poll until the newest handoff for a topic or path is saved/open or resumed.')
    wait_p.add_argument('target', help='Topic or path')
    wait_p.add_argument('--status', required=True, choices=('saved', 'open', 'resumed'))
    wait_p.add_argument('--root', default='.')
    wait_p.add_argument('--timeout', type=float, default=600)
    wait_p.add_argument('--interval', type=float, default=2)
    history_p = sub.add_parser('history')
    history_p.add_argument('root', nargs='?', default='.')
    history_p.add_argument('--topic')
    history_p.add_argument('--offset', type=int, default=0)
    history_p.add_argument('--limit', type=int, default=10)
    for parser in (session_p, lookup_p, save_p, prepare_p, ack_p, verify_p, resume_p):
        parser.add_argument('--fields', help='Comma-separated optional top-level keys; diagnostics and successful delivery context are always included')
    a = p.parse_args(argv)
    if a.command == 'session':
        result = session_info(a.root)
        if a.fields:
            keep = {'outcome', 'action'} | set(ledger._split_csv(a.fields))
            result = {k: v for k, v in result.items() if k in keep}
        print(json.dumps(result))
        return 0
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
                body='## Objective\nState the authorized goal.\n## Constraints\nList the limits the next session must keep.\n## Current state\nRecord verified evidence.\n## Decisions\nRecord choices already made and why.\n## Pending operations\nName any operation started but not confirmed, or None.\n## Next steps\nName the exact next action; put files it edits or depends on in references as path#Lstart-Lend.\n',
                references=[], skills=[],
                continuity=[dict(id='goal', text='Replace with the original authorized goal and durable constraints.', status='open')])))
            return 0
    root_note = ''
    if a.command == 'save':
        explicit_root = a.root is not None  # positional default is None, so `save .` stays explicit
        try:
            draft = save_draft(a)
        except (OSError, ValueError) as exc:
            print(json.dumps(dict(outcome='invalid', action=str(exc)[:500] + '; correct the input and retry.')))
            return 1
        if isinstance(draft, dict) and draft.get('outcome'):
            print(json.dumps(draft))
            return 1
    if hasattr(a, 'session'):
        recorded = terminal_session(getattr(a, 'root', '.'))
        host = handoff_identity.host_session()
        chosen = recorded or host or a.session or default_session(getattr(a, 'root', '.'))
        retry = False
        if a.command == 'save' and a.session and chosen != a.session:
            request = a.request_id or hashlib.sha256(json.dumps(draft, sort_keys=True).encode()).hexdigest()[:32]
            retry = original_retry_session(a.root, a.session, request)
        if retry:
            print('Finishing the interrupted save of session %s from this terminal.' % a.session, file=sys.stderr)
            chosen = a.session
        elif recorded and a.session and a.session != recorded:
            print('Using hook-recorded session %s instead of differing --session %s.'
                  % (recorded, a.session), file=sys.stderr)
        elif not recorded and host and a.session and a.session != host:
            print('Using current host session %s instead of differing --session %s.' % (host, a.session), file=sys.stderr)
        a.session = chosen
        if not a.session and a.command != 'lookup':
            print(json.dumps(NO_SESSION))
            return 1
    if a.command == 'save':
        refused, root_note = redirect_save_root(a, explicit_root)
        if refused:
            print(json.dumps(refused))
            return 1
        if root_note:
            print(root_note, file=sys.stderr)
    try:
        if a.command == 'history':
            result = history(a.root, a.topic, a.offset, a.limit)
        elif a.command == 'recover-transfer':
            result = recover_transfer(a.path, a.root, a.session, a.previous_session, a.resumed_at, a.fingerprint, a.confirm_holder)
        elif a.command == 'repair':
            result = repair(a.path, a.root, a.session)
        elif a.command == 'verify':
            result = verify(a.path, a.root, a.session, a.timeout, a.confirm_verify)
        elif a.command == 'prepare':
            catalog = load_state_json(a.catalog) if a.catalog else None
            result = prepare(a.path, a.root, a.session, a.execute, catalog, a.budget_bytes, a.reuse_receipt)
        elif a.command == 'acknowledge':
            result = acknowledge(a.path, a.root, a.session)
        elif a.command == 'wait':
            result = wait(a.root, a.target, a.status, a.timeout, a.interval)
        elif a.command == 'resume':
            catalog = load_state_json(a.catalog) if a.catalog else None
            result = resume(a.root, a.session, a.target, a.budget_bytes, a.confirm_verify, catalog,
                            a.reuse_receipt, a.legacy_next_step or 'next_step' in ledger._split_csv(a.fields or ''))
        elif a.command == 'save':
            request = a.request_id or hashlib.sha256(json.dumps(draft, sort_keys=True).encode()).hexdigest()[:32]
            result = save(a.root, a.session, request, draft)
            if root_note:
                result['warnings'] = list(result.get('warnings') or []) + [root_note]
        else:
            result = lookup(a.root, a.max_age_days, a.offset, a.topic, session=a.session)
    except (OSError, ledger.ConflictError, ValueError, TypeError, KeyError) as exc:
        result = dict(outcome='blocked', action=str(exc)[:500] + '; preserve the draft/checkpoint and restore access or correct the input before retrying. Do not clear or adopt.')
    result.setdefault('project', handoff_identity.project_root(getattr(a, 'root', '.')))  # shows where a 'none' looked
    if a.command == 'resume' and result['outcome'] == 'resumed':
        delivery_metrics(result, a.root, a.session, a.fields)
    telemetry(a.command, a.root, getattr(a, 'session', None), result)
    delivered_path = result.get('path')
    result = project_output(result, getattr(a, 'fields', None))
    output = json.dumps(result, ensure_ascii=False)
    print(output)
    sys.stdout.flush()
    if a.command == 'resume' and result['outcome'] == 'resumed':
        try:
            delivered(a.root, delivered_path, a.session)
        except (OSError, ValueError, TypeError, KeyError):
            pass  # Keep pending replay if post-delivery diagnostics cannot persist.
    if a.command == 'save' and result['outcome'] == 'incomplete':
        return 1
    return 1 if result['outcome'] in ('error', 'blocked', 'invalid', 'conflict', 'needs-context', 'needs-original-draft', 'verification-failed', 'verification-timeout', 'timeout', 'needs-confirmation') else 0


if __name__ == '__main__':
    sys.exit(cli(sys.argv[1:]))
