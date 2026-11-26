#!/usr/bin/env python3
"""Terminal registry (G48, G49, G52): identity at the terminal stage, outside any LLM.

A shell-start snippet runs `register --shell-pid $$`, which writes
~/.context-watch/terminals/<TID>.json with the shell's pid and exact kernel start
time, its tty, and the Herdr terminal or tmux pane only when that pane's shell is
this shell or one of its ancestors, then prints `export KIT_TERMINAL_ID=<TID>`.

At SessionStart each agent session (Claude Code or Codex, agent or subagent) adds
an entry under its TID. KIT_TERMINAL_ID is only a hint: it is trusted when the
TID's shell, compared as pid plus exact start, is an ancestor of the hook. Otherwise
the session gets a background TID anchored to its agent process, so a background
job that inherited a terminal's environment never acts as that terminal.

Ancestry reads the kernel (macOS sysctl KERN_PROC_PID, Linux /proc/<pid>/stat),
never ps: ps prints start times to the second, and two processes can share one.
"""
import contextlib
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid

MAX_DEPTH = 64
AGENT_PROGRAMS = ('claude', 'codex')
TID_PATTERN = re.compile(r'^(tid|bg)-[A-Za-z0-9-]{1,120}$')

# macOS struct kinfo_proc, measured on this platform: 648 bytes, p_starttime
# (timeval: sec int64 at 0, usec int32 at 8), p_pid at 40, e_ppid at 560.
_CTL_KERN, _KERN_PROC, _KERN_PROC_PID, _KERN_PROCARGS2 = 1, 14, 1, 49
_KINFO_SIZE, _OFF_PID, _OFF_PPID = 648, 40, 560

_libc = None


def _sysctl(mib, size):
    global _libc
    import ctypes
    if _libc is None:
        _libc = ctypes.CDLL(None, use_errno=True)
    arr = (ctypes.c_int * len(mib))(*mib)
    buf = ctypes.create_string_buffer(size)
    length = ctypes.c_size_t(size)
    if _libc.sysctl(arr, len(mib), buf, ctypes.byref(length), None, ctypes.c_size_t(0)) != 0:
        return b''
    return buf.raw[:length.value]


def proc_info(pid):
    """(parent pid, exact start) of `pid` from the kernel, or None when unreadable.

    The start is (seconds, microseconds) on macOS and (clock ticks since boot, 0)
    on Linux: both only ever compare with values read the same way."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return None
    if sys.platform == 'darwin':
        import struct
        raw = _sysctl([_CTL_KERN, _KERN_PROC, _KERN_PROC_PID, pid], _KINFO_SIZE)
        if len(raw) < _KINFO_SIZE or struct.unpack_from('i', raw, _OFF_PID)[0] != pid:
            return None
        sec, usec = struct.unpack_from('qi', raw, 0)
        return struct.unpack_from('i', raw, _OFF_PPID)[0], (sec, usec)
    try:
        with open('/proc/%d/stat' % pid, encoding='utf-8', errors='replace') as stream:
            fields = stream.read().rsplit(')', 1)[1].split()
        return int(fields[1]), (int(fields[19]), 0)  # stat fields 4 (ppid) and 22 (starttime)
    except (OSError, IndexError, ValueError):
        return None


def proc_program(pid):
    """Basename of argv[0] of `pid`, or '' when unreadable."""
    argv0 = ''
    if sys.platform == 'darwin':
        raw = _sysctl([_CTL_KERN, _KERN_PROCARGS2, int(pid)], 1 << 18)
        # int argc, then the exec path, NUL padding, then argv[0].
        parts = [p for p in raw[4:].split(b'\0') if p]
        argv0 = parts[1].decode('utf-8', 'replace') if len(parts) > 1 else ''
    else:
        try:
            with open('/proc/%d/cmdline' % int(pid), 'rb') as stream:
                argv0 = stream.read().split(b'\0', 1)[0].decode('utf-8', 'replace')
        except OSError:
            argv0 = ''
    return os.path.basename(argv0).lstrip('-')


def _own_pid():
    return os.getpid()


def _ancestors(origin=None):
    """(pid, start) from `origin` (default: this process) upward, at most MAX_DEPTH
    links; the walk ends at pid 1 or the first process the kernel will not describe."""
    current = _own_pid() if origin is None else origin
    for _ in range(MAX_DEPTH):
        info = proc_info(current)
        if info is None:
            return
        yield current, info[1]
        if current <= 1:
            return
        current = info[0]


def is_ancestor(pid, start, origin=None):
    """True when process `pid`, started exactly at `start`, is `origin` or above it."""
    start = tuple(start or ())
    return any(p == pid and s == start for p, s in _ancestors(origin))


# -------------------------------------------------------------------- registry

def registry_dir():
    return os.path.join(os.path.expanduser('~'), '.context-watch', 'terminals')


def record_path(tid):
    if not TID_PATTERN.match(tid or ''):
        raise ValueError('not a terminal id: %r' % (tid,))
    return os.path.join(registry_dir(), tid + '.json')


def read_record(tid):
    try:
        with open(record_path(tid), encoding='utf-8') as stream:
            data = json.load(stream)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get('tid') == tid else None


@contextlib.contextmanager
def _locked(tid):
    import fcntl
    os.makedirs(registry_dir(), mode=0o700, exist_ok=True)
    fd = os.open(record_path(tid) + '.lock', os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _write(record):
    path = record_path(record['tid'])
    tmp = '%s.%d.tmp' % (path, os.getpid())
    with open(tmp, 'w', encoding='utf-8') as stream:
        json.dump(record, stream, indent=2, sort_keys=True)
        stream.write('\n')
    os.replace(tmp, path)


def _now():
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


def _run(args):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return ''


def _contains(pane_shell, shell_pid):
    """The pane's shell is this shell or one of its ancestors (pid 1 never is one)."""
    return pane_shell > 1 and any(p == pane_shell for p, _ in _ancestors(shell_pid))


