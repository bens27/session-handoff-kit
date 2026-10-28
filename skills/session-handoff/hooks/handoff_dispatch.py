"""Deterministic lifecycle dispatcher: one validated decision per request. Stdlib only.

Read-only except for the persisted synthetic identity and explicit holds. It never
launches a restart and never treats checkpoint text as authorization.
"""
import hashlib
import json
import os
import re
import sys

import handoff_identity
from handoff_holds import (  # noqa: F401  (re-exported for existing callers)
    SCOPE, _holds_dir, _hold_roots, read_holds, applicable_holds, set_hold)
import handoff_intents as intents
import handoff_ledger as ledger
import handoff_workspace as workspace
from handoff_record import Record
import handoff_protocol as protocol
from handoff_contract import (
    DISPATCH_BRANCHES as BRANCH, DISPATCH_CODES as CODES, DISPATCH_RETRY as RETRY,
    dispatch_result as result, blocked_result as blocked, python_command, emit_output, exit_status)

LEDGER = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'handoff_ledger.py')
PREFLIGHT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'restart_preflight.py')
OPERATIONS = ('save', 'pressure', 'retrieve', 'resume', 'autoresume')
REQUEST_KEYS = frozenset(('operation', 'prompt', 'target', 'session', 'authorization', 'override', 'host',
                          'request_id', 'reuse', 'expect', 'topic', 'draft'))
DRAFT_REQUIRED = ('topic', 'description', 'body', 'suggested_skills')
# ticket/base/role are #95's checkpoint-header fields: save checks their format and writes them.
DRAFT_OPTIONAL = frozenset(('predecessor', 'references', 'optional_references', 'continuity', 'skills',
                            'verify', 'verify_baseline', 'reason', 'ticket', 'base', 'role'))
HOST_WINDOW = intents.HOST_WINDOW

HOST_KEYS = frozenset(('restart_capable', 'workers', 'hold', 'restart_request_id'))

NEUTRAL = re.compile(r'^\s*(handoff-status:|\[context-watch\]\s*handoff saved\b)', re.I)
PRESSURE = re.compile(r'^\s*(\[context-watch\]|context-watch:).*\b(write|publish|save)\b.{0,40}\bhandoff\b', re.I | re.S)
QUOTED = re.compile(r"\"[^\"]*\"|`[^`]*`|“[^”]*”|(?<!\w)'[^']*'(?!\w)|(?<!\w)‘[^’]*’(?!\w)")
MAINTENANCE = re.compile(r'\b(skill|skill\.md|pointer|hooks?|tests?|fix|edit|refactor|docs?|description|verifier|fixtures?)\b', re.I)
LIFECYCLE_WORD = re.compile(r'\b(handoffs?|resume|retrieve|checkpoint|park)\b', re.I)
SAVE = re.compile(r'^(park|save|checkpoint)( (this|the|my|a))? (work|session|handoff|checkpoint)'
                  r'|^hand off( (this|the|my))? (work|session|handoff)$|^(save|write)( a| the)? handoff$'
                  r'|^session handoff( to (yourself|your successor|successor|self))?$|^hand ?off$', re.I)


def targeted_command(prompt, root=None):
    """Parse an exact opening command; readable names must match stored metadata."""
    if not isinstance(prompt, str) or QUOTED.search(prompt):
        return None
    match = re.fullmatch(r'(resume|retrieve)\s+(.+)', prompt.strip(), re.I)
    if not match or protocol.generic_intent(prompt):
        return None
    operation, target = match.group(1).lower(), match.group(2)
    if target.startswith(("'", '‘')) or target.endswith(("'", '’')):
        return None
    if not re.search(r'\s', target) and target.endswith('.md'):
        return operation, target
    if root is None:
        return None
    topic = ledger.slugify_topic(target)
    errors = []
    records = ledger._records(handoff_identity.project_root(root), errors)
    if errors:
        return None
    topic_records = [r for r in records if r['topic'] == topic]
    matches = [r for r in records if r['fm'].get('status', 'open') == 'open'
               and r['fm'].get('description', '').casefold() == target.casefold()]
    if topic_records:
        heads = [r for r in topic_records if r['fm'].get('status', 'open') == 'open']
        conflict = any(r['topic'] != topic for r in matches)
        if not conflict and (target == topic or len(heads) > 1):
            return operation, topic
        # Readable topics and descriptions share the ownership check. Conflicts
        # retain all candidates; only canonical topic spelling selects explicitly.
        matches += heads or [max(topic_records, key=lambda r: (r['ended'], r['path']))]
    if matches:
        unique = {r['path']: r for r in matches}
        return operation, [dict(path=r['path'], topic=r['topic'], ended=r['ended'],
                                description=r['fm'].get('description', ''),
                                terminal=r['fm'].get('terminal'), session_id=r['fm'].get('session_id'))
                           for r in unique.values()]
    return None


def classify(prompt):
    """Label a user prompt; only exact lifecycle commands route to an operation."""
    if not isinstance(prompt, str) or not prompt.strip():
        return 'none'
    if NEUTRAL.search(prompt):
        return 'neutral'
    if PRESSURE.search(prompt):
        return 'pressure'
    text = QUOTED.sub(' ', prompt)
    if text != prompt:
        return 'none'  # a quoted lifecycle phrase is a mention, never a command
    targeted = targeted_command(text)
    if targeted:
        return targeted[0]
    if MAINTENANCE.search(text) and LIFECYCLE_WORD.search(text):
        return 'maintenance'
    intent = protocol.generic_intent(text)
    if intent:
        return intent
    if SAVE.search(text.strip().rstrip('.!').strip()):
        return 'save'
    words = text.strip().split()
    opening = re.match(r'^(?:handoffs?|resume|retrieve|checkpoint|park|hand off)(?:\s|[.!]?$)',
                       text.strip(), re.I)
    park_object = (words[0].lower().rstrip('.!') != 'park' or
                   re.fullmatch(r'park(?:\s+(?:(?:this|the|my|a|your)\s+)?'
                                r'(?:work|session|handoff|checkpoint|this|it|that))?[.!]*',
                                text.strip(), re.I))
    if opening and park_object and len(words) <= 4 and '?' not in text:
        return 'lifecycle_unrecognised'
    return 'none'


