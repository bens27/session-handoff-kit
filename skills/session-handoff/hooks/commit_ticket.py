#!/usr/bin/env python3
"""Opt-in commit-msg hook. Only HANDOFF_TICKET_ID supplies new attribution."""
import os
from pathlib import Path
import re
import sys
import string
import subprocess
import tempfile

TICKET_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._/-]{0,127}\Z', re.ASCII)


def comment_character():
    try:
        result = subprocess.run(['git', 'config', '--get', 'core.commentChar'],
                                capture_output=True, text=True, timeout=1)
        value = result.stdout.strip()
        if result.returncode == 0 and (len(value) == 1 or value == 'auto'):
            return value
    except (OSError, UnicodeError, subprocess.SubprocessError):
        pass
    return '#'


def main(argv):
    if len(argv) != 1:
        return 0
    path = Path(argv[0])
    text = path.read_bytes().decode('utf-8')
    ticket = os.environ.get('HANDOFF_TICKET_ID', '')
    if not ticket and not re.search(r'^ticket[ \t]*:', text, re.I | re.M):
        return 0
    lines = text.splitlines(keepends=True)
    # Git comments and verbose diff are not commit message content.
    comment = comment_character()
    if comment == 'auto':
        # The editor's chosen character is not exposed to commit-msg. Preserve
        # ambiguous punctuation-prefixed lines, including real '#' body text.
        if any(line and line[0] in string.punctuation for line in lines):
            return 0
        comment = '#'
    visible = []
    for index, line in enumerate(lines):
        if line.startswith(comment + ' ------------------------ >8 '):
            break
        if not line.startswith(comment):
            visible.append((index, line.rstrip('\r\n')))
    while visible and not visible[-1][1].strip():
        visible.pop()
    if not visible:
        return 0  # Preserve Git's empty-message rejection.
    end = visible[-1][0] + 1
    block = []
    for _, line in reversed(visible):
        if not line.strip():
            break
        block.append(line)
    block.reverse()
    # A trailer paragraph consists only of token/value lines and continuations.
    is_trailers = all(re.match(r'[A-Za-z0-9-]+[ \t]*:', line) or
                      (i > 0 and line.startswith((' ', '\t')))
                      for i, line in enumerate(block))
    is_trailers = is_trailers and len(visible) > len(block)
    values = []
    if is_trailers:
        ticket_field = False
        for line in block:
            match = re.match(r'ticket[ \t]*:[ \t]*(.*)$', line, re.I)
            if match:
                values.append(match.group(1).strip())
                ticket_field = True
            elif line.startswith((' ', '\t')) and ticket_field:
                # Ticket IDs cannot span multiple lines.
                values[-1] += ' ' + line.strip()
            else:
                ticket_field = False
    if values:
        if len(values) != 1 or not TICKET_ID.fullmatch(values[0]):
            print('session-handoff: invalid Ticket trailer (expected one ticket ID)', file=sys.stderr)
            return 1
        return 0
    if not ticket:
        return 0
    from handoff_contract import METADATA_FORMATS
    if not METADATA_FORMATS['ticket'].fullmatch(ticket):
        return 0
    newline = '\r\n' if '\r\n' in text else '\n'
    prefix = ''.join(lines[:end]).rstrip('\r\n')
    separator = newline if is_trailers else newline * 2
    updated = prefix + separator + f'Ticket: {ticket}' + newline + ''.join(lines[end:])
    descriptor, temporary = tempfile.mkstemp(prefix='.ticket-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(updated.encode('utf-8'))
            stream.flush()
            os.fchmod(stream.fileno(), path.stat().st_mode & 0o777)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return 0


if __name__ == '__main__':
    try:
        code = main(sys.argv[1:])
    except (OSError, UnicodeError, ImportError) as exc:
        print('session-handoff ticket hook skipped: ' + str(exc), file=sys.stderr)
        code = 0
    sys.exit(code)
