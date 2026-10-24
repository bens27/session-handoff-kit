"""Identity module: who am I, where is my project, where is my state.

A leaf module (standard library only, no kit imports) that owns:

- session identity (terminal pointer -> CODEX_THREAD_ID -> HANDOFF_SESSION_ID),
  terminal identity (HANDOFF_TERMINAL_ID -> AGENTSROOM_AGENT_ID) and the latch
  key fallback ``t-`` + sha1(transcript)[:16];
- the canonical project root (handoff convention discovery, linked-worktree
  boundary);
- every temp-state path, as one named function each. Callers never append a
  suffix to a path from here.

The state directory is a parameter of every path function. ``None`` means the
environment knob HANDOFF_STATE_DIR when set and non-empty, else
``tempfile.gettempdir()`` (read at call time, so tests may swap it).

File names and hash derivations are frozen: sessions in flight keep their latch
and pointers across an upgrade (SPEC.md, "Temp state").
"""
import hashlib
import json
import os
import stat
import tempfile
import time

STATE_DIR_ENV = 'HANDOFF_STATE_DIR'
POINTER_MAX_BYTES = 4096
POINTER_MAX_AGE_S = 86400


def truthy(value):
    """'1', 'true', 'yes', 'on' (any case, surrounding space ignored)."""
    return str(value or '').strip().lower() in ('1', 'true', 'yes', 'on')


# ------------------------------------------------------------------ state dir

def state_dir(directory=None):
    """Where temp state lives: `directory`, else an absolute $HANDOFF_STATE_DIR,
    else gettempdir(). A relative value is ignored: it would resolve against each
    process's cwd and could land state inside the project tree."""
    if directory:
        return directory
    value = (os.environ.get(STATE_DIR_ENV) or '').strip()
    return value if os.path.isabs(value) else tempfile.gettempdir()


def temp_bases():
    """System temp locations a project must never be recorded under."""
    return {os.path.realpath(p) for p in (tempfile.gettempdir(), '/tmp', '/var/tmp') if p}


def under_temp_base(path):
    path = os.path.realpath(path)
    return any(path == b or path.startswith(b + os.sep) for b in temp_bases())


def handoffs_dir_usable(root):
    """True when <root>/.handoffs may be discovered, read or written.

    Anywhere but an exact temp base, a directory is enough. At a temp base
    (world-writable, e.g. /tmp) another local user could plant it, so it must
    be a real directory, not a symlink, owned by this uid with owner-only
    write (no 0o022 bits): the same owner rule as read_project_pointer, which
    additionally requires 0o077-clean modes on its regular pointer file.
    """
    hdir = os.path.join(root, ".handoffs")
    if os.path.realpath(root) not in temp_bases():
        return os.path.isdir(hdir)
    try:
        info = os.lstat(hdir)
    except OSError:
        return False
    return stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and not info.st_mode & 0o022


class UntrustedStateDir(OSError):
    """An exact temp base whose .handoffs fails handoffs_dir_usable."""
    reason = 'untrusted-temp-handoffs'

    def __init__(self, root):
        super().__init__('untrusted-temp-handoffs: %s' % os.path.join(root, '.handoffs'))


def local_state_dir(root, sub, create=False):
    """The one chokepoint for <root>/.handoffs/<sub> state (887c009c).

    At an exact temp base, a missing .handoffs is created 0o700 only when create
    is set, and .handoffs is then re-checked, so one planted before or during
    creation is refused: UntrustedStateDir, with nothing read or written there.
    """
    hdir = os.path.join(root, '.handoffs')
    if os.path.realpath(root) in temp_bases():
        if create:
            try:
                os.mkdir(hdir, 0o700)
            except FileExistsError:
                pass
        if not handoffs_dir_usable(root):
            if create or os.path.lexists(hdir):
                raise UntrustedStateDir(root)
    path = os.path.join(hdir, sub) if sub else hdir
    if create:
        os.makedirs(path, exist_ok=True)
    return path


def _sha1(text, length):
    return hashlib.sha1(text.encode('utf-8', 'replace')).hexdigest()[:length]


# --------------------------------------------------------------- project root

def parse_front_matter(text):
    """Very small line-based front matter parser: key: value between --- fences."""
    fm = {}
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return fm
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration:
        return {}
    for line in lines[1:end]:
        if line.strip() == "---":
            break
        if ":" in line:
            key, _, value = line.partition(":")
            fm[key.strip().lower()] = value.strip()
    return fm


