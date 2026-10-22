#!/usr/bin/env python3
"""handoff-holds: durable dispatcher holds (project, topic and terminal scopes).

Holds gate lifecycle execution and restarts. The dispatcher sets and reports them;
restart preflight and adopt read them. Shared-main holds apply to linked worktrees.
Stdlib only.
"""
import hashlib
import json
import os
import re
import time

import handoff_identity
import handoff_protocol as protocol
import handoff_workspace as workspace

SCOPE = re.compile(r'project|topic:[a-z0-9]+(?:-[a-z0-9]+)*|terminal:[\w.:-]{1,120}')


def _holds_dir(root):
    return os.path.join(root, '.handoffs', 'holds')


def _hold_roots(root):
    """Main controls are shared; worker controls are additional, never overrides."""
    local = handoff_identity.project_root(root)
    worktree = handoff_identity.linked_worktree_root(local)
    if not worktree:
        return [local]
    gitdir, common = workspace.linked_common(worktree)
    if common is None:
        return [local]
    if handoff_identity.same_dir(common, gitdir):
        return [local]
    main = os.path.dirname(common)
    return list(dict.fromkeys([os.path.realpath(main), os.path.realpath(local)]))


def read_holds(root):
    """Union of shared-main and local holds; unreadable/corrupt state fails closed."""
    holds, seen = [], set()
    for origin in _hold_roots(root):
        try:
            names = sorted(os.listdir(_holds_dir(origin)))
        except FileNotFoundError:
            continue
        for name in names:
            if not name.endswith('.json'):
                continue
            try:
                record = protocol.load_state_json(os.path.join(_holds_dir(origin), name), 8192)
                if not (isinstance(record.get('scope'), str) and SCOPE.fullmatch(record['scope'])):
                    raise ValueError
            except ValueError:
                record = dict(scope='project', reason='unreadable hold record %s; inspect it before releasing' % name)
            hold = dict(scope=record['scope'], reason=str(record.get('reason', ''))[:300],
                        created=record.get('created'), source='explicit')
            identity = json.dumps(hold, sort_keys=True)
            if identity not in seen:
                seen.add(identity)
                holds.append(dict(hold, root=origin))
    return holds


def applicable_holds(holds, topic):
    terminal = protocol.terminal_id()
    return [h for h in holds if h['scope'] == 'project' or (topic and h['scope'] == 'topic:' + topic)
            or (terminal and h['scope'] == 'terminal:' + terminal)]


def set_hold(root, scope, reason=None):
    path = os.path.join(_holds_dir(root), hashlib.sha256(scope.encode()).hexdigest()[:24] + '.json')
    if reason is None:
        try:
            os.unlink(path)
        except FileNotFoundError:
            return dict(outcome='none', code='no_hold', scope=scope, action='No hold for %s; nothing to release.' % scope)
        return dict(outcome='released', code='released', scope=scope, action='Released %s. Re-dispatch before acting.' % scope)
    os.makedirs(_holds_dir(root), exist_ok=True)
    protocol.write_json(path, dict(scope=scope, reason=reason, created=time.time(), terminal=protocol.terminal_id()))
    return dict(outcome='held', code='held', scope=scope, path=path,
                action='Held %s. Lifecycle execution and restarts stay blocked until an explicit release.' % scope)