def _herdr_terminal(shell_pid):
    if (os.environ.get('HERDR_ENV') or '').strip().lower() not in ('1', 'true', 'yes', 'on'):
        return None
    pane = (os.environ.get('HERDR_PANE_ID') or '').strip()
    if not pane:
        return None
    herdr = os.environ.get('HERDR_BIN_PATH') or 'herdr'
    try:
        info = json.loads(_run([herdr, 'pane', 'process-info', '--pane', pane]) or '{}')
        pane_shell = int(info['result']['process_info']['shell_pid'])
        if not _contains(pane_shell, shell_pid):
            return None
        data = json.loads(_run([herdr, 'pane', 'get', pane]) or '{}')['result']['pane']
        return {'pane_id': str(data['pane_id']), 'terminal_id': str(data['terminal_id'])}
    except (ValueError, KeyError, TypeError):
        return None


def _tmux_pane(shell_pid):
    pane = (os.environ.get('TMUX_PANE') or '').strip()
    if not pane or not os.environ.get('TMUX'):
        return None
    try:
        pane_shell = int(_run(['tmux', 'display-message', '-p', '-t', pane, '#{pane_pid}']).strip())
    except ValueError:
        return None
    return {'pane': pane} if _contains(pane_shell, shell_pid) else None


def _tty():
    for fd in (0, 1, 2):
        with contextlib.suppress(OSError):
            return os.ttyname(fd)
    return None


def register(shell_pid):
    """Create a TID for the shell `shell_pid` and write its record."""
    info = proc_info(shell_pid)
    if info is None:
        raise OSError('cannot read process %s from the kernel' % shell_pid)
    tid = 'tid-' + uuid.uuid4().hex
    record = {'version': 1, 'tid': tid, 'kind': 'terminal', 'created': _now(), 'tty': _tty(),
              'shell': {'pid': shell_pid, 'start': list(info[1])}, 'sessions': []}
    herdr = _herdr_terminal(shell_pid)
    if herdr:
        record['herdr'] = herdr
    tmux = _tmux_pane(shell_pid)
    if tmux:
        record['tmux'] = tmux
    with _locked(tid):
        _write(record)
    return tid


def _agent_anchor():
    """The nearest claude or codex ancestor of the hook, else the hook's parent."""
    chain = list(_ancestors())
    for pid, start in chain[1:]:
        if proc_program(pid) in AGENT_PROGRAMS:
            return pid, start
    return chain[1] if len(chain) > 1 else (chain[0] if chain else (os.getpid(), (0, 0)))


def _trusted_tid():
    tid = (os.environ.get('KIT_TERMINAL_ID') or '').strip()
    if not TID_PATTERN.match(tid) or not tid.startswith('tid-'):
        return None, None
    record = read_record(tid)
    shell = (record or {}).get('shell') or {}
    if not isinstance(shell.get('pid'), int) or not is_ancestor(shell['pid'], shell.get('start')):
        return None, None
    return tid, record


def _background_tid():
    pid, start = _agent_anchor()
    tid = 'bg-%d-%s' % (pid, '-'.join(str(int(x)) for x in start))
    return tid, {'version': 1, 'tid': tid, 'kind': 'background', 'created': _now(),
                 'anchor': {'pid': pid, 'start': list(start), 'program': proc_program(pid)}, 'sessions': []}


def kit_session_id(agent, provider_session):
    return 'ks-' + hashlib.sha1(('%s:%s' % (agent, provider_session)).encode('utf-8')).hexdigest()[:16]


def _form(record, subagent):
    if subagent:
        return 'subagent'
    if record.get('kind') == 'background':
        return 'background'
    if record.get('herdr'):
        return 'herdr'
    if record.get('tmux'):
        return 'tmux'
    if (os.environ.get('AGENTSROOM_AGENT_ID') or '').strip():
        return 'agentsroom'
    return 'terminal'


