#!/usr/bin/env python3
"""Claude mandatory gate. Bootstrap uses only stdlib until runtime validation.

Invoke with python3 -I -B from a sealed runtime, never a project checkout.
An absent project opt-in returns before importing kit code or reading a trace.
"""
import hashlib
import fcntl
import json
import os
from pathlib import Path
import shlex
import stat
import sys

RECOVERY_TOOLS = frozenset(('Read', 'Glob', 'Grep', 'TaskGet', 'TaskList', 'AskUserQuestion'))


def project_root(cwd):
    """Stdlib bootstrap discovery, without importing code from a project tree.

    Established ledger/Git boundaries take precedence; a config identifies an
    otherwise uninitialised project. Never cross HOME or a linked worktree.
    """
    start = Path(cwd).resolve()
    home, configured = Path.home().resolve(), None
    for folder in (start, *start.parents):
        if (folder / '.handoffs').is_dir():
            return str(folder)
        if configured is None and (folder / '.context-watch.json').is_file():
            configured = folder
        if (folder / '.git').exists() or folder == home:
            return str(configured or folder)
    return str(configured or start)


def timing(project, cwd, cw, model, window):
    """Timing only: no project setting grants continuation or skill activation."""
    global_path = Path.home() / '.context-watch/thresholds.json'
    try:
        user = json.loads(global_path.read_text())
    except FileNotFoundError:
        user = {}
    if not isinstance(user, dict):
        raise ValueError('global configuration must be an object')
    point, source = None, 'default'
    for data, path in ((user, global_path), (project, Path(cwd) / '.context-watch.json')):
        if 'mandatory_at_tokens' in data:
            point, source = data['mandatory_at_tokens'], str(path)
            if point is not None and (type(point) is not int or point <= 0):
                raise ValueError('mandatory_at_tokens from %s must be a positive integer or null' % path)
    import watch_decision as wd
    lead, lead_source, error = wd.warning_lead(cw.warning_window_candidates(cwd))
    if error:
        raise ValueError(error)
    limit, _ = cw.resolve_threshold(model, window, cwd)
    if type(window) is int and window > 0:
        reserve = wd.WatchConfig.from_environ(os.environ).reserve
        limit, _ = wd.cap_threshold(limit, '', window, 'reported', reserve)
        if point is not None and point > window - reserve:
            raise ValueError('mandatory_at_tokens exceeds reported window minus reserve')
    warning = max(0, limit - (lead or 0))
    if point is not None and point < warning:
        raise ValueError('mandatory_at_tokens is below warning point %s' % warning)
    return point, source, warning, lead_source


def user_auto(project):
    """Launch/user authority only. Project values can disable the user default."""
    explicit = os.environ.get('HANDOFF_AUTO', '').strip()
    if explicit:
        return explicit.lower() in ('1', 'true', 'yes', 'on')
    if project.get('auto') is False:
        return False
    try:
        user = json.loads((Path.home() / '.context-watch/thresholds.json').read_text())
        return isinstance(user, dict) and user.get('auto') is True
    except (OSError, ValueError):
        return False


def save_command(folder, cwd, session):
    project = json.loads((Path(cwd) / '.context-watch.json').read_text())
    return ('HANDOFF_AUTO=%d ' % user_auto(project)) + shlex.join([
                       'python3', '-B', str(folder / 'hooks/handoff_ledger.py'),
                       'save', cwd, '--session', session, '--input', '-'])


def is_save(evt, command):
    if evt.get('tool_name') != 'Bash':
        return False
    text = (evt.get('tool_input') or {}).get('command', '')
    prefix = command + " <<'HANDOFF_JSON'\n"
    suffix = '\nHANDOFF_JSON'
    if not text.startswith(prefix) or not text.endswith(suffix):
        return False
    body = text[len(prefix):-len(suffix)]
    if '\nHANDOFF_JSON\n' in body:
        return False
    try:
        return isinstance(json.loads(body), dict)
    except ValueError:
        return False


