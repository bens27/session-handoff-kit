# Split etiquette maintenance

The local AgentsRoom launch rewrites `~/.agentsroom/roles/_etiquette.md`.
Its on-demand sections live in `~/.agentsroom/roles/etiquette/`. After a launch
that changes the source, run from this repository:

```sh
python3 scripts/check-etiquette
```

This standalone check reads local files, prints section names and findings,
and exits 0 when they match, 1 for drift or pending policy review, or 2 for an
unrecognized source layout or invalid input. It compares text with whitespace
normalized, including punctuation and wording. It detects missing sections,
missing INDEX links, extra section files, and unmapped new source content.
It does not install a hook or change launch behavior.

To synchronize mapped sections, use `python3 scripts/check-etiquette --sync`.
This preserves existing section headings, all of INDEX, and recorded policy
deviations. A new tool family or changed source layout requires updating the
script's mapping and adding a trigger row manually; unrecognized source content
stops synchronization before writes. Review the diff before using the new rules.
For another installation, pass `--source`, `--directory`, and `--deviations`.

`docs/etiquette-deviations.json` records each intentional difference, its reason,
review status, and fingerprints of both source and split text. A pending review
always remains visible with exit 1. An approved exception passes only while both
fingerprints match; any subsequent edit requires review again. Synchronization
never overwrites a recorded exception, even when its fingerprints change.
After Ben explicitly approves keeping a deviation, set its status to `approved`.
If Ben chooses the source policy instead, remove that entry and run `--sync`.
Only update fingerprints after reviewing both texts.

Run the focused regression tests with `python3 tests/test_etiquette.py`.

## Sync on 2026-10-18

- Updated seven existing sections: backlog, agent mailbox, spawning agents,
  dev commands, remote machines, secrets, and commit messages. Changes include
  internal backlog activity comments, spawned-run reporting, terminal reuse,
  remote authentication and per-connection confirmation preferences, unresolved
  secret diagnosis, and replacing a now-inaccurate proposed commit message.
- Added five missing sections and trigger rows: developer inboxes, personal
  todos, agent catalog, Kubernetes, and keep awake.
- Preserved every original INDEX line, including the just-in-time loading rules
  and all existing trigger rows.
- Initially preserved the stricter database rule for Ben's review. Ben chose
  “Match current source”: requested writes require a writable connection and
  the user's explicit per-connection opt-in. The database section now matches
  the source and the deviation manifest is empty. All 36 sections match.
- Context thresholds, resume packages, handoff hooks, and the original source
  file were not changed.

The live section changes are local machine files outside this repository.
The script, tests, deviation record, and this maintenance guide are tracked here.