def _command(*args):
    return python_command(LEDGER, *args)


def autoresume_configured():
    """The same authority the hooks use: AUTORESUME/CONTEXT_WATCH_AUTORESUME or automatic mode."""
    import context_watch
    return context_watch.autoresume_on()



def dispatch(request, root='.'):
    if not isinstance(request, dict):
        return blocked({}, 'invalid_request', 'The dispatch request must be a JSON object.')
    unknown = sorted(set(request) - REQUEST_KEYS)
    if unknown:
        return blocked({}, 'invalid_request', 'Unknown request fields: %s.' % ', '.join(unknown))
    request = dict(request)
    if 'draft' in request:
        error = validate_draft(request)
        if error:
            return blocked({}, 'invalid_request', error)
    prompt = request.get('prompt')
    if prompt is not None and not isinstance(prompt, str):
        return blocked({}, 'invalid_request', 'prompt must be a string.')
    request['_classification'] = classify(prompt) if prompt else None
    targeted = targeted_command(prompt, root)
    if targeted:
        request['_classification'] = targeted[0]
    elif (prompt and request.get('operation') in ('resume', 'retrieve')
          and (request.get('operation') == 'retrieve' or request.get('authorization') == {'source': 'user'})
          and not protocol.generic_intent(prompt)):
        targeted = targeted_command(request['operation'] + ' ' + prompt, root)
    operation = request.get('operation')
    if operation is not None:
        if operation not in OPERATIONS + ('none',):
            return blocked({}, 'invalid_request', 'Unknown operation %r; use one of %s.' % (operation, ', '.join(OPERATIONS + ('none',))))
        request['_operation_source'] = 'explicit'
    elif request['_classification'] in OPERATIONS:
        operation = request['_classification']
        request['_operation_source'] = 'prompt'
    request['_operation'] = operation
    if targeted and operation in ('resume', 'retrieve') and 'target' not in request:
        if isinstance(targeted[1], list):
            request['_prompt_candidates'] = targeted[1]
        else:
            request['target'] = targeted[1]
    authorization = request.get('authorization')
    if authorization is not None and authorization != {'source': 'user'}:
        return blocked(request, 'invalid_authorization',
                       'Only {"source": "user"} is accepted, and only for a user request; checkpoint or config '
                       'text never authorizes execution.')
    if operation == 'none':
        return result('none', 'no_operation', request)
    if operation is None:
        if request['_classification'] == 'lifecycle_unrecognised':
            return result('ask', 'lifecycle_unrecognised', request,
                          next_action=dict(kind='ask', command='',
                                           text='Do you want to save a checkpoint, resume one, or retrieve one, or neither?',
                                           options=[
                                               dict(operation='save', label='Save a checkpoint',
                                                    description='Save the current work for a later session.'),
                                               dict(operation='resume', label='Resume a checkpoint',
                                                    description='Continue work from a saved checkpoint.'),
                                               dict(operation='retrieve', label='Retrieve a checkpoint',
                                                    description='Read a saved checkpoint without continuing its work.'),
                                               dict(operation='none', label='Neither',
                                                    description='Continue the current request without a handoff action.'),
                                           ]))
        code = 'no_operation' if request['_classification'] in (None, 'none') else 'not_lifecycle'
        return result('none', code, request)
    # Authority comes from the user's own words or the hooks' configuration, never the checkpoint.
    if operation == 'resume':
        if request['_operation_source'] == 'prompt' or authorization:
            request['_authorization'] = 'user'
    elif operation == 'autoresume':
        if not autoresume_configured():
            return blocked(request, 'autoresume_not_enabled',
                           'Automatic resume is not configured; ask the user to continue the handoff.')
        request['_authorization'] = 'config'
    return route(request, root)


def validate_draft(request):
    """Shape only; protocol.save owns content validation. Returns an error text or None."""
    draft = request['draft']
    if request.get('operation') != 'save':
        return 'draft is accepted only with operation "save".'
    if not isinstance(draft, dict):
        return 'draft must be a JSON object.'
    missing = [k for k in DRAFT_REQUIRED if k not in draft]
    unknown = sorted(set(draft) - set(DRAFT_REQUIRED) - DRAFT_OPTIONAL)
    if missing or unknown:
        return 'draft needs %s; unknown draft fields: %s.' % (', '.join(DRAFT_REQUIRED), ', '.join(unknown) or 'none')
    for key in ('topic', 'description', 'body'):
        if not isinstance(draft[key], str) or not draft[key].strip():
            return 'draft.%s must be a nonempty string.' % key
    suggested = draft['suggested_skills']
    if (not isinstance(suggested, list) or not all(isinstance(n, str) and n.strip() for n in suggested)
            or len(set(suggested)) != len(suggested)):
        return 'draft.suggested_skills must be a list of unique skill names ([] allowed).'
    if request.get('topic') is not None and request['topic'] != draft['topic']:
        return 'topic and draft.topic differ.'
    request['topic'] = draft['topic']
    return None


stale_marked = intents.stale_marked


