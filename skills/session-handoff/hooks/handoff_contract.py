"""Public output and continuation contracts, independent of lifecycle state."""

import json
import os
import re
import shlex


# The successor prompt restart_preflight hands to agents_restart. context_watch
# recognizes it at the start of a prompt (case and leading space ignored) to
# record the successor session before its transcript exists.
RESTART_PROMPT_PREFIX = 'Continue the handoff: run exactly '
RESTART_PROMPT_RE = re.compile(r'\s*' + re.escape(RESTART_PROMPT_PREFIX), re.I)

DELIVERY_FIELDS = frozenset({
    'path', 'body', 'references', 'skills', 'next_step_section', 'verification',
    'verify_required', 'trusted', 'workspace', 'optional_references',
    'withheld_references', 'delivery_receipt', 'reused_from', 'predecessor',
    'continuity', 'next_step', 'reviewed', 'review',
})
# Small orientation keys first; large content last so a clipped read keeps the next step
LEADING = ('outcome', 'action', 'next_step', 'stage', 'path', 'next_step_section')
TRAILING = ('references', 'skills', 'body')
DIAGNOSTIC_FIELDS = frozenset({
    'outcome', 'action', 'stage', 'warnings', 'errors', 'problems',
    'withheld_references', 'exit_code', 'log', 'excerpt', 'verify',
    'baseline_failures', 'new_failures', 'fixed', 'candidates', 'holder',
})


def project_output(result, fields=None):
    """Select optional output without discarding a successful transfer's context."""
    if fields:
        keep = DIAGNOSTIC_FIELDS | {field.strip() for field in fields.split(',')}
        if result.get('outcome') in ('prepared', 'retrieved', 'resumed'):
            keep |= DELIVERY_FIELDS
        result = {key: value for key, value in result.items() if key in keep}
    order = {key: i for i, key in enumerate(LEADING)}
    order.update((key, len(LEADING) + 1 + i) for i, key in enumerate(TRAILING))
    return dict(sorted(result.items(), key=lambda item: order.get(item[0], len(LEADING))))


# Profiles preserve the distinct, already published CLI wire formats. In
# particular, non-delivery preflight output retains raw ordering. Adopt applies
# project_output first, retaining raw escaping but leading with continuation text.
_OUTPUT_PROFILES = {
    'raw': dict(ensure_ascii=True, sort_keys=False),
    'protocol': dict(ensure_ascii=False, sort_keys=False),
    'lane': dict(ensure_ascii=True, sort_keys=True),
}


def render_output(value, *, profile='raw', fields=None, pretty=False):
    """Encode one public value without a newline or changing the input object."""
    options = _OUTPUT_PROFILES[profile]
    if profile == 'protocol':
        value = project_output(value, fields)
    elif fields is not None:
        raise ValueError('Field projection requires the protocol profile.')
    return json.dumps(value, indent=2 if pretty else None, **options)


def emit_output(value, *, profile='raw', fields=None, pretty=False):
    """Write one complete JSON line; delivery acknowledgement stays with callers."""
    print(render_output(value, profile=profile, fields=fields, pretty=pretty))


def python_command(script, *args):
    """Quote complete argv; callers retain ownership of installation selection."""
    return ' '.join(shlex.quote(str(arg)) for arg in ('python3', script) + args)


def ledger_command(hooks_dir, *args):
    return python_command(os.path.join(hooks_dir, 'handoff_ledger.py'), *args)


def continuation_command(hooks_dir, operation, path, session, fields=None):
    args = (operation, path, '--session', session)
    if fields is not None:
        args += ('--fields', fields)
    return ledger_command(hooks_dir, *args)


def adopt_command(script, root, request_id):
    return python_command(os.path.realpath(script), 'adopt', '--root',
                          os.path.realpath(root), '--request-id', request_id)


def handle_command(script, handle):
    # Handles have already been validated/published by the intent store. Keep
    # its historical literal suffix instead of changing how callers quote it.
    return python_command(os.path.realpath(script), 'adopt') + ' ' + handle