def record_session(evt, agent):
    """Add this hook's session to its TID (G49) and return the entry with its tid.

    A subagent (an event carrying agent_id) is named by its agent_id and its parent
    is the kit session of the event's session_id."""
    evt = evt or {}
    agent_id = str(evt.get('agent_id') or '').strip()
    session = str(evt.get('session_id') or '').strip()
    provider = agent_id or session
    if not provider:
        return None
    tid, fresh = _trusted_tid()
    if tid is None:
        tid, fresh = _background_tid()
    with _locked(tid):
        record = read_record(tid) or fresh
        pid, start = _agent_anchor()
        entry = {'kit_session': kit_session_id(agent, provider), 'agent': agent, 'provider_session': provider,
                 'form': _form(record, bool(agent_id)),
                 'parent': kit_session_id(agent, session) if agent_id and session else None,
                 'process': {'pid': pid, 'start': list(start)}}
        sessions = [s for s in record.get('sessions') or [] if isinstance(s, dict)]
        if not any(s.get('kit_session') == entry['kit_session'] for s in sessions):
            sessions.append(dict(entry, started=_now()))
            record['sessions'] = sessions
            _write(record)
    return dict(entry, tid=tid)


def _herdr_json(*args):
    herdr = os.environ.get('HERDR_BIN_PATH') or 'herdr'
    try:
        return json.loads(_run([herdr, 'pane'] + list(args)) or 'null')['result']
    except (ValueError, KeyError, TypeError):
        return None


def _named_sessions(pid, sessions):
    """Sessions whose agent process is `pid` at its exact recorded start (G52: a reused pid is not them)."""
    info = proc_info(pid) if isinstance(pid, int) else None
    if info is None:
        return []
    return [s['kit_session'] for s in sessions
            if (s.get('process') or {}).get('pid') == pid and list((s.get('process') or {}).get('start') or []) == list(info[1])]


def _bracketed_read(terminal_id):
    """One list -> process-info -> get pass; a pane renumbered in between is a mismatch, never a result."""
    listed = _herdr_json('list')
    if listed is None:
        return 'fail', 'herdr pane list was unreadable'
    panes = [p for p in listed.get('panes') or [] if isinstance(p, dict) and p.get('terminal_id') == terminal_id]
    if len(panes) != 1:
        return 'fail', 'herdr pane list has %d panes with terminal %s, not one' % (len(panes), terminal_id)
    pane_id = str(panes[0].get('pane_id') or '')
    info = (_herdr_json('process-info', '--pane', pane_id) or {}).get('process_info')
    confirmed = (_herdr_json('get', pane_id) or {}).get('pane') or {}
    if not isinstance(info, dict) or info.get('pane_id') != pane_id or confirmed.get('terminal_id') != terminal_id:
        return 'mismatch', 'pane %s no longer maps to terminal %s' % (pane_id, terminal_id)
    return 'ok', (pane_id, info)


def process_info(tid):
    """G50: the processes in the Herdr terminal of `tid`, each naming the TID's sessions it runs."""
    record = read_record(tid) if TID_PATTERN.match(tid or '') else None
    terminal_id = ((record or {}).get('herdr') or {}).get('terminal_id')
    if not terminal_id:
        return {'ok': False, 'tid': tid, 'error': 'no Herdr terminal recorded for this TID'}
    for _ in range(2):
        status, value = _bracketed_read(terminal_id)
        if status != 'mismatch':
            break
    if status != 'ok':
        return {'ok': False, 'tid': tid, 'terminal_id': terminal_id, 'error': value}
    pane_id, info = value
    sessions = [s for s in record.get('sessions') or [] if isinstance(s, dict) and s.get('kit_session')]
    processes = [dict(p, sessions=_named_sessions(p.get('pid'), sessions))
                 for p in info.get('foreground_processes') or [] if isinstance(p, dict)]
    return {'ok': True, 'tid': tid, 'terminal_id': terminal_id, 'pane_id': pane_id,
            'shell_pid': info.get('shell_pid'), 'processes': processes,
            'sessions': [s['kit_session'] for s in sessions]}


def main(argv):
    import argparse
    parser = argparse.ArgumentParser(prog='handoff_terminal.py', description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest='command', required=True)
    reg = sub.add_parser('register', help='create this shell\'s TID and print its export line')
    reg.add_argument('--shell-pid', type=int, default=os.getppid())
    sub.add_parser('snippet', help='print the line a shell rc file runs at start')
    pinfo = sub.add_parser('process-info', help='print the processes of a TID\'s Herdr terminal as JSON')
    pinfo.add_argument('tid')
    args = parser.parse_args(argv)
    if args.command == 'process-info':
        result = process_info(args.tid)
        print(json.dumps(result, sort_keys=True))
        return 0 if result['ok'] else 1
    if args.command == 'snippet':
        print('eval "$(%s %s register --shell-pid $$ 2>/dev/null)"' % ('python3', os.path.abspath(__file__)))
        return 0
    try:
        tid = register(args.shell_pid)
    except (OSError, ValueError) as exc:
        print('handoff_terminal: %s' % exc, file=sys.stderr)
        return 1
    print('export KIT_TERMINAL_ID=%s' % tid)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