def stale_text(record):
    return ('Stale restart intent %s (session %s) was dispatched over %d minutes ago with no host evidence; it is '
            'preserved but never auto-adopted. Recover with a new save from the requesting session, or an explicit '
            'adopt: %s' % (record['request_id'], record['session'], HOST_WINDOW // 60,
                          intents.adopt_command(record['root'], record['request_id'])))


def run_restart(root, path, workers, capable, hold):
    """restart_preflight's own `restart` command, out of process: dispatch and preflight never import each other."""
    import subprocess
    argv = [sys.executable, '-B', PREFLIGHT, 'restart', '--root', root, '--path', path, '--workers', json.dumps(workers)]
    argv += ['--host-capable'] if capable else []
    argv += ['--hold'] if hold else []
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=120)
        out = json.loads(done.stdout)
        if not isinstance(out, dict):
            raise ValueError('not a JSON object')
        return out
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return dict(outcome='blocked', reason='preflight-unavailable', stage='preflight', detail=str(exc)[:300],
                    action='Do not call agents_restart; restart state is unknown. Reconcile before any retry.')


def draft_request_id(root, session, draft):
    """Same session and same draft give the same ID before and after it lands: a retry never publishes twice."""
    key = '\0'.join((os.path.realpath(root), session, json.dumps(draft, sort_keys=True)))
    return 'draft-' + hashlib.sha256(key.encode()).hexdigest()[:32]


def published_under(root, session, request_id):
    """Front matter of the checkpoint this save request already published, or None."""
    identity = hashlib.sha256((os.path.realpath(root) + '\0' + session + '\0' + request_id).encode()).hexdigest()
    return next((r['fm'] for r in ledger._records(root, []) if r['fm'].get('checkpoint_id') == identity), None)


def draft_save(root, request, session, target, common):
    """Publish the draft, then on the AgentsRoom profile run the restart preflight once."""
    draft = dict(request['draft'])
    request_id = request.get('request_id') or draft_request_id(root, session, draft)
    if not draft.get('predecessor'):
        # A retry keeps the predecessor its first attempt recorded, so the content hash still matches.
        prior = published_under(root, session, request_id)
        predecessor = prior.get('predecessor') if prior is not None else target.get('predecessor')
        if predecessor:
            draft['predecessor'] = predecessor
    warnings = list(common.pop('warnings', []))
    catalog = protocol.installed_catalog()['skills']
    warnings += ['Suggested skill %r is not installed; it stays advisory.' % n
                 for n in draft['suggested_skills'] if n not in catalog]
    try:
        saved = protocol.save(root, session, request_id, draft)
    except (OSError, ValueError) as exc:
        saved = dict(outcome='invalid', action=str(exc))
    extra = dict(request_id=request_id, suggested_skills=list(draft['suggested_skills']), **common)
    if saved.get('outcome') != 'saved':
        return blocked(request, 'publication_failed', 'Publication %s: %s Keep the draft; never clear.'
                       % (saved.get('outcome'), saved.get('action', '')), stage='publication',
                       publication=saved, warnings=warnings, **extra)
    warnings += saved.get('warnings') or []
    extra.update(path=saved['path'], checkpoint_id=saved['checkpoint_id'], transition=saved['transition'],
                 warnings=warnings)
    if saved['transition'].get('kind') != 'agents_restart':
        return result('proceed', 'saved', request, next_action=dict(
            kind='continue', command='', text=saved['transition'].get('action', saved.get('action', ''))), **extra)
    host = request.get('host') or {}
    workers = host.get('workers')
    if not isinstance(host.get('restart_capable'), bool) or not isinstance(workers, list) or not isinstance(host.get('hold'), bool):
        return blocked(request, 'restart_blocked', 'The checkpoint is published, but restart needs current host '
                       'observations: restart_capable (bool), the complete workers list ([] when observed empty) and '
                       'hold (bool). Do not call agents_restart; continue manually in a fresh session.',
                       stage='host_observations', **extra)
    out = run_restart(root, saved['path'], workers, host['restart_capable'], host['hold'])
    warnings += out.get('warnings') or []
    if out.get('outcome') != 'ready' or out.get('call') != 'agents_restart':
        extra['request_id'] = out.get('request_id', request_id)
        return blocked(request, 'restart_blocked', 'The checkpoint is published; restart did not pass (%s). %s'
                       % (out.get('reason') or out.get('outcome'), out.get('action', '')),
                       stage=out.get('stage') or out.get('reason') or out.get('outcome'), preflight=out, **extra)
    extra.update(save_request_id=request_id, request_id=out['request_id'])
    return result('proceed', 'restart', request, call='agents_restart', agent=out['agent'], prompt=out['restart_prompt'],
                  next_action=dict(kind='call', command='agents_restart', text=(
                      'Dispatch is marked started. Write the session status, then call agents_restart(agent=%s, '
                      'prompt=<prompt>) exactly as the final tool action and end the turn. Suggested skills are '
                      'advisory only.' % out['agent'])), **extra)


def _identity_file(root, terminal):
    key = hashlib.sha256((os.path.realpath(root) + '\0' + terminal).encode()).hexdigest()[:24]
    return os.path.join(root, '.handoffs', 'dispatch', 'identity-%s.json' % key)


def synthetic_identity(root):
    """One persisted id per project terminal; never shared across terminals."""
    terminal = protocol.terminal_id()
    if not terminal:
        return ''
    path = _identity_file(root, terminal)
    try:
        handoff_identity.local_state_dir(root, 'dispatch', create=True)
    except handoff_identity.UntrustedStateDir:
        return ''  # never adopt or persist an identity under a planted .handoffs
    with ledger.locked(path + '.lock'):
        try:
            value = protocol.load_state_json(path, 4096).get('session')
            if isinstance(value, str) and value.startswith('synthetic-'):
                return value
        except (OSError, ValueError):
            pass
        import uuid
        value = 'synthetic-' + str(uuid.uuid4())
        protocol.write_json(path, dict(session=value, terminal=terminal, root=os.path.realpath(root)))
        return value