def read_front_matter(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        return parse_front_matter(f.read())


def same_dir(a, b):
    return os.path.normcase(os.path.realpath(os.path.expanduser(a))) == \
        os.path.normcase(os.path.realpath(os.path.expanduser(b)))


def fallback_dir(root):
    """The per-project directory save_path() writes to when a project has no
    local .handoffs convention."""
    return os.path.expanduser(os.path.join("~", ".claude", "handoffs",
                                           os.path.basename(os.path.abspath(root))))


def fallback_claims(d):
    """True when d's fallback directory holds a handoff recording d as its project."""
    fdir = fallback_dir(d)
    try:
        for name in os.listdir(fdir):
            if name.endswith(".md"):
                project = read_front_matter(os.path.join(fdir, name)).get("project")
                if project and same_dir(project, d):
                    return True
    except Exception:
        pass
    return False


def linked_worktree_root(start):
    """Nearest linked-worktree boundary; submodules retain ancestor discovery.

    Inspect the marker directly: a missing/slow Git executable must never make
    discovery fall through to the main checkout's checkpoint directory.
    """
    current = os.path.abspath(start)
    while True:
        marker = os.path.join(current, '.git')
        if os.path.isfile(marker):
            # Submodules also use a Git-file marker, but must still inherit the
            # superproject convention. A linked worktree has common metadata
            # (or its reciprocal gitdir marker when common metadata is lost).
            try:
                with open(marker, encoding='utf-8') as stream:
                    pointer = stream.read(8192).strip()
                if pointer.startswith('gitdir: ') and pointer[8:].strip():
                    gitdir = os.path.realpath(os.path.join(current, pointer[8:].strip()))
                    if (os.path.lexists(os.path.join(gitdir, 'commondir'))
                            or os.path.isfile(os.path.join(gitdir, 'gitdir'))
                            or os.path.basename(os.path.dirname(gitdir)) == 'worktrees'):
                        return current
                else:
                    return current  # malformed metadata must not fall into main
            except (OSError, ValueError):
                return current  # preserve a fail-closed boundary on unreadable metadata
        if os.path.isdir(marker):
            return None
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


def project_root(start):
    """Nearest ancestor (inclusive) with a .handoffs/ dir or a fallback-dir handoff
    naming it as project, stopping at $HOME; else start. A linked Git worktree
    is a boundary: use its root if no nearer handoff convention exists, and
    never discover its main checkout's ledger. Other Git repositories retain
    convention-based discovery: a folder of projects can itself be a repo.
    Fallback handoffs never make a system temp base (/tmp, /var/tmp,
    gettempdir()) a project: scratch dirs under it must not inherit that ledger."""
    start = os.path.abspath(start)
    worktree = linked_worktree_root(start)
    home = os.path.abspath(os.path.expanduser("~"))
    bases = temp_bases()
    d = start
    while d != home:
        if handoffs_dir_usable(d) or (
                (d != start or worktree is not None) and os.path.realpath(d) not in bases and fallback_claims(d)):
            return d
        if worktree and same_dir(d, worktree):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return start


def canonical_root(start):
    """realpath(project_root(start)): the form every hash below is keyed on."""
    return os.path.realpath(project_root(start))


# ------------------------------------------------------------------- identity

def terminal_id():
    """Wrapper identity that survives /clear: one terminal, one current handoff.

    A blank HANDOFF_TERMINAL_ID falls through to AGENTSROOM_AGENT_ID. Ids over
    120 characters keep a readable prefix plus a digest of the whole value, so
    two long ids sharing a prefix never name the same terminal."""
    for name in ('HANDOFF_TERMINAL_ID', 'AGENTSROOM_AGENT_ID'):
        value = (os.environ.get(name) or '').strip()
        if value:
            value = value.splitlines()[0].strip()
            if len(value) > 120:
                value = value[:100] + '-' + hashlib.sha256(value.encode('utf-8', 'replace')).hexdigest()[:16]
            return value
    return ''


def host_session():
    """The host's own thread id (Codex), or ''."""
    return (os.environ.get('CODEX_THREAD_ID') or '').strip()


def environment_session():
    """The caller-declared HANDOFF_SESSION_ID, or ''."""
    return (os.environ.get('HANDOFF_SESSION_ID') or '').strip()


def without_session_identity(environ):
    """A copy of `environ` a new host session can start from: no inherited session ids."""
    child = dict(environ)
    child.pop('CODEX_THREAD_ID', None)
    child.pop('HANDOFF_SESSION_ID', None)
    return child


def runner_environment(environ):
    """A copy of `environ` for an auto runner's children: one terminal per runner.

    Drops inherited session ids and AGENTSROOM_AGENT_ID (it would route a save to
    restarting the caller's tab), and always assigns a fresh runner-<uuid>
    HANDOFF_TERMINAL_ID: an inherited one (claude-auto's tmux-<uuid>, the caller's
    own) would let children resume the caller's or a sibling runner's handoffs.
    Call once per runner and reuse the result, so every run shares one terminal."""
    import uuid
    child = without_session_identity(environ)
    child.pop('AGENTSROOM_AGENT_ID', None)
    child['HANDOFF_TERMINAL_ID'] = 'runner-' + uuid.uuid4().hex
    return child


def session_key(evt):
    """The session id, else a hash of the transcript path, so sessions whose
    host omits the id never share one latch; None when neither is known."""
    sid = str(evt.get("session_id") or "").strip()
    if sid:
        return sid
    tp = evt.get("transcript_path") or ""
    return "t-" + _sha1(tp, 16) if tp else None


def _read_terminal_pointer(path):
    """The pointer object (first POINTER_MAX_BYTES characters); ValueError when not one."""
    with open(path, encoding='utf-8') as stream:
        raw = stream.read(POINTER_MAX_BYTES)
    try:
        data = json.loads(raw)
    except RecursionError:
        raise ValueError('excessively nested pointer') from None
    if not isinstance(data, dict):
        raise ValueError('pointer is not an object')
    return data


def terminal_session(root, directory=None):
    """Session id the hooks recorded for this terminal in this project, or ''."""
    terminal = terminal_id()
    if not terminal:
        return ''
    try:
        data = _read_terminal_pointer(terminal_pointer_path(root, terminal, directory))
        if 0 <= time.time() - float(data['ts']) <= POINTER_MAX_AGE_S and isinstance(data['session_id'], str):
            host = host_session()
            # A terminal survives restarts; its cached hook session may not.
            if host and host != (data.get('host_session_id') or data['session_id']):
                return ''
            return data['session_id'].strip()[:200]
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return ''


def default_session(root, directory=None):
    """Fallback order: terminal pointer -> CODEX_THREAD_ID -> HANDOFF_SESSION_ID."""
    return terminal_session(root, directory) or host_session() or environment_session()


# ----------------------------------------------------------- temp-state paths

def session_note_path(root, session=None, directory=None):
    """Where context_watch.py leaves this project's live session facts
    (session id, transcript, whether the trigger fired), so ledger commands
    run from the agent's shell can read them without hook stdin."""
    key = _sha1(canonical_root(root) + "\0" + (session or "unidentified"), 20)
    return os.path.join(state_dir(directory), "context-watch-session-%s.json" % key)


def session_status_path(root, session, directory=None):
    return session_note_path(root, session, directory) + '.status'


def session_routed_path(root, session, directory=None):
    return session_note_path(root, session, directory) + '.routed'


def prepared_receipt_path(root, session, directory=None):
    return session_note_path(root, session, directory) + '.prepared'


def delivery_receipt_path(root, session, directory=None):
    return prepared_receipt_path(root, session, directory) + '.delivery'


def publisher_lock_path(root, directory=None):
    """Lock target serialising publication in a project (ledger.locked adds '.lock')."""
    return session_note_path(root, 'publisher', directory)


def latch_path(session_id, stage=1, directory=None):
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(session_id))
    return os.path.join(state_dir(directory),
                        "context-watch-%s.fired%s" % (safe, "" if stage == 1 else stage))


