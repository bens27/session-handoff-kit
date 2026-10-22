#!/usr/bin/env python3
"""handoff-record: one checkpoint file, read once, asked questions.

A checkpoint record owns everything that decides whether a checkpoint is
eligible to adopt: its front matter (parsed by handoff_identity), the status
machine, the content fingerprint, the publication receipt and its seal. Callers
ask the record (open? published? trusted? paused? live?) instead of reading raw
front-matter keys, so the answer has one owner.

Stdlib + handoff_identity only. The ledger (claims, scans, resolve) is imported
lazily where a question needs the whole chain, so hooks that must survive a
missing sibling can still import this module.
"""

import json
import os

from handoff_identity import parse_front_matter

CLOSED_STATUSES = ("resumed", "superseded", "abandoned", "unpublished")
VOLATILE_KEYS = ('claimed:', 'claim_owner:', 'status:', 'resumed:', 'resumed_by:', 'transfer_receipt:',
                 'superseded:', 'superseded_by:', 'abandoned:')


def is_open(fm):
    """Lenient status: anything not closed (missing or unknown counts as open)."""
    return (fm.get("status") or "open").lower() not in CLOSED_STATUSES


def content_fingerprint(text):
    import hashlib
    fm = parse_front_matter(text)
    lines = text.splitlines()
    if fm:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == '---')
        lines = [l for l in lines[1:end] if not l.startswith(VOLATILE_KEYS)] + lines[end + 1:]
    return hashlib.sha256('\n'.join(lines).encode()).hexdigest()


def _read_receipt(path):
    with open(path + '.published') as f:
        return json.loads(f.read(4096))


def publication_valid(path, text, fm):
    try:
        receipt = _read_receipt(path)
        return (isinstance(receipt, dict) and receipt.get('checkpoint_id') == fm.get('checkpoint_id')
                and receipt.get('fingerprint') == content_fingerprint(text))
    except (OSError, ValueError):
        return False


def _receipt_key(create=False):
    """Per-user secret outside any repo (~/.context-watch/receipt.key, 0600), or b''.
    A cloned repo cannot read it, so it cannot forge a checkpoint receipt."""
    path = os.path.join(os.path.expanduser("~"), ".context-watch", "receipt.key")
    for attempt in (0, 1):
        try:
            with open(path, "rb") as f:
                key = f.read(256)
            if len(key) >= 32:
                return key
        except OSError:
            pass
        if not create or attempt:
            break
        try:
            os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(os.urandom(32))
        except FileExistsError:
            pass  # a concurrent first use won; read its key
        except OSError:
            break
    return b""


def _mac(key, checkpoint_id, fingerprint):
    import hmac
    import hashlib
    return hmac.new(key, ("%s\0%s" % (checkpoint_id, fingerprint)).encode(), hashlib.sha256).hexdigest()


def seal(checkpoint_id, fingerprint):
    """The publication receipt this machine writes: id, content fingerprint, HMAC under the local key."""
    key = _receipt_key(create=True)
    return dict(checkpoint_id=checkpoint_id, fingerprint=fingerprint,
                mac=_mac(key, checkpoint_id, fingerprint) if key else "")


def valid_seal(receipt, checkpoint_id, fingerprint):
    import hmac
    key = _receipt_key()
    return bool(key and isinstance(receipt, dict) and isinstance(receipt.get('mac'), str)
                and receipt.get('checkpoint_id') == checkpoint_id
                and receipt.get('fingerprint') == fingerprint
                and hmac.compare_digest(receipt['mac'], _mac(key, checkpoint_id, fingerprint)))


def trusted(path, text=None, fm=None):
    """True only when this machine's own save published exactly this content.
    Legacy, hand-written, cloned or edited checkpoints are untrusted: their
    verify command and out-of-project references need explicit confirmation."""
    import hmac
    try:
        if text is None:
            with open(path, encoding="utf-8", errors="replace") as f:
                text = f.read()
        fm = fm if fm is not None else parse_front_matter(text)
        receipt = _read_receipt(path)
        key = _receipt_key()
        mac = receipt.get("mac")
        return bool(key and isinstance(mac, str) and fm.get("checkpoint_id")
                    and receipt.get("checkpoint_id") == fm["checkpoint_id"]
                    and receipt.get("fingerprint") == content_fingerprint(text)
                    and hmac.compare_digest(mac, _mac(key, fm["checkpoint_id"], receipt["fingerprint"])))
    except (OSError, ValueError, AttributeError, StopIteration):
        return False


