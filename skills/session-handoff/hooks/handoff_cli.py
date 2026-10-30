"""Ledger CLI routing and legacy argv compatibility, separate from file state.

The option table is the grammar. The normalization pass intentionally keeps
legacy first-value, flag-removal and unknown-positional behavior before argparse
consumes known options. In particular, argparse must not reinterpret an owner
beginning with '-' or change legacy diagnostics/exit codes.
"""
import argparse
import os
import sys

from handoff_contract import emit_output


PROTOCOL_COMMANDS = frozenset(('lookup', 'save', 'prepare', 'verify', 'acknowledge',
                              'history', 'report', 'resume', 'wait', 'session',
                              'repair', 'recover-transfer', 'review', 'note',
                              'run-register', 'run-abandon', 'run-list'))
JSON_OPTIONS = ('json', 'pretty')
OWNER_COMMANDS = ('resume', 'supersede', 'abandon', 'claim', 'release')
LEGACY_OPTIONS = {
    'list': JSON_OPTIONS + ('max-age-days',),
    'resolve': JSON_OPTIONS,
    'new-path': JSON_OPTIONS,
    'save-path': (),
    **{command: ('owner',) for command in OWNER_COMMANDS},
    'supersede': ('owner', 'by'),
}


class LegacyUsage(ValueError):
    """Already formatted historical CLI diagnostic (exit 1, not argparse's 2)."""


def _max_age(value):
    try:
        return int(value)
    except ValueError:
        return 14


def parse_legacy(command, args):
    """Normalize only declared options; leave unknown and duplicate values alone."""
    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    parser.set_defaults(json=False, pretty=False, max_age_days=14, owner=None, by=None)
    remaining, normalized = list(args), []
    for option in LEGACY_OPTIONS.get(command, ()):
        if option == 'by' and not remaining:
            raise LegacyUsage('usage: handoff_ledger.py %s <path>' % command)
        flag = '--' + option
        if option in JSON_OPTIONS:
            parser.add_argument(flag, action='store_true')
            if flag in remaining:
                normalized.append(flag)
                remaining = [arg for arg in remaining if arg != flag]
            continue
        parser.add_argument(flag, type=_max_age if option == 'max-age-days' else str)
        if flag not in remaining:
            continue
        index = remaining.index(flag)
        if index + 1 == len(remaining):
            if option == 'owner':
                raise LegacyUsage('usage: handoff_ledger.py %s <path> [--owner X]' % command)
            if option == 'by':
                raise LegacyUsage('usage: handoff_ledger.py supersede <path> [--by <new-path>]')
        else:
            # '=' keeps flag-like values literal, as the original parser did.
            normalized.append(flag + '=' + remaining[index + 1])
        remaining = remaining[:index] + remaining[index + 2:]
    parsed = parser.parse_args(normalized)
    parsed.positionals = remaining
    return parsed


def legacy_resume(command, args):
    return command == 'resume' and args and not args[0].startswith('-') and all(
        not arg.startswith('-') or arg == '--owner' for arg in args[1:])