def runtime_folder(cwd):
    folder = Path(__file__).resolve().parent.parent
    project = Path(cwd).resolve()
    if folder == project or project in folder.parents:
        raise ValueError('gate runtime is inside this project')
    for parent in (folder, *folder.parents):
        if (parent / '.git').exists():
            raise ValueError('gate runtime is inside a Git project')
        info = parent.stat()
        if info.st_uid not in (0, os.getuid()):
            raise ValueError('gate runtime has an untrusted ancestor owner')
        if info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX:
            raise ValueError('gate runtime has a writable ancestor')
    manifest = json.loads((folder / 'runtime-manifest.json').read_text())
    identity = hashlib.sha256(json.dumps(manifest, sort_keys=True,
        separators=(',', ':')).encode()).hexdigest()
    if folder.name != manifest['version'] + '-' + identity:
        raise ValueError('gate runtime identity mismatch')
    files = set()
    for path in (folder, *folder.rglob('*')):
        info = path.lstat()
        if path.is_symlink() or info.st_uid != os.getuid() or info.st_mode & 0o222:
            raise ValueError('gate runtime must be sealed and user-owned')
        if path.is_file() and path.name != 'runtime-manifest.json':
            files.add(path.relative_to(folder).as_posix())
            entry = manifest['files'].get(path.relative_to(folder).as_posix(), {})
            if entry != {'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                         'mode': info.st_mode & 0o777}:
                raise ValueError('gate runtime integrity mismatch')
    if files != manifest['files'].keys():
        raise ValueError('gate runtime payload is incomplete')
    return folder


def output(event, reason):
    if event == 'PreToolUse':
        print(json.dumps({'hookSpecificOutput': {'hookEventName': event,
            'permissionDecision': 'deny', 'permissionDecisionReason': reason}}))
    else:
        print(json.dumps({'systemMessage': reason}))


def gate_state(cwd, session):
    directory = Path.home() / '.context-watch/mandatory'
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    for path in (directory, directory.parent):
        info = path.lstat()
        if path.is_symlink() or info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise ValueError('unsafe mandatory state directory')
    digest = hashlib.sha256((str(Path(cwd).resolve()) + '\0' + session).encode()).hexdigest()
    fd = os.open(directory / (digest + '.json'), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    info = os.fstat(fd)
    if info.st_uid != os.getuid() or info.st_mode & 0o077 or not stat.S_ISREG(info.st_mode):
        os.close(fd)
        raise ValueError('unsafe mandatory state file')
    stream = os.fdopen(fd, 'r+')
    fcntl.flock(stream, fcntl.LOCK_EX)
    return stream


def enforce(evt, folder, cw, point, source, warning, lead_source, usage):
    event, cwd = evt['hook_event_name'], evt.get('cwd') or os.getcwd()
    session = cw.session_key(evt)
    if not session:
        print(json.dumps({'systemMessage': 'mandatory-unavailable: missing session identity'}))
        return
    with gate_state(cwd, session) as stream:
        raw = stream.read()
        state = json.loads(raw) if raw else {}
        active = state.get('trigger')
        if not active and (not usage or usage['occupancy'] < point):
            if not usage:
                print(json.dumps({'systemMessage': 'mandatory-unavailable: context telemetry unavailable'}))
            return
        if not active:
            latch = cw.latch_path(session)
            try:
                fd = os.open(latch, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            except FileExistsError:
                pass
            else:
                with os.fdopen(fd, 'w') as out:
                    out.write('%d/%d/%d\n' % (usage['occupancy'], point, usage['occupancy']))
            cw.write_session_note(cwd, evt, True)
            note = cw._ledger().read_session_note(cwd, session=session)
            active = note.get('trigger_id')
            if not active:
                raise ValueError('mandatory trigger could not be persisted')
            state = {'trigger': active, 'stops': 0, 'save_attempts': 0, 'last_stop_attempts': -1}
        records = cw._ledger()._records(cw.handoff_identity.project_root(cwd))
        published = any(r['fm'].get('session_id') == session
            and r['fm'].get('trigger_id') == active and not r['problems']
            and r['record'].published() and r['record'].trusted() for r in records)
        if published:
            return  # Restart holds belong to their owners; never clear them here.
        command = save_command(folder, cwd, session)
        reason = ('Mandatory handoff at %s tokens [%s]; warning point %s [%s]. Publish before new work. '
                  'Use a literal JSON draft in a quoted HANDOFF_JSON heredoc. Save command: %s'
                  % (point, source, warning, lead_source, command))
        result = None
        if event == 'Stop':
            attempts = state.get('save_attempts', 0)
            if (evt.get('stop_hook_active') or state['stops'] >= 2
                    or state.get('last_stop_attempts') == attempts):
                result = {'systemMessage': 'Mandatory save is still blocked. Report the human '
                          'blocker and checkpoint text; automatic continuation is unavailable.'}
            else:
                state['stops'] += 1
                state['last_stop_attempts'] = attempts
                result = {'decision': 'block', 'reason': reason}
        elif is_save(evt, command):
            state['save_attempts'] = state.get('save_attempts', 0) + 1
        else:
            result = {'hookSpecificOutput': {'hookEventName': event,
                'permissionDecision': 'deny', 'permissionDecisionReason': reason}}
        stream.seek(0)
        json.dump(state, stream)
        stream.truncate()
        stream.flush()
        os.fsync(stream.fileno())
        if result:
            print(json.dumps(result))


def obligation_observed(evt, project, cwd):
    """Error-path evidence only; never import a runtime that failed validation.

    A healthy path uses the watcher's complete observation/timing validation.
    Here only a persisted trigger or fresh, measured Claude usage may deny work.
    """
    try:
        session = str(evt.get('session_id') or '').strip()
        if not session and evt.get('transcript_path'):
            session = 't-' + hashlib.sha1(str(evt['transcript_path']).encode()).hexdigest()[:16]
        if session:
            with gate_state(cwd, session) as stream:
                state = json.loads(stream.read() or '{}')
                if state.get('trigger'):
                    return True
    except (OSError, ValueError, AttributeError):
        pass
    try:
        user = json.loads((Path.home() / '.context-watch/thresholds.json').read_text())
    except (OSError, ValueError):
        user = {}
    point = project.get('mandatory_at_tokens', user.get('mandatory_at_tokens') if isinstance(user, dict) else None)
    if type(point) is not int or point <= 0:
        return False
    try:
        from datetime import datetime, timezone
        with open(evt.get('transcript_path') or '', 'rb') as stream:
            if os.fstat(stream.fileno()).st_size > 2_000_000:
                stream.seek(-2_000_000, os.SEEK_END)
                stream.readline()
            usage, timestamp = None, None
            for line in stream:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(row, dict) or row.get('isSidechain'):
                    continue
                if (row.get('type') == 'system' and row.get('subtype') == 'compact_boundary') or row.get('isCompactSummary'):
                    usage, timestamp = None, None
                    continue
                message = row.get('message')
                candidate = message.get('usage') if isinstance(message, dict) else None
                if isinstance(candidate, dict) and 'input_tokens' in candidate:
                    usage, timestamp = candidate, row.get('timestamp')
        if not usage:
            return False
        if timestamp:
            dt = datetime.fromisoformat(str(timestamp).replace('Z', '+00:00'))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if (datetime.now(timezone.utc) - dt).total_seconds() > 600:
                return False
        tokens = [usage.get(key) or 0 for key in ('input_tokens', 'cache_creation_input_tokens',
                  'cache_read_input_tokens', 'output_tokens')]
        return all(type(n) is int and n >= 0 for n in tokens) and sum(tokens) >= point
    except (OSError, ValueError, TypeError):
        return False


def main():
    if os.environ.get('CONTEXT_WATCH_DISABLE') == '1':
        return
    evt = json.load(sys.stdin)
    cwd = project_root(evt.get('cwd') or os.getcwd())
    evt = dict(evt, cwd=cwd)
    try:
        project = json.loads((Path(cwd) / '.context-watch.json').read_text())
    except (OSError, ValueError):
        return
    if not isinstance(project, dict) or project.get('handoff_v3') is not True:
        return
    event = evt.get('hook_event_name')
    if event not in ('PreToolUse', 'Stop') or evt.get('agent_id'):
        return
    if evt.get('tool_name') in RECOVERY_TOOLS:
        return
    recovery_save = False
    try:
        folder = runtime_folder(cwd)
        # -I removes cwd/PYTHONPATH; only the verified runtime supplies kit modules.
        sys.path.insert(0, str(folder / 'hooks'))
        import context_watch as cw
        recovery_save = is_save(evt, save_command(folder, cwd, cw.session_key(evt)))
        entries = cw.load_transcript_tail(evt.get('transcript_path') or '')
        usage, model = cw.claude_usage(entries)
        age = cw.usage_age_seconds(entries)
        if age is not None and age > cw.STALE_USAGE_S:
            usage = None
        try:
            point, source, warning, lead_source = timing(project, cwd, cw, model, evt.get('context_window'))
        except ValueError as exc:
            if cw.claim_once(evt, '.mandatory-config'):
                print(json.dumps({'systemMessage': 'handoff-status: invalid mandatory configuration: %s; '
                                  'automatic v3 transitions disabled.' % exc}))
            return
        if point is not None:
            enforce(evt, folder, cw, point, source, warning, lead_source, usage)
    except Exception as exc:
        output(event if not recovery_save and obligation_observed(evt, project, cwd) else 'recovery',
               'mandatory-state-error: %s; report the blocker.' % type(exc).__name__)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(json.dumps({'systemMessage': 'mandatory-unavailable: invalid hook input (%s)' % type(exc).__name__}))