class Record:
    """A checkpoint as read from disk. `fm` is the parsed front matter; ledger
    scans may adjust its status (unpublished/superseded) in place, and every
    question below sees that adjustment."""

    __slots__ = ("path", "text", "fm")

    def __init__(self, path, text, fm=None):
        self.path = path
        self.text = text
        self.fm = parse_front_matter(text) if fm is None else fm

    @classmethod
    def load(cls, path):
        with open(path, encoding="utf-8", errors="replace") as f:
            return cls(path, f.read())

    @classmethod
    def load_trusted(cls, path):
        """The record when this machine sealed exactly these bytes, else None
        (unreadable, unpublished, edited, cloned). One read: the trust check
        and the fields the caller inherits describe the same content."""
        try:
            record = cls.load(path)
        except (OSError, ValueError):
            return None
        return record if record.trusted() else None

    @staticmethod
    def set_fields(path, **fields):
        """Replace these front-matter keys atomically (the ledger's rewrite)."""
        import handoff_ledger as ledger
        ledger._set_fields(path, list(fields.items()), tuple('%s:' % key for key in fields))

    @staticmethod
    def render(body="## Objective\nx\n\n## Current state\nx\n\n## Next steps\nx\n", **fields):
        """Checkpoint text from fields, for tests and tools that build records."""
        head = "".join("%s: %s\n" % kv for kv in fields.items())
        return "---\n%s---\n%s" % (head, body)

    # -- keys -------------------------------------------------------------
    def get(self, key, default=None):
        value = self.fm.get(key)
        return default if value is None else value

    def has(self, key):
        return bool(self.fm.get(key))

    @property
    def status(self):
        return (self.fm.get("status") or "open").lower()

    checkpoint_id = property(lambda self: self.fm.get("checkpoint_id"))
    topic = property(lambda self: self.fm.get("topic"))
    session_id = property(lambda self: self.fm.get("session_id"))
    terminal = property(lambda self: self.fm.get("terminal"))
    project = property(lambda self: self.fm.get("project"))
    verify = property(lambda self: self.fm.get("verify"))
    runner_id = property(lambda self: self.fm.get("runner_id"))
    claim_owner = property(lambda self: self.fm.get("claim_owner"))
    predecessor = property(lambda self: self.fm.get("predecessor"))
    paused = property(lambda self: self.fm.get("automatic_paused") or "")
    waiting_input = property(lambda self: self.fm.get("waiting_input") or "")

    @property
    def held(self):
        """A human hold: waiting for input or automatically paused."""
        return bool(self.fm.get("waiting_input") or self.fm.get("automatic_paused"))

    @property
    def continuity(self):
        """Parsed continuity items; raises ValueError on malformed JSON."""
        return json.loads(self.fm.get("continuity") or "[]")

    # -- status machine ---------------------------------------------------
    @property
    def is_open(self):
        return is_open(self.fm)

    @property
    def open_strict(self):
        """Status literally 'open' (a scan may have downgraded it to unpublished)."""
        return self.fm.get("status") == "open"

    # -- seal / publication -----------------------------------------------
    @property
    def fingerprint(self):
        return content_fingerprint(self.text)

    def published(self):
        return publication_valid(self.path, self.text, self.fm)

    def has_receipt(self):
        return os.path.exists(self.path + ".published")

    def trusted(self):
        return trusted(self.path, self.text, self.fm)

    # -- chain ------------------------------------------------------------
    def authoritative(self, root):
        """True when this file is the newest record of its topic under root,
        compared on canonical paths so a symlinked root does not split it."""
        import handoff_ledger as ledger
        newest = ledger.resolve(self.path, root).get("authoritative")
        return newest is not None and os.path.realpath(newest) == os.path.realpath(self.path)


def live(root, path):
    """True while a checkpoint is open and authoritative, i.e. an intent still guards it.
    Unreadable but present counts as live: keep the guard."""
    try:
        record = Record.load(path)
        return record.open_strict and record.authoritative(root)
    except (OSError, ValueError):
        return os.path.exists(path)