def resolve_identity(root, request):
    """Hook record > host thread > explicit > environment > persisted synthetic (save/retrieve only)."""
    explicit = request.get('session')
    if explicit is not None and (not isinstance(explicit, str) or not explicit.strip()):
        return None, 'session must be a nonempty string.'
    explicit = (explicit or '').strip()
    authoritative = [(source, value) for source, value in (
        ('hook', handoff_identity.terminal_session(root)), ('host', handoff_identity.host_session())) if value]
    environment = handoff_identity.environment_session()
    identity = dict(session='', source='none', provenance='', conflict=None)
    if authoritative:
        source, value = authoritative[0]
        identity.update(session=value, source=source,
                        provenance='context-watch terminal record' if source == 'hook' else 'CODEX_THREAD_ID')
        if explicit and explicit != value:
            identity['conflict'] = {'explicit': explicit, source: value}
    elif explicit:
        identity.update(session=explicit, source='explicit', provenance='request.session')
        if environment and environment != explicit:
            identity['conflict'] = {'explicit': explicit, 'environment': environment}
    elif environment:
        identity.update(session=environment, source='environment', provenance='HANDOFF_SESSION_ID')
    elif request['_operation'] in ('save', 'pressure', 'retrieve'):
        value = synthetic_identity(root)
        if value:
            identity.update(session=value, source='synthetic', provenance='.handoffs/dispatch (terminal-scoped)')
    return identity, None


TIMES = ('created', 'parked', 'last_resumed')


def _candidate(item):
    return dict(path=item['path'], topic=item['topic'], ended=item['ended'], description=item['description'][:240],
                mine=bool(item.get('mine')), **{k: item.get(k, '') for k in TIMES})


def intent_target_request(root, request, host):
    """Let host.restart_request_id name the checkpoint before any ambiguity check.

    Applies only without an explicit target, and only to a valid, unexpired
    pending intent of this terminal; the intent's path is then an exact target,
    validated like any other. Anything else leaves selection unchanged, and
    pending_dispatch still reports invalid or foreign intents.
    """
    request_id = host.get('restart_request_id')
    if request.get('target') is not None or not isinstance(request_id, str):
        return request
    try:
        record = intents.status(root, request_id)
    except (OSError, ValueError):
        return request
    terminal = protocol.terminal_id()
    if (not record or record['state'] != 'pending' or intents.expired(record)
            or not terminal or record['terminal'] != terminal):
        return request
    return dict(request, target=record['path'])


def select_target(root, request, session):
    """Exact path/topic never falls back; otherwise zero/one/many owned is deterministic."""
    operation, target = request['_operation'], request.get('target')
    if request.get('_prompt_candidates'):
        terminal = protocol.terminal_id()
        candidates = [_candidate(dict(item, mine=(bool(terminal) and item['terminal'] == terminal
                      if item.get('terminal') else bool(session) and item.get('session_id') == session)))
                      for item in request['_prompt_candidates'][:5]]
        total = len(request['_prompt_candidates'])
        if total > 1:
            return dict(candidates=candidates, total=total), ('ask', 'target_ambiguous',
                    'That name matches %d checkpoints (showing %d); ask the user which one.' % (total, len(candidates)))
        if not candidates[0]['mine']:
            return dict(candidates=candidates), ('ask', 'foreign_only',
                                                'That readable name belongs to another session; ask the user to name its exact path or kebab-case topic.')
        target = candidates[0]['path']
    if target is not None:
        if not isinstance(target, str) or not target.strip():
            return None, ('blocked', 'invalid_request', 'target must be a nonempty path or topic.')
        is_path = os.sep in target or target.endswith('.md')
        if is_path:
            target = os.path.realpath(os.path.join(root, target))
            if not os.path.isfile(target):
                return dict(path=target), ('blocked', 'target_not_found', 'No checkpoint at %s. Exact targets never fall back; check the path.' % target)
        elif not ledger.TOPIC_RE.fullmatch(target):
            return None, ('blocked', 'invalid_request', 'target must be a checkpoint path or kebab-case topic.')
        resolved = ledger.resolve(target, root)
        chain = resolved['chain']
        if not chain:
            return dict(topic=target), ('blocked', 'target_not_found', 'No checkpoint for topic %r. Exact targets never fall back.' % target)
        heads = [c for c in chain if c['status'] == 'open']
        if not is_path and len(heads) > 1:
            return dict(topic=target, candidates=[dict(path=c['path'], ended=c['ended'], description=c['description']) for c in heads]), (
                'ask', 'topic_ambiguous', 'Topic %r has %d open checkpoints; ask the user which one.' % (target, len(heads)))
        if is_path:  # the ledger may spell the root unresolved (/tmp vs /private/tmp): keep its spelling
            path = next((c['path'] for c in chain if os.path.realpath(c['path']) == target), target)
        else:
            path = resolved['authoritative']
        info = dict(path=path, topic=resolved['topic'], authoritative=resolved['authoritative'], selection='exact')
        if is_path and resolved['authoritative'] != path:
            return info, ('blocked', 'target_superseded', 'This checkpoint was superseded by %s. Exact targets never switch; '
                          'ask the user before using the newer one.' % resolved['authoritative'])
        status = next(c['status'] for c in chain if c['path'] == path)
        if status == 'unpublished':
            recovery = protocol.publication_recovery(root, path)
            info['recovery'] = [recovery]
            return info, ('blocked', recovery.get('code', 'publication_incomplete'), recovery['text'])
        if status != 'open':
            if operation == 'retrieve':
                return info, ('proceed', 'history_only', 'Closed checkpoint (%s): inspection only.' % status)
            return info, ('blocked', 'target_closed', 'This checkpoint is %s; it cannot be resumed again.' % status)
        return info, None
    found = protocol.lookup(root, session=session)
    if found['outcome'] == 'error':
        return None, ('blocked', 'lookup_error', found['action'], True)
    if found['outcome'] == 'incomplete':
        return dict(recovery=found['recovery']), ('blocked', found['recovery'][0].get('code', 'publication_incomplete'), found['action'])
    mine = [i for i in found['items'] if i.get('mine')]
    selected = found.get('selected')
    if selected:
        item = next((i for i in found['items'] if i['path'] == selected), {})
        info = dict(path=selected, selection=found.get('selection') or ('owned' if mine else 'sole'),
                    **{k: item.get(k, '') for k in TIMES})
        if ledger.age_notice(item):
            info['age_notice'] = ledger.age_notice(item)
        if found.get('alternatives'):
            info['alternatives'] = [_candidate(i) for i in found['alternatives']]
            info['alternatives_total'] = found.get('alternatives_total', len(found['alternatives']))
        return info, None
    if len(mine) > 1:
        return dict(candidates=[_candidate(i) for i in mine]), ('ask', 'target_ambiguous', 'Several of this session\'s checkpoints are open; ask the user which one.')
    if found['total']:
        return dict(candidates=[_candidate(i) for i in found['items']]), (
            'ask', 'foreign_only', 'Open checkpoints belong to other sessions; never adopt them without the user naming one.')
    code = {'claimed': 'claimed_elsewhere', 'stale': 'stale_only'}.get(found['outcome'], 'no_open_handoff')
    return None, ('ask' if code == 'stale_only' else 'none', code, found['action'])


