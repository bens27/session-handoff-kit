#!/usr/bin/env python3
"""handoff-staleness: is a parked checkpoint still the work to resume?

classify(root, fm, lookup) compares the checkpoint's `ticket:` / `base:` header
with the `Ticket:` trailers on origin/main and the ticket's backlog state, and
answers one verdict:

  conflicting   base is gone or not on origin/main, or the ticket no longer exists
  superseded    the ticket is closed (done or won't fix)
  progressed    origin/main carries commits for this ticket since base
  fresh         nothing for this ticket landed since base
  unclassified  not enough metadata (or no origin/main) to say

Precedence runs top to bottom. `lookup(ticket)` answers 'open' | 'closed' |
'missing' or raises; any failure there turns the verdict git-only and is
recorded in `backlog`, never raised. origin/main is the local remote-tracking
ref: no fetch happens here.

Stdlib only.
"""

import handoff_contract as contract
import handoff_workspace as workspace

ORIGIN_MAIN = 'refs/remotes/origin/main'
BACKLOG_STATES = ('open', 'closed', 'missing')


class BacklogUnavailable(Exception):
    """The backlog could not be asked; the message says why, without secrets."""


def _plural(n, word):
    return '%d %s%s' % (n, word, '' if n == 1 else 's')


def ticket_matches(ticket, value):
    """A trailer names the ticket when either id is a prefix of the other (8-hex short id vs UUID)."""
    a, b = ticket.strip().lower(), value.strip().lower()
    return bool(a and b) and (a.startswith(b) or b.startswith(a))


def _ask(lookup, ticket):
    """(state, None) from the backlog, or (None, why) when it cannot answer."""
    try:
        state = lookup(ticket)
    except BacklogUnavailable as exc:
        return None, str(exc) or 'backlog unavailable'
    except Exception as exc:  # a broken client must never break a resume
        return None, 'backlog lookup failed (%s)' % type(exc).__name__
    if state not in BACKLOG_STATES:
        return None, 'backlog answered an unknown ticket state'
    return state, None


def classify(root, fm, lookup):
    meta = contract.checkpoint_metadata(fm or {})
    ticket, base = meta.get('ticket', ''), meta.get('base', '')
    out = dict(verdict='unclassified', reason='', ticket=ticket, base=base, origin_main='')
    if not ticket and not base:
        out.update(source='none', reason='checkpoint records no ticket or base')
        return out

    state, why = _ask(lookup, ticket) if ticket else (None, 'no ticket recorded in the checkpoint')
    out['source'] = 'git+backlog' if state else 'git-only'
    if not state:
        out['backlog'] = why

    def done(verdict, reason):
        out.update(verdict=verdict, reason=reason)
        return out

    origin = workspace.resolve_commit(root, ORIGIN_MAIN)
    out['origin_main'] = origin
    base_sha = ''
    if base:
        base_sha = workspace.resolve_commit(root, base)
        if not base_sha:
            return done('conflicting', 'base %s is not a commit in this repository' % base[:12])
        if origin and not workspace.is_ancestor(root, base_sha, origin):
            return done('conflicting', 'base %s is not on origin/main' % base[:12])
    if state == 'missing':
        return done('conflicting', 'ticket %s is not in the backlog' % ticket)
    if state == 'closed':
        return done('superseded', 'ticket %s is closed in the backlog' % ticket)

    if not base_sha:
        if state == 'open':
            return done('fresh', 'ticket %s is open; no base recorded to compare' % ticket)
        return done('unclassified', 'no base recorded and the backlog did not answer')
    if not origin:
        return done('unclassified', 'origin/main is not available locally to compare base %s' % base[:12])

    commits = workspace.commits_between(root, base_sha, origin)
    if commits is None:
        return done('unclassified', 'could not list origin/main commits since base %s' % base[:12])
    mine = [sha for sha, values in commits if ticket and any(ticket_matches(ticket, v) for v in values)]
    if mine:
        shown = ', '.join(sha[:7] for sha in mine[:5]) + (', ...' if len(mine) > 5 else '')
        return done('progressed', 'origin/main has %s for Ticket %s since base: %s'
                    % (_plural(len(mine), 'commit'), ticket, shown))
    if not commits:
        return done('fresh', 'origin/main has not moved since base %s' % base[:12])
    return done('fresh', 'origin/main moved %s since base %s, none for this ticket'
                % (_plural(len(commits), 'commit'), base[:12]))
