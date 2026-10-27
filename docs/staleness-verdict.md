# Resume staleness verdict

A parked checkpoint can go stale while it waits: another worker lands part of
the ticket, the ticket gets closed, or main is rewritten under its base. Every
successful `resume` (and therefore `restart_preflight.py adopt`, and a replayed
delivery) adds a `staleness` object so the successor knows before it acts:

```json
"staleness": {
  "verdict": "progressed",
  "reason": "origin/main has 1 commit for Ticket 6cf33871 since base: 3f2f41b",
  "source": "git-only",
  "backlog": "AGENTSROOM_MCP_SESSION is not set",
  "ticket": "6cf33871", "base": "9e625c0…", "origin_main": "a7c20b5…"
}
```

## Inputs

- The checkpoint header's `ticket:` and `base:` lines (SPEC §7.1, work
  metadata). Malformed values count as absent.
- `Ticket:` trailers on the commits in `base..origin/main` (the opt-in commit
  hook writes them). A trailer matches when either id is a prefix of the other,
  ignoring case, so an 8-hex short id matches the full UUID.
- The ticket's backlog state, from AgentsRoom `backlog_get`: `done` or
  `wontFix` is closed, an unknown ticket is missing, anything else is open.
- `origin/main` means the local `refs/remotes/origin/main`. Resume never
  fetches; fetch first if the verdict must reflect the remote right now.

## Verdicts

First match wins:

| Verdict | When | What the successor does |
|---|---|---|
| `conflicting` | `base` is not a commit here, `base` is not an ancestor of origin/main, or the ticket is not in the backlog | Stop and reconcile: the plan was made against history that no longer exists, or for a ticket that does not |
| `superseded` | the ticket is closed | Confirm with the user before doing more work on it |
| `progressed` | commits for this ticket landed on origin/main since `base` | Read those commits; drop the steps they already did |
| `fresh` | none of the above | Continue with Next steps |
| `unclassified` | no ticket and no base, no local origin/main to compare, or no base while the backlog is unreachable | Nothing to say; continue |

Without a `base`, an open ticket is `fresh` and a closed one `superseded`.
Without a `ticket`, only git is consulted. For the three stale verdicts the
resume `action` starts with `Staleness <verdict> (<source>): <reason>;
reconcile before continuing.`

## Git-only fallback

`source` is `git+backlog` when the backlog answered, `git-only` otherwise, with
`backlog` saying why: `AGENTSROOM_MCP_SESSION` unset, no `AgentsRoom-MCP` entry
in `.mcp.json`, app not reachable, HTTP error, or no ticket recorded. Nothing in
the check can fail a resume: any error becomes `unclassified` with the
exception type in `reason`.

## The MCP client

`hooks/handoff_mcp.py`, stdlib only:

- Reads the `AgentsRoom-MCP` URL from `.mcp.json` in the project, then in the
  main checkout when the project is a linked worktree.
- Refuses any URL that is not `http(s)` on `127.0.0.1`, `localhost` or `::1`,
  or that carries userinfo, so a repository-supplied `.mcp.json` cannot send
  the token off the machine.
- Sends `Authorization: Bearer $AGENTSROOM_MCP_SESSION` built from the
  environment; the header value in `.mcp.json` is ignored. No proxies, no
  redirects, 3-second timeout.
- Calls only tools in `READ_TOOLS` (`backlog_get`); any other name raises
  `PermissionError` before a request is sent.
- Error messages carry no headers, response bodies or token.

Tests: `tests/test_staleness.py` (verdict table against real Git repositories),
`tests/test_mcp_client.py` (client against a local fake MCP server, JSON and
SSE), and the resume cases in `tests/test_checkpoint_metadata.py`.