def latch_floor_path(session_id, directory=None):
    return latch_path(session_id, 1, directory) + '.floor'


def latch_claim_path(session_id, suffix, directory=None):
    """One-shot claim beside the first latch (suffix such as '.resume')."""
    return latch_path(session_id, 1, directory) + suffix


def auto_count_path(cwd, directory=None):
    try:
        root = project_root(cwd)
    except Exception:
        root = cwd
    return os.path.join(state_dir(directory),
                        "context-watch-auto-%s.count" % _sha1(os.path.realpath(root), 20))


def terminal_pointer_path(cwd, terminal, directory=None):
    return os.path.join(state_dir(directory), "context-watch-terminal-%s.json"
                        % _sha1(canonical_root(cwd) + "\0" + terminal, 20))


def project_pointer_path(session, directory=None):
    """Root-independent record of the project a session's hooks ran in: a save run
    from another cwd (for example a scratch dir holding the draft) cannot find the
    cwd-keyed session note, but it can find this."""
    return os.path.join(state_dir(directory),
                        "context-watch-project-%s.json" % _sha1(str(session), 20))


def read_project_pointer(session, max_age_s=POINTER_MAX_AGE_S, directory=None):
    """The recorded project, or '' unless the pointer is trustworthy. It lives at a
    predictable path in a shared temp dir, so accept only a bounded regular file
    (never a symlink) owned by this user with no group/other bits, naming a
    directory this user owns outside every temp base."""
    if not session:
        return ""
    try:
        fd = os.open(project_pointer_path(session, directory), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o077 or info.st_size > POINTER_MAX_BYTES):
                return ""
            raw = stream.read(POINTER_MAX_BYTES + 1)
            if len(raw) > POINTER_MAX_BYTES:
                return ""
            note = json.loads(raw.decode("utf-8"))
        project = note.get("project")
        if not (note.get("session_id") == session and isinstance(project, str)
                and time.time() - float(note.get("ts") or 0) <= max_age_s):
            return ""
        project = os.path.realpath(project)
        owner = os.stat(project)
        if stat.S_ISDIR(owner.st_mode) and owner.st_uid == os.getuid() and not under_temp_base(project):
            return project
    except Exception:
        pass
    return ""
