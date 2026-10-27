"""Checkpoint review observations. No network or AgentsRoom client required."""

VERDICTS = ('accept', 'reject', 'none')


def resolve_review(*, native_review=None, tags=()):
    """Resolve an explicit native observation, otherwise exact fallback tags.

    None means the native source is unavailable; 'none' means it was read and
    has no completed verdict. Identity fields and task status are not verdicts.
    Conflicting tags do not establish a review outcome.
    """
    if native_review is not None:
        if not isinstance(native_review, str) or native_review not in VERDICTS:
            raise ValueError('native_review must be accept, reject, none, or null')
        return dict(reviewed=native_review, source='native')
    if not isinstance(tags, (list, tuple)) or not all(isinstance(tag, str) for tag in tags):
        raise ValueError('tags must be a list of strings')
    found = {tag.split(':', 1)[1] for tag in tags if tag in ('review:accept', 'review:reject')}
    if len(found) == 1:
        return dict(reviewed=found.pop(), source='tag')
    return dict(reviewed='none', source='conflicting-tags' if found else 'none')


def ticket_review(ticket=None, *, previous=None, reviewer_feedback_added=False, tags=()):
    """Read supplied backlog_get task snapshots; never contact the app.

    The caller must verify newly appended reviewer feedback before setting the
    rejection-evidence flag. A single status/submitter/reviewer is insufficient.
    With no app snapshot, only caller-supplied checkpoint/ticket tags are used.
    """
    if ticket is None:
        return resolve_review(tags=tags)
    if not isinstance(ticket, dict) or not isinstance(ticket.get('id'), str) or not ticket['id']:
        raise ValueError('ticket must be a task object with an id')
    if type(reviewer_feedback_added) is not bool:
        raise ValueError('reviewer_feedback_added must be a boolean')
    native = None
    if previous is not None:
        if not isinstance(previous, dict) or previous.get('id') != ticket['id']:
            raise ValueError('review transition must refer to the same ticket')
        if previous.get('status') == 'in_review':
            if ticket.get('status') == 'done':
                native = 'accept'
            elif ticket.get('status') in ('in_progress', 'todo') and reviewer_feedback_added:
                native = 'reject'
    return resolve_review(native_review=native, tags=ticket.get('tags', []))


def record_review(path, *, fingerprint, native_review=None, tags=(), ticket_id=None):
    """Record an advisory observation for exactly the reviewed checkpoint bytes.

    The caller supplies the fingerprint observed during review. The sidecar is
    independent of publication/claim state and never changes the header.
    Raw native_review values are caller assertions, not observed transitions.
    """
    import os
    from datetime import datetime, timezone
    from handoff_ledger import locked
    from handoff_protocol import write_json
    from handoff_record import Record, content_fingerprint

    result = resolve_review(native_review=native_review, tags=tags)
    if native_review is not None:
        result['source'] = 'caller'
    if ticket_id is not None and (not isinstance(ticket_id, str) or not ticket_id or len(ticket_id) > 200):
        raise ValueError('ticket_id must be a nonempty string of at most 200 characters')
    path = os.fspath(path)
    with locked(path):
        checkpoint = Record.load(path)
        actual = content_fingerprint(checkpoint.text)
        if fingerprint != actual:
            raise ValueError('Checkpoint changed since review; inspect it again before recording a verdict')
        if not checkpoint.fm.get('checkpoint_id') or not checkpoint.published():
            raise ValueError('Review recording requires a published checkpoint')
        result.update(checkpoint_id=checkpoint.fm['checkpoint_id'], fingerprint=actual,
                      ticket_id=ticket_id, recorded_at=datetime.now(timezone.utc).isoformat())
        write_json(path + '.review', dict(version=1, **result))
    return result


def read_review(path):
    """Read local evidence without contacting the app or changing any state.

    Missing, damaged or content-mismatched evidence cannot imply approval.
    A valid observation is historical evidence, not a fresh remote review check.
    """
    import json
    import os
    from datetime import datetime
    from handoff_record import Record, content_fingerprint

    path = os.fspath(path)
    result = dict(reviewed='none', source='none')
    try:
        checkpoint = Record.load(path)
        result.update(checkpoint_id=checkpoint.fm.get('checkpoint_id'),
                      fingerprint=content_fingerprint(checkpoint.text))
        with open(path + '.review', encoding='utf-8') as stream:
            raw = stream.read(16385)
        if len(raw) > 16384:
            return dict(result, source='invalid')
        stored = json.loads(raw)
        if (not isinstance(stored, dict) or type(stored.get('version')) is not int or stored['version'] != 1
                or stored.get('reviewed') not in VERDICTS
                or stored.get('source') not in ('caller', 'tag', 'none', 'conflicting-tags')
                or not isinstance(stored.get('recorded_at'), str)
                or (stored.get('ticket_id') is not None and (not isinstance(stored['ticket_id'], str)
                    or not 1 <= len(stored['ticket_id']) <= 200))
                or (stored.get('source') in ('none', 'conflicting-tags') and stored['reviewed'] != 'none')
                or (stored.get('source') == 'tag' and stored['reviewed'] == 'none')):
            return dict(result, source='invalid')
        if datetime.fromisoformat(stored['recorded_at']).tzinfo is None:
            return dict(result, source='invalid')
        if (not result['checkpoint_id'] or stored.get('checkpoint_id') != result['checkpoint_id']
                or stored.get('fingerprint') != result['fingerprint']):
            return dict(result, source='stale')
        return dict(result, reviewed=stored['reviewed'], source=stored['source'],
                    ticket_id=stored.get('ticket_id'), recorded_at=stored['recorded_at'])
    except FileNotFoundError:
        return result
    except (OSError, ValueError, TypeError):
        return dict(result, source='invalid')