def command_template(script, args, suffix):
    """Append a literal instructional fragment, never treat it as executable argv.

    Placeholders such as <draft.json> and [--hold] must stay exactly as printed.
    Only code-authored suffixes belong here; dynamic values belong in args.
    """
    return python_command(script, *args) + ' ' + suffix


def continuity_items(value):
    """Validate a register; each stable ID has an explicit disposition."""
    if not isinstance(value, list):
        raise ValueError('continuity must be a list of items with id, text and status.')
    seen = set()
    for item in value:
        if not isinstance(item, dict) or set(item) - {'id', 'text', 'status', 'owner', 'evidence'}:
            raise ValueError('Each continuity item accepts only id, text, status, owner and evidence.')
        if any(not isinstance(item.get(key), str) or not item[key].strip()
               for key in ('id', 'text', 'status')):
            raise ValueError('Each continuity item needs nonempty id, text and status strings.')
        if item['id'] in seen:
            raise ValueError('Duplicate continuity item ID: ' + item['id'])
        seen.add(item['id'])
        if item['status'] not in ('open', 'delegated', 'resolved', 'retired'):
            raise ValueError('Continuity status must be open, delegated, resolved or retired.')
        for key in ('owner', 'evidence'):
            if key in item and (not isinstance(item[key], str) or not item[key].strip()):
                raise ValueError('Continuity %s must be a nonempty string.' % key)
        if item['status'] == 'delegated' and not item.get('owner'):
            raise ValueError('Delegated continuity items require an owner.')
        if item['status'] in ('resolved', 'retired') and not item.get('evidence'):
            raise ValueError('Resolved or retired continuity items require evidence or a reason.')
    return value


def merge_continuity(previous, updates):
    """Omission retains prior items; explicit updates replace the same stable ID."""
    merged = {item['id']: item for item in continuity_items(previous)}
    merged.update((item['id'], item) for item in continuity_items(updates))
    return list(merged.values())


# Checkpoint header metadata (SPEC §7.1). Each is one optional `key: value`
# line; a checkpoint without them is a legacy checkpoint, not an invalid one.
METADATA_FORMATS = {
    # AgentsRoom ticket id: full lowercase UUID, or its first 8 hex digits.
    'ticket': re.compile(r'[0-9a-f]{8}(?:-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})?'),
    # Commit the work started from. Drafts may abbreviate (7+); headers hold the full sha.
    'base': re.compile(r'[0-9a-f]{7,64}'),
    'role': re.compile(r'[a-z][a-z0-9]*(?:-[a-z0-9]+)*'),
}
METADATA_HINTS = {
    'ticket': 'a lowercase ticket UUID or its 8-hex short id, for example 6cf33871',
    'base': 'an existing commit sha of this project (7 to 64 hex digits)',
    'role': 'a kebab-case role of at most 40 characters, for example backend',
}
METADATA_INHERITED = ('ticket', 'role')  # base is a fact of one save, never inherited


def metadata_problem(key, value):
    """Why a draft's metadata value is malformed, or '' when it is acceptable ('' clears)."""
    if value == '':
        return ''
    if not (isinstance(value, str) and METADATA_FORMATS[key].fullmatch(value)
            and (key != 'role' or len(value) <= 40)):
        return 'Invalid %s %s: supply %s, or omit it.' % (key, json.dumps(value)[:80], METADATA_HINTS[key])
    return ''


def checkpoint_metadata(fm):
    """The well-formed ticket/base/role of a parsed header; absent or malformed values are omitted."""
    out = {}
    for key, pattern in METADATA_FORMATS.items():
        value = fm.get(key, '')
        if value and pattern.fullmatch(value) and (key != 'base' or len(value) in (40, 64)) \
                and (key != 'role' or len(value) <= 40):
            out[key] = value
    return out