def cli(argv, ledger):
    if not argv or argv[0] in ('--help', '-h'):
        print('Preferred workflow: resume [--session S] (one-shot) | lookup | save | prepare | verify | acknowledge | wait | history | report | session.\nRun a command with --help for arguments. Legacy interfaces below:')
        print(ledger.__doc__)
        return 0
    cmd, args = argv[0], argv[1:]
    # Legacy `resume <path> [--owner X]` keeps its direct transfer; any other resume form is the one-shot protocol command.
    if cmd in PROTOCOL_COMMANDS and not legacy_resume(cmd, args):
        from handoff_protocol import cli
        return cli(argv)
    if cmd in ("dispatch", "hold") or (cmd == "release" and args[:1] == ["--scope"]):
        from handoff_dispatch import cli as dispatch_cli
        return dispatch_cli(argv)
    try:
        parsed = parse_legacy(cmd, args)
    except LegacyUsage as exc:
        print(str(exc), file=sys.stderr)
        return 1
    args = parsed.positionals
    if cmd == "list":
        as_json = parsed.json
        max_age = parsed.max_age_days
        root = args[0] if args else "."
        stats = {}
        handoffs = ledger.scan(root, max_age, stats)
        if as_json:
            emit_output(handoffs, pretty=parsed.pretty)
        else:
            for h in handoffs:
                print("%s\t%s\t%s\t%s" % (h["ended"], h["topic"], h["path"],
                                          h["description"]))
        if stats.get('errors'):
            print('lookup failed: some handoff locations are unreadable; run lookup for recovery details.', file=sys.stderr)
            return 1
        if not handoffs and not as_json:
            from handoff_protocol import lookup
            print(lookup(root, max_age)['action'])
        if stats["stale"]:
            print("%d open handoff(s) older than %d days hidden; raise --max-age-days to "
                  "see them, or `abandon <path>` to close them" % (stats["stale"], max_age),
                  file=sys.stderr)
        return 0
    if cmd == "resolve":
        as_json = parsed.json
        if not args:
            print("usage: handoff_ledger.py resolve <topic-or-path> [dir] [--json]",
                  file=sys.stderr)
            return 1
        root = args[1] if len(args) > 1 else "."
        resolved = ledger.resolve(args[0], root)
        if not resolved["chain"]:
            print("no handoffs found for topic: %s" % resolved["topic"], file=sys.stderr)
            return 1
        resolved['history_count'] = len(resolved['chain'])
        resolved['chain'] = resolved['chain'][-1:]
        if as_json:
            emit_output(resolved, pretty=parsed.pretty)
        else:
            for h in resolved["chain"]:
                print("%s\t%s\t%s\t%s" % (h["ended"], h["status"], h["path"],
                                          h["description"]))
                for problem in h["problems"]:
                    print("  problem: %s" % problem)
            print("authoritative: %s" % resolved["authoritative"])
            if resolved["must_also_read"]:
                print("must_also_read: %s" % ", ".join(resolved["must_also_read"]))
            if resolved["missing_references"]:
                print("missing_references: %s" % ", ".join(resolved["missing_references"]))
            for u in resolved["unresolved_references"]:
                print("unresolved_reference: %s (%s)" % (u["ref"], u["reason"]))
        return 0
    if cmd == "save-path":
        root = args[0] if args else "."
        print(ledger.save_path(root))
        return 0
    if cmd == "new-path":
        as_json = parsed.json
        if not args:
            print("usage: handoff_ledger.py new-path <topic> [dir] [--json]",
                  file=sys.stderr)
            return 1
        if len(args[0]) > 80 or not ledger.TOPIC_RE.fullmatch(args[0]):
            print("invalid topic: use a short kebab-case topic", file=sys.stderr)
            return 1
        root = args[1] if len(args) > 1 else "."
        result = ledger.new_path(args[0], root)
        if as_json:
            emit_output(dict(result, template=ledger.read_template()), pretty=parsed.pretty)
        else:
            for key in ("directory", "filename", "path", "created", "project", "git",
                        "reason", "skills"):
                print("%s: %s" % (key, result[key]))
        return 0
    if cmd in ("resume", "supersede", "abandon", "claim", "release"):
        owner = parsed.owner
        if not args and cmd != "supersede":
            print("usage: handoff_ledger.py %s <path>" % cmd, file=sys.stderr)
            return 1
        superseded_by = parsed.by
        path = args[0]
        for p in (path, superseded_by):
            if p is not None and not os.path.isfile(p):
                print("no such handoff: %s" % p, file=sys.stderr)
                return 1
        try:
            if cmd == "claim":
                print("claimed (%s): %s" % (ledger.claim(path, owner), path))
                for line in ledger.claim_report(path):
                    print(line)
                return 0
            if cmd == "release":
                ledger.release(path, owner)
                print("released: %s" % path)
                return 0
            status = {"resume": "resumed", "supersede": "superseded",
                      "abandon": "abandoned"}[cmd]
            stamp = ledger._mark(path, status, superseded_by, owner)
        except ledger.ConflictError as e:
            print("refused: %s" % e, file=sys.stderr)
            return 1
        print("marked %s (%s): %s" % (status, stamp, path))
        return 0
    print("unknown subcommand: %s" % cmd, file=sys.stderr)
    return 1
