#!/usr/bin/env python3
"""handoff-autoclose: retire open checkpoints whose work has landed (ticket #162).

A choose/listing (protocol.lookup with auto_close=True) calls sweep() once. An
open checkpoint is retired when ALL of these hold:

  - its header names a ticket and its save recorded no uncommitted work
    (workspace_changes_hash is the hash of an empty change set);
  - backlog_get answers status `done` without the wontFix flag;
  - a `git fetch origin` succeeds and refs/remotes/origin/main resolves
    (asked only after some ticket reads done, so idle listings stay offline);
  - every commit the header names (the sha of `git:` and `workspace_git:`,
    plus `base:` when present) resolves and is an ancestor of origin/main.

Anything else (backlog unreachable, ticket unknown, won't-fix or cancelled,
fetch failure, a commit that is unresolvable or not landed, dirty save) leaves
the checkpoint open: missing evidence never closes work. Retiring appends a
sealed line to the notes sidecar (handoff_notes.retire); the checkpoint file
is never written. Never called from resume.
"""
import hashlib
import os
import re
import subprocess

import handoff_contract as contract
import handoff_ledger as ledger
import handoff_mcp as mcp
import handoff_notes as notes
import handoff_staleness as staleness
import handoff_workspace as workspace

ORIGIN_MAIN = staleness.ORIGIN_MAIN
EMPTY_CHANGES = hashlib.sha256(b'').hexdigest()
FETCH_TIMEOUT = 10
SHA_RE = re.compile(r'[0-9a-f]{7,64}')


def named_commits(fm):
    """The shas the header names, or None when a present field is malformed."""
    out = []
    for key in ('git', 'workspace_git'):
        value = fm.get(key) or ''
        if not value:
            continue
        sha = value.rsplit('@', 1)[-1]
        if '@' not in value or not SHA_RE.fullmatch(sha):
            return None
        out.append(sha)
    base = contract.checkpoint_metadata(fm).get('base')
    if fm.get('base') and not base:
        return None
    if base:
        out.append(base)
    return list(dict.fromkeys(out))


def candidates(root):
    """Open, clean-saved checkpoints with a ticket and at least one named commit."""
    out = []
    for r in ledger._records(root):
        fm = r['fm']
        if r['problems'] or not fm.get('checkpoint_id') or fm.get('status') != 'open':
            continue
        if fm.get('workspace_changes_hash') != EMPTY_CHANGES:
            continue
        ticket = contract.checkpoint_metadata(fm).get('ticket')
        commits = named_commits(fm)
        if ticket and commits:
            out.append(dict(path=r['path'], checkpoint_id=fm['checkpoint_id'], ticket=ticket, commits=commits))
    return out


def fetch(root):
    """`git fetch origin` with a short timeout; False on any failure."""
    env = dict(os.environ, GIT_TERMINAL_PROMPT='0')
    try:
        return subprocess.run(['git', '-C', root, 'fetch', '--quiet', '--no-tags', 'origin'],
                              capture_output=True, timeout=FETCH_TIMEOUT, env=env).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _landed(root, commits, origin):
    full = [workspace.resolve_commit(root, sha) for sha in commits]
    if not all(full) or not all(workspace.is_ancestor(root, sha, origin) for sha in full):
        return None
    return full


def _done_candidates(pending, lookup):
    """The candidates whose ticket reads done (not won't-fix), each with its status.
    One BacklogUnavailable answer stops the asking: nothing is retired."""
    statuses, done = {}, []
    for c in pending:
        if c['ticket'] not in statuses:
            try:
                statuses[c['ticket']] = lookup(c['ticket'])
            except staleness.BacklogUnavailable:
                return []
        found = statuses[c['ticket']]
        if found and found['status'] == 'done' and not found['wont_fix']:
            done.append((c, found))
    return done


def sweep(root, status_lookup=None, fetcher=fetch):
    """Retire every eligible checkpoint; returns [{path, ticket}] newly retired.
    The backlog is asked first, so a fetch happens only when some ticket is done."""
    pending = candidates(root)
    if not pending:
        return []
    done = _done_candidates(pending, status_lookup or mcp.status_lookup(root))
    if not done or not fetcher(root):
        return []
    origin = workspace.resolve_commit(root, ORIGIN_MAIN)
    if not origin:
        return []
    retired = []
    for c, found in done:
        full = _landed(root, c['commits'], origin)
        if not full:
            continue
        evidence = dict(ticket=c['ticket'], status=found['status'], commits=full, origin_main=origin)
        if notes.retire(c['path'], c['checkpoint_id'], evidence, ledger.locked) == 'retired':
            retired.append(dict(path=c['path'], ticket=c['ticket']))
    return retired