def continuation_document(document, previous=None):
    """Carry trusted metadata independently of the author's next-action summary.

    The caller decides predecessor trust. Explicit dependency lists replace the
    old next-action dependencies; register entries require an explicit update.
    """
    previous = previous or {}
    effective = dict(document)
    for key in ('skills', 'references', 'optional_references'):
        if key not in effective:
            effective[key] = previous.get(key, '')
    # Inherited metadata passes through the reader: a malformed stored value is dropped, not copied.
    inherited = checkpoint_metadata(previous)
    for key in METADATA_INHERITED:
        if key not in effective:
            effective[key] = inherited.get(key, '')
    effective['continuity'] = merge_continuity(
        json.loads(previous.get('continuity', '[]')), document.get('continuity', []))
    return effective


DISPATCH_BRANCHES = {'save': 'branches/save.md', 'pressure': 'branches/save.md', 'retrieve': 'branches/resume.md',
          'resume': 'branches/resume.md', 'autoresume': 'branches/resume.md'}
# code -> retryable. Only transient read/state failures retry, and only the same request.
DISPATCH_CODES = {
    'ok': False, 'recovery': False, 'history_only': False, 'held': False, 'released': False,
    'no_operation': False, 'not_lifecycle': False, 'lifecycle_unrecognised': False,
    'no_open_handoff': False, 'no_hold': False,
    'claimed_elsewhere': False, 'stale_only': False, 'foreign_only': False, 'target_ambiguous': False,
    'topic_ambiguous': False, 'confirmation_required': False, 'invalid_request': False,
    'invalid_authorization': False, 'invalid_override': False, 'autoresume_not_enabled': False,
    'identity_conflict': False, 'identity_required': False, 'target_not_found': False,
    'target_superseded': False, 'target_closed': False, 'dispatch_pending': False,
    'dispatch_state_invalid': False, 'restart_successor': False, 'lookup_error': True, 'publication_incomplete': False,
    'hold_state_unreadable': True, 'state_changed': True,
    'saved': False, 'restart': False, 'restart_blocked': False, 'publication_failed': False, 'dispatch_stale': False,
}
DISPATCH_RETRY = dict(max_attempts=3, backoff_seconds=[1, 4, 16], same_request=True)


def dispatch_result(outcome, code, request, **extra):
    operation = request.get('_operation')
    # Report the validated source on every outcome; only a proceed path may set authorized_to_execute.
    base = dict(outcome=outcome, code=code, retryable=False, operation=operation,
                operation_source=request.get('_operation_source'), classification=request.get('_classification'),
                authorized_to_execute=False, authorization_source=request.get('_authorization'),
                identity=request.get('_identity') or dict(session='', source='none', provenance='', conflict=None),
                branch=DISPATCH_BRANCHES.get(operation), restart=dict(eligible='unknown', blockers=[]),
                next_action=dict(kind='continue', command='', text='Continue the current request.'), recheck=[])
    base.update(extra)
    base['retryable'] = DISPATCH_CODES[code]
    if base['retryable']:
        base['retry'] = dict(DISPATCH_RETRY)
    return base


def blocked_result(request, code, text, retryable=False, **extra):
    return dispatch_result('blocked', code, request, retryable=retryable,
                  next_action=dict(kind='report', command='', text=text), **extra)


_EXIT_FAILURES = {
    'protocol': frozenset(('error', 'blocked', 'invalid', 'conflict', 'needs-context',
                          'needs-original-draft', 'verification-failed',
                          'verification-timeout', 'timeout', 'needs-confirmation')),
    'report': frozenset(('unavailable',)),
    'dispatch': frozenset(('blocked',)),
}


def exit_status(surface, outcome, operation=None):
    """Published exit policies differ by CLI and operation; do not unify them."""
    if surface == 'preflight':
        expected = {'adopt': 'resumed', 'restart': 'ready'}.get(operation)
        return int(expected is not None and outcome != expected)
    if surface == 'protocol' and operation == 'save' and outcome == 'incomplete':
        return 1
    return int(outcome in _EXIT_FAILURES[surface])
