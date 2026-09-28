# AgentsRoom MCP verification — 2026-09-28

Question: is the proposed AgentsRoom fix necessary to resolve handoff waste?

Initial conclusion: not established as broadly proposed. Existing transfer choices offer
an immediate alternative to raw replay. An independent stale skill-library copy
is a confirmed rollout gap. Profile hook registration is a separate integration
question, not a fix for the host's transfer workflow.

## Evidence from read-only AgentsRoom tools

- `capabilities_get(overview)`: app/server 1.193.0; no degraded modules.
- `product_help_get(context-drift-detection)`: Restart agent opens Context transfer
  with Agent summary, Raw transcript and Light context. Agent summary displays
  progress and can be cancelled. The feature lists no global setting.
- `product_help_get(multi-provider)`: same-provider live model changes can retain
  the session; other changes restart with transferred context. Provider-wide CLI
  options exist, and per-agent flags may replace them.
- `product_help_get(codex-data-storage)`: Codex homes are generated per project
  and account, consistent with the filesystem audit. This does not prove which
  hook definitions a running Codex process has discovered/trusted.
- `settings_list(global/project)` and `settings_get(providerCliOptions)`: no
  exposed transfer-mode setting found; the global Codex flags contain skill
  disable entries, not a Session Handoff hook registration. No settings changed.
- `skills_list` / `skills_get(session-handoff)`: account-wide library entry
  `zzaq83gtmuhdpr5w`, source `claude-import`, updated 2026-09-25T19:54:01.796Z,
  7,548-character body, six bundled files using `scripts/`, `assets/` and
  `references/`. The instructions still prescribe manual checkpoint writing
  and legacy resume/supersede commands. This is distinct from the shared v0.13.0
  filesystem installation.
- `settings_list(project)` / `agents_list` / `settings_list(folder)`: no project
  baseline, no default skill IDs on the three saved agents, and this project is
  not filed in a folder. This limits the claim: the old library copy is available,
  but these results do not prove it was injected into the sampled sessions.
- `product_help_get(skills-library)`: library imports/exports and Git-source
  fetch/save create/update copies; Git updates are explicit, never background
  sync. Attached skill bodies load at launch; resumed sessions do not replay them.

## Revised action

1. Refresh the existing AgentsRoom library entry and its bundled files from
   v0.13.0. Do not attach it everywhere as a workaround; that would add startup
   context. Validate its body, files and actual loading path in a fresh session.
2. For host-driven replacement choose Agent summary and wait for completion.
   Keep Raw transcript for cases where its extra material is intentional.
3. Verify automatic kit hook discovery/trust in the actual Codex profile before
   deciding which registration steps are needed. Missing generated-home files
   alone cannot establish the full effective runtime configuration.
4. Narrow any developer report to reproducible failure after choosing Agent
   summary: incomplete transfer, an ignored completed summary, unwanted raw
   fallback, or an empty session generating a recursive handoff. The old logs
   alone cannot distinguish those from a deliberate Raw transcript choice.

No agents were restarted/spawned, no settings/library entries were changed, and
no developer report was sent. The existing issue and rollout guide were corrected
locally; this verification is not a new release.

## Completed follow-up

The statements above record the initial read-only tool audit. Subsequent authorized
work refreshed the existing global skill through the same API used by the desktop
(the MCP save tool only supports project skills), updated its local cache, and
verified all eight files and the new body through MCP. No duplicate was created.

All 11 current AgentsRoom Codex homes now register the shared hooks. Fresh local
app-server `hooks/list` calls discover three enabled entries each, no warnings or
errors, and **untrusted** status throughout. Native user review is still required;
no security bypass or trust-database edit was used. These checks used the kit cwd.
Portskill now vendors v0.13.0; its 18 integration tests pass.

A deterministic installed-code harness now proves three narrow host defects:
partial-summary acceptance, stale-summary acceptance during pending deletion, and
producer termination after transcript write denial. Two success controls pass.
In-memory candidate changes turn all three failures green. See
[the developer report](agentsroom-transfer-issue.md) for commands, scope and repair
criteria. The app itself remains unchanged, the historical incident's exact cause
remains unproven, and no developer message was sent.