RECOVERY_KINDS = ('repair', 'recover-transfer')


def override_action(root, request, session):
    """Only the kit's validated explicit recovery commands; nothing bypasses identity.

    Runs before holds are read: like retrieve and save, recovery repairs ledger state
    and executes no next steps, so holds do not gate it."""
    override = request['override']
    if not isinstance(override, dict) or override.get('kind') not in RECOVERY_KINDS:
        return ('blocked', 'invalid_override', 'Overrides are limited to explicit recovery: %s.' % ', '.join(RECOVERY_KINDS)), None
    path = override.get('path')
    if not isinstance(path, str) or not os.path.isfile(path):
        return ('blocked', 'invalid_override', 'Recovery needs the exact existing checkpoint path.'), None
    if override['kind'] == 'repair':
        return None, _command('repair', path, '--root', root, '--session', session)
    fields = ('previous_session', 'resumed_at', 'fingerprint')
    if not all(isinstance(override.get(k), str) and override[k].strip() for k in fields) or not re.fullmatch(r'[0-9a-f]{64}', override['fingerprint']):
        return ('blocked', 'invalid_override', 'recover-transfer needs previous_session, resumed_at and a 64-hex fingerprint.'), None
    if override.get('user_confirmed') is not True:
        return ('ask', 'confirmation_required', 'Ask the user to confirm this terminal performed the legacy transfer '
                '(holder %s at %s); only their confirmation sets user_confirmed.' % (override['previous_session'], override['resumed_at'])), None
    return None, _command('recover-transfer', path, '--root', root, '--session', session, '--previous-session',
                          override['previous_session'], '--resumed-at', override['resumed_at'],
                          '--fingerprint', override['fingerprint'], '--confirm-holder')


