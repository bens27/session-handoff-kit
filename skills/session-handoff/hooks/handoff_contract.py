"""Continuation data that must survive publication and delivery adapters."""

import json


DELIVERY_FIELDS = frozenset({
    'path', 'body', 'references', 'skills', 'next_step_section', 'verification',
    'verify_required', 'trusted', 'workspace', 'optional_references',
    'withheld_references', 'delivery_receipt', 'reused_from', 'predecessor',
    'continuity',
})
DIAGNOSTIC_FIELDS = frozenset({
    'outcome', 'action', 'stage', 'warnings', 'errors', 'problems',
    'withheld_references', 'exit_code', 'log', 'excerpt', 'verify',
    'baseline_failures', 'new_failures', 'fixed', 'candidates', 'holder',
})


def project_output(result, fields=None):
    """Select optional output without discarding a successful transfer's context."""
    if not fields:
        return result
    keep = DIAGNOSTIC_FIELDS | {field.strip() for field in fields.split(',')}
    if result.get('outcome') in ('prepared', 'retrieved', 'resumed'):
        keep |= DELIVERY_FIELDS
    return {key: value for key, value in result.items() if key in keep}


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
    effective['continuity'] = merge_continuity(
        json.loads(previous.get('continuity', '[]')), document.get('continuity', []))
    return effective