def pending_dispatch(root, host, session, path=None, saving=False):
    """Durable restart intent for this session, terminal or checkpoint; never confirmed by us.

    The host request ID is optional: a read-only scan finds pending intents on this
    terminal or for the target checkpoint, and creates nothing when none exist.
    Returns (record, failure, warnings). Only records relevant to the caller (its
    session, terminal or target) can fail a dispatch, and save or pressure never
    fails on intent state: an unattributable, foreign or stale record is a warning.
    """
    warnings = []
    terminal = protocol.terminal_id()
    target = os.path.realpath(path) if path else None
    here = os.path.realpath(root)

    def invalid(where, why, relevant):
        text = 'Invalid restart intent %s (%s); preserve it and reconcile with the host.' % (where, why)
        if relevant and not saving:
            return ('blocked', 'dispatch_state_invalid', text[:-1] + ' before any lifecycle action.')
        warnings.append(text)
        return None

    reported = set()

    def expired(record):
        # adopt refuses an expired intent, so routing a successor to it is a dead end
        if record['request_id'] not in reported:
            reported.add(record['request_id'])
            warnings.append('Stale restart intent %s (session %s) expired after %d days; it is preserved but not '
                            'routed. Save a new checkpoint to restart.'
                            % (record['request_id'], record['session'], intents.TTL['pending'] // intents.DAY))

    request_id = host.get('restart_request_id')
    if request_id is not None:
        try:
            record = intents.status(root, request_id)
        except ValueError as exc:
            failure = invalid('for request %s' % request_id, exc, True)
            if failure:
                return None, failure, warnings
            record = None
        if record is not None and intents.expired(record):
            expired(record)
            record = None
        if record is not None and record['session'] == session and not (saving and stale_marked(record)):
            return record, None, warnings
    try:
        locations = intents.entries(root)
    except OSError as exc:
        text = 'Restart intents are unreadable (%s); restore access.' % exc
        if saving:
            return None, None, warnings + [text]
        return None, ('blocked', 'dispatch_state_invalid', text), warnings
    matches = []
    for where in locations:
        try:
            raw = protocol.load_state_json(where, 65536)
        except (OSError, ValueError) as exc:
            raw, error = None, exc
        fields = raw if isinstance(raw, dict) else {}
        relevant = ((fields.get('session') == session and bool(session))
                    or (bool(terminal) and fields.get('terminal') == terminal)
                    or (target is not None and isinstance(fields.get('path'), str)
                        and os.path.realpath(fields['path']) == target))
        if isinstance(fields.get('root'), str) and os.path.realpath(fields['root']) != here:
            warnings.append('Stale restart intent %s recorded for another project root %s (moved or copied '
                            'project); it does not apply here.' % (where, fields['root']))
            continue
        if raw is None:
            failure = invalid(where, error, False)
            continue
        try:  # status() validates strictly; the directory exists, so it creates nothing
            record = intents.status(root, fields.get('request_id'))
            if record is None:
                raise ValueError('it does not match its request ID')
        except (OSError, ValueError) as exc:
            failure = invalid(where, exc, relevant)
            if failure:
                return None, failure, warnings
            continue
        if record['state'] == 'pending' and relevant:
            if intents.expired(record):
                expired(record)
                continue
            if stale_marked(record):
                if record['request_id'] not in reported:
                    reported.add(record['request_id'])
                    warnings.append(stale_text(record))
                if saving:
                    continue  # recovery is a new save; the stale intent stays preserved
            matches.append(record)
    # This session's own intent first, then one for the target checkpoint
    matches.sort(key=lambda r: (r['session'] != session, r['path'] != target, -r['intent_at']))
    return (matches[0] if matches else None), None, warnings


def successor_action(root, record, session):
    """A successor session runs one atomic adopt: identity, retrieval, receipt, resume."""
    command = intents.adopt_command(record['root'], record['request_id'])
    return dict(kind='run', command=command, text=(
        'A restart intent (%s) from session %s is pending for this checkpoint; this session is its successor. '
        'Run this command exactly as given (no pipe or clipping); it verifies identity, retrieves, records the '
        'receipt and resumes, emitting the body once. Continue with ## Next steps. On blocked, preserve state and '
        'report the stage; never restart, dispatch another intent or pick another checkpoint.'
        % (record['request_id'], record['session'])))


def delivery_mode(root, session, path, fingerprint, reuse):
    """Fresh unless the caller proves the delivered content is still in this context."""
    if reuse is None:
        return dict(mode='fresh', reason='default'), None
    if not isinstance(reuse, dict) or not isinstance(reuse.get('receipt'), str) or not isinstance(reuse.get('context_hash'), str):
        return dict(mode='fresh', reason='invalid_reuse'), None
    if reuse['context_hash'] != fingerprint:
        return dict(mode='fresh', reason='context_hash_mismatch'), None
    import time
    try:
        # prepare deletes nothing here; compaction removes this file, ending the context generation
        delivered = protocol.load_state_json(handoff_identity.delivery_receipt_path(root, session), 65536)
        live = (delivered.get('token') == reuse['receipt'] and delivered.get('path') == path
                and delivered.get('fingerprint') == fingerprint and 0 <= time.time() - float(delivered['created']) < protocol.REPLAY_TTL)
    except (OSError, ValueError, KeyError, TypeError):
        live = False
    if not live:
        return dict(mode='fresh', reason='receipt_not_live'), None
    return dict(mode='reuse', reason='receipt_live'), reuse['receipt']


def workspace_state(root):
    snapshot = workspace.snapshot(root)
    if snapshot is None:
        return dict(kind='workspace', git=False)
    head, status = snapshot
    return dict(kind='workspace', git=True, head=head,
                status=hashlib.sha256(status.encode()).hexdigest()[:16])


def recheck_state(root, authorization, holds):
    recheck = [dict(kind='authorization', source=authorization),
               dict(kind='holds', scopes=sorted(h['scope'] + '|' + str(h.get('created')) for h in holds)),
               workspace_state(root)]
    return recheck, hashlib.sha256(json.dumps(recheck, sort_keys=True).encode()).hexdigest()[:32]


def save_request_id(root, session, topic):
    """Stable across retries of one save; changes once that save lands for this session."""
    generation = sum(1 for r in ledger._records(root) if r['fm'].get('session_id') == session)
    key = '\0'.join((os.path.realpath(root), session, topic or '', str(generation)))
    return 'dispatch-' + hashlib.sha256(key.encode()).hexdigest()[:24]


def _stop(request, decision, **extra):
    outcome, code, text = decision[:3]
    kind = {'ask': 'ask', 'none': 'continue', 'proceed': 'run'}.get(outcome, 'report')
    return result(outcome, code, request, retryable=bool(decision[3:] and decision[3]),
                  next_action=dict(kind=kind, command='', text=text), **extra)


DEFINITE = ('restart_unsupported', 'hold', 'dispatch_pending', 'dispatch_refused', 'dispatch_retrieved',
            'delegation_unobserved', 'worker_unregistered', 'worker_inventory_invalid')


def delegated_items(path):
    if not path or not os.path.isfile(path):
        return []
    try:
        checkpoint = Record.load(path)
        if not checkpoint.trusted():
            return []
        items = checkpoint.continuity
    except (OSError, ValueError):
        return []
    return [i for i in items if isinstance(i, dict) and i.get('status') == 'delegated']


def restart_eligibility(host, holds, pending, checkpoint):
    """true/false/unknown from host-supplied evidence; absent evidence is never assumed."""
    blockers = []
    capable = host.get('restart_capable')
    if capable is None:
        blockers.append('restart_capability_unknown')
    elif capable is False:
        blockers.append('restart_unsupported')
    if holds:
        blockers.append('hold')
    if pending:
        blockers.append({'pending': 'dispatch_pending', 'refused': 'dispatch_refused'}.get(pending['state'], 'dispatch_retrieved'))
    delegated = delegated_items(checkpoint)
    workers = host.get('workers')
    if workers is None:
        blockers.append('worker_inventory_unknown')
    elif not isinstance(workers, list) or not all(isinstance(w, dict) and isinstance(w.get('id'), str) and w['id'] for w in workers):
        blockers.append('worker_inventory_invalid')
    else:
        observed = {w['id'] for w in workers} | {w['name'] for w in workers if isinstance(w.get('name'), str)}
        owners = {i['owner'] for i in delegated}
        blockers += ['delegation_unobserved:' + i['id'] for i in delegated if i['owner'] not in observed]
        blockers += ['worker_unregistered:' + w['id'] for w in workers
                     if w['id'] not in owners and w.get('name') not in owners]
    definite = any(b.split(':')[0] in DEFINITE for b in blockers)
    return dict(eligible=False if definite else 'unknown' if blockers else True, blockers=blockers,
                host_confirmed=False, delegated=[dict(id=i['id'], owner=i['owner']) for i in delegated],
                evidence='caller-supplied host observations; this kit cannot observe the host itself')


def route(request, root):
    operation = request['_operation']
    root = handoff_identity.project_root(root)
    host = request.get('host') or {}
    if not isinstance(host, dict) or set(host) - HOST_KEYS:
        return blocked(request, 'invalid_request', 'host accepts only: %s.' % ', '.join(sorted(HOST_KEYS)))
    identity, error = resolve_identity(root, request)
    if error:
        return blocked(request, 'invalid_request', error)
    request['_identity'] = identity
    session = identity['session']
    if identity['conflict']:
        return blocked(request, 'identity_conflict', 'The supplied session differs from the authoritative %s identity. '
                       'Never borrow another identity; drop the session field or fix the caller.' % identity['source'])
    if not session:
        return blocked(request, 'identity_required', 'No usable session identity: %s needs a hook-recorded, host or '
                       'explicit session. Ask the user or pass this session\'s actual id.' % operation)
    if operation == 'autoresume' and identity['source'] not in ('hook', 'host'):
        return blocked(request, 'identity_required', 'Automatic resume needs a hook-recorded or host identity.')
    if request.get('override') is not None:
        decision, command = override_action(root, request, session)
        if decision:
            return _stop(request, decision)
        return result('proceed', 'recovery', request, next_action=dict(
            kind='run', command=command, text='Explicit recovery only; it does not authorize the checkpoint next steps.'))
    saving = operation in ('save', 'pressure')
    if saving:
        target, decision = save_target(root, request, session)
    else:
        target, decision = select_target(root, intent_target_request(root, request, host), session)
        if target and target.get('path') and os.path.isfile(target['path']):
            target['fingerprint'] = protocol.snapshot(target['path'])
    if decision and decision[1] != 'history_only':
        if decision[1] in ('publication_incomplete', 'publication_modified'):
            recovery = target['recovery'][0]
            return result('blocked', decision[1], request, target=target, next_action=dict(
                              kind='ask' if recovery['kind'] in ('abandon', 'modified') else 'run',
                              command=recovery['command'], text=recovery['text']))
        return _stop(request, decision, target=target)
    try:
        holds = read_holds(root)
    except OSError as exc:
        return blocked(request, 'hold_state_unreadable', 'Holds are unreadable (%s); restore access. Unknown holds block execution.' % exc)
    topic = (target or {}).get('topic') or (ledger._topic_for_path(target['path']) if target and target.get('path') else None)
    active = applicable_holds(holds, topic)
    if host.get('hold') is True:
        active.append(dict(scope='host', reason='host reported a hold', created=None, source='host'))
    if target and target.get('path') and operation == 'autoresume':
        if Record.load(target['path']).held:
            active.append(dict(scope='checkpoint', reason='checkpoint is waiting for input or paused', created=None, source='checkpoint'))
    authorized = request.get('_authorization')
    recheck, state = recheck_state(root, authorized, holds)
    common = dict(target=target, holds=active, recheck=recheck, state=state)
    expect = request.get('expect')
    if expect is not None:
        if not isinstance(expect, dict) or not isinstance(expect.get('state'), str):
            return blocked(request, 'invalid_request', 'expect must be {"state": <token from a previous dispatch>}.')
        if expect['state'] != state:
            return blocked(request, 'state_changed', 'Authorization, holds or workspace changed since the last dispatch; '
                           'dispatch again and act only on the new next_action.', **common)
    checkpoint = (target or {}).get('predecessor') if saving else (target or {}).get('path')
    pending, failure, warnings = pending_dispatch(root, host, session, checkpoint, saving)
    if warnings:
        common['warnings'] = warnings
    if failure:
        return _stop(request, failure, **common)
    common['restart'] = restart_eligibility(host, active, pending, checkpoint)
    if saving and pending and pending['session'] == session:
        return blocked(request, 'dispatch_pending', 'A restart intent for this session is still pending. Reconcile with '
                       'the host before any retry; never re-dispatch or record host confirmation yourself.', **common)
    if pending and not saving and stale_marked(pending):
        return blocked(request, 'dispatch_stale', stale_text(pending), **common)
    if saving and request.get('draft') is not None:
        return draft_save(root, request, session, target, common)
    if decision:  # history_only
        return result('proceed', 'history_only', request, next_action=dict(
            kind='run', command=_command('history', root, '--topic', target['topic']), text=decision[2]), **common)
    if saving:
        request_id = request.get('request_id') or save_request_id(root, session, target.get('topic'))
        command = _command('save', root, '--session', session, '--request-id', request_id)
        text = 'Pipe the checkpoint JSON to this command; reuse this request ID on retry.' + (
            ' Set "predecessor": %s in the draft.' % json.dumps(target['predecessor']) if target['predecessor'] else '')
        if operation == 'pressure':
            text = ('Start no new work. Finish only the action already in flight; record any unfinished one under '
                    '## Pending operations. ') + text + ' Then stop.'
        return result('proceed', 'ok', request, request_id=request_id, next_action=dict(kind='run', command=command, text=text), **common)
    execute = operation in ('resume', 'autoresume') and bool(authorized)
    if execute and pending:
        # The successor is proven by the environment's terminal, never by a target path or stdin
        terminal = protocol.terminal_id()
        if (pending['session'] == session or pending['path'] != os.path.realpath(target['path'])
                or not terminal or pending['terminal'] != terminal):
            return blocked(request, 'dispatch_pending', 'Restart intent %s (session %s, checkpoint %s) is pending. '
                           'The requesting session must not resume it; only its successor on terminal %s does, after '
                           'the successor step in reference.md ## AgentsRoom restart. Reconcile with the host; never '
                           're-dispatch or record host confirmation yourself.'
                           % (pending['request_id'], pending['session'], pending['path'], pending['terminal']), **common)
        if not active:
            return result('proceed', 'restart_successor', request, authorized_to_execute=False,
                          authorization_source=authorized, next_action=successor_action(root, pending, session), **common)
    if execute and active:
        hold = active[0]
        how = ('run ' + _command('release', '--scope', hold['scope'], '--root', hold.get('root', root)) if hold['source'] == 'explicit'
               else 'ask the %s to lift it' % hold['source'])
        return result('blocked', 'held', request, next_action=dict(kind='ask', command='', text=(
            'Held (%s: %s). Only the user releases a hold; ask them, then %s.' % (hold['scope'], hold['reason'], how))), **common)
    delivery, receipt = delivery_mode(root, session, target['path'], target.get('fingerprint'), request.get('reuse'))
    args = ['resume' if execute else 'prepare', target['path'], '--root', root, '--session', session]
    if receipt:
        args += ['--reuse-receipt', receipt]
    if execute:  # bounded like the hook fast path; required delivery context is always kept
        args += ['--fields', 'path,body']
    return result('proceed', 'ok', request, authorized_to_execute=execute, authorization_source=authorized, delivery=delivery,
                  next_action=dict(kind='run', command=_command(*args), text=(
                      'Run it exactly as given, with no head, pipe or redirect: its output is the complete '
                      'required package. It claims, verifies and acknowledges; then continue with ## Next steps.' if execute else
                      'Retrieve only; do not execute next steps until the user authorizes continuation.')), **common)


def save_target(root, request, session):
    target = dict(predecessor=None)
    topic = request.get('topic')
    if topic is None:
        return target, None
    if not isinstance(topic, str) or not ledger.TOPIC_RE.fullmatch(topic):
        return None, ('blocked', 'invalid_request', 'topic must be kebab-case.')
    resolved = ledger.resolve(topic, root)
    heads = [c for c in resolved['chain'] if c['status'] == 'open']
    if len(heads) > 1:
        return dict(topic=topic, candidates=[c['path'] for c in heads]), (
            'ask', 'topic_ambiguous', 'Topic %r has %d open checkpoints; ask the user which one this save continues.' % (topic, len(heads)))
    target.update(topic=topic, predecessor=resolved['authoritative'])
    return target, None


def cli(argv):
    cmd, args = argv[0], argv[1:]
    if cmd == 'dispatch':
        import argparse
        p = argparse.ArgumentParser(prog='handoff_ledger.py dispatch')
        p.add_argument('--input', default='-')
        p.add_argument('--root', default='.')
        p.add_argument('--pretty', action='store_true', help='Indent JSON output')
        a = p.parse_args(args)
        try:
            raw = protocol.read_cli_input(a.input)
            request = json.loads(raw)
        except (OSError, ValueError) as exc:
            emit_output(blocked({}, 'invalid_request', 'Unreadable request: %s.' % exc))
            return 1
        out = dispatch(request, a.root)
        emit_output(out, pretty=a.pretty)
        return exit_status('dispatch', out['outcome'])
    if cmd in ('hold', 'release'):
        import argparse
        p = argparse.ArgumentParser(prog='handoff_ledger.py ' + cmd)
        p.add_argument('--scope', required=True, help='project | topic:<topic> | terminal:<id>')
        p.add_argument('--root', default='.')
        p.add_argument('--pretty', action='store_true', help='Indent JSON output')
        if cmd == 'hold':
            p.add_argument('--reason', required=True)
        a = p.parse_args(args)
        reason = getattr(a, 'reason', None)
        if not SCOPE.fullmatch(a.scope) or (cmd == 'hold' and not reason.strip()):
            emit_output(dict(outcome='blocked', code='invalid_request', retryable=False,
                                  action='Use --scope project|topic:<topic>|terminal:<id> and a nonempty --reason.'))
            return 1
        emit_output(set_hold(handoff_identity.project_root(a.root), a.scope, reason and reason.strip()[:300]), pretty=a.pretty)
        return 0
    print('unknown subcommand: %s' % cmd, file=sys.stderr)
    return 1
