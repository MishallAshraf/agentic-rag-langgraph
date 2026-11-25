"""Step 13 - Who is asking, and what they may do.

Authentication (who): users with bcrypt-hashed passwords, and server-side
sessions. The browser holds only a random session token in an HttpOnly,
SameSite=Strict cookie: scripts cannot read it, other sites cannot send it,
and deleting its row signs the user out everywhere it was used.

Authorization (what), always checked in code, never by the model:
  - bots: a member may use only the bots granted to them; admins may use all
  - conversations: a thread belongs to the user who started it
  - documents: a bot's tools are bound to its own collection (Step 10), so
    access to a bot is access to exactly its documents and nothing else
  - notes: each user has their own notes folder per bot
  - quotas: a daily budget of turns and tokens per user

Audit log: one append-only row per security-relevant event (sign-ins,
refusals, turns, approvals, admin changes). Each row stores a hash of the
previous row, so editing or deleting a past row breaks the chain, and
`python -m engine.manage_users audit --verify` finds where. Message text is never
logged: only who, what, when and how much.
"""

import hashlib
import json
import os
import re
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite
import bcrypt

ROOT = Path(__file__).resolve().parent.parent
AUTH_DB = Path(os.getenv("AUTH_DB", str(ROOT / "data" / "auth.sqlite")))

USERNAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{2,31}$")  # also a folder name for notes
ROLES = ("admin", "member")
MIN_PASSWORD_CHARS = 10
MAX_PASSWORD_BYTES = 72  # bcrypt ignores anything longer, so refuse it instead
SESSION_TTL_S = 7 * 24 * 3600
QUOTA_TURNS = int(os.getenv("QUOTA_TURNS_PER_DAY", "100"))
QUOTA_TOKENS = int(os.getenv("QUOTA_TOKENS_PER_DAY", "300000"))

# Checked against when the username does not exist, so a wrong username takes
# as long as a wrong password and response times do not reveal who has an account.
_DUMMY_HASH = bcrypt.hashpw(b"no such user", bcrypt.gensalt(rounds=12))

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    username TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('admin', 'member')),
    disabled INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,          -- sha256 of the token: a stolen database holds no usable tokens
    username TEXT NOT NULL REFERENCES users(username),
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS grants (
    username TEXT NOT NULL REFERENCES users(username),
    bot_id TEXT NOT NULL,
    PRIMARY KEY (username, bot_id)
);
CREATE TABLE IF NOT EXISTS threads (
    thread_id TEXT PRIMARY KEY,
    bot_id TEXT NOT NULL,
    username TEXT NOT NULL REFERENCES users(username),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS usage (
    username TEXT NOT NULL,
    day TEXT NOT NULL,
    turns INTEGER NOT NULL DEFAULT 0,
    tokens INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (username, day)
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    bot_id TEXT,
    thread_id TEXT,
    detail TEXT,
    ip TEXT,
    prev_hash TEXT NOT NULL,
    hash TEXT NOT NULL
);
"""

GENESIS = "0" * 64


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def check_username(username: str):
    if not USERNAME_PATTERN.fullmatch(username):
        raise ValueError("username must be 3-32 characters: lowercase letters, digits, underscores, "
                         "starting with a letter")


def check_password(password: str):
    if len(password) < MIN_PASSWORD_CHARS:
        raise ValueError(f"password must have at least {MIN_PASSWORD_CHARS} characters")
    if len(password.encode()) > MAX_PASSWORD_BYTES:
        raise ValueError(f"password must be at most {MAX_PASSWORD_BYTES} bytes")


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def row_hash(prev_hash: str, fields: dict) -> str:
    return hashlib.sha256((prev_hash + json.dumps(fields, sort_keys=True)).encode()).hexdigest()


@dataclass(frozen=True)
class User:
    username: str
    role: str

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


class AuthStore:
    def __init__(self, path: Path = AUTH_DB):
        self.path = Path(path)
        self.db: aiosqlite.Connection | None = None

    async def open(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = await aiosqlite.connect(self.path, isolation_level=None)  # autocommit; explicit BEGIN below
        self.db.row_factory = aiosqlite.Row
        await self.db.execute("PRAGMA journal_mode=WAL")  # the API and the CLI may use it at the same time
        await self.db.execute("PRAGMA foreign_keys=ON")
        await self.db.executescript(SCHEMA)
        return self

    async def close(self):
        if self.db:
            await self.db.close()

    async def _one(self, sql: str, *args):
        async with self.db.execute(sql, args) as cur:
            return await cur.fetchone()

    async def _all(self, sql: str, *args):
        async with self.db.execute(sql, args) as cur:
            return await cur.fetchall()

    # ---------------------------------------------------------------- users

    async def create_user(self, username: str, password: str, role: str = "member"):
        check_username(username)
        check_password(password)
        if role not in ROLES:
            raise ValueError(f"role must be one of {ROLES}")
        hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12)).decode()
        try:
            await self.db.execute("INSERT INTO users VALUES (?, ?, ?, 0, ?)", (username, hashed, role, now_iso()))
        except aiosqlite.IntegrityError:
            raise ValueError(f"user '{username}' already exists") from None

    async def set_password(self, username: str, password: str):
        check_password(password)
        hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12)).decode()
        await self._require_user(username)
        await self.db.execute("UPDATE users SET password_hash = ? WHERE username = ?", (hashed, username))
        await self.end_sessions(username)  # a new password signs out every old session

    async def set_disabled(self, username: str, disabled: bool):
        await self._require_user(username)
        await self.db.execute("UPDATE users SET disabled = ? WHERE username = ?", (int(disabled), username))
        if disabled:
            await self.end_sessions(username)

    async def _require_user(self, username: str):
        if not await self._one("SELECT 1 FROM users WHERE username = ?", username):
            raise ValueError(f"no user '{username}'")

    async def authenticate(self, username: str, password: str) -> User | None:
        row = await self._one("SELECT * FROM users WHERE username = ?", username)
        candidate = password.encode()[:MAX_PASSWORD_BYTES]
        if row is None:
            bcrypt.checkpw(candidate, _DUMMY_HASH)  # same work as a real check
            return None
        if not bcrypt.checkpw(candidate, row["password_hash"].encode()) or row["disabled"]:
            return None
        return User(row["username"], row["role"])

    async def list_users(self) -> list[dict]:
        rows = await self._all("SELECT username, role, disabled, created_at FROM users ORDER BY username")
        result = []
        for r in rows:
            bots = [g["bot_id"] for g in await self._all(
                "SELECT bot_id FROM grants WHERE username = ? ORDER BY bot_id", r["username"])]
            result.append({**dict(r), "bots": bots})
        return result

    # ---------------------------------------------------------------- sessions

    async def start_session(self, username: str) -> str:
        token = secrets.token_urlsafe(32)
        await self.db.execute("DELETE FROM sessions WHERE expires_at < ?", (time.time(),))
        await self.db.execute("INSERT INTO sessions VALUES (?, ?, ?)",
                              (hash_token(token), username, time.time() + SESSION_TTL_S))
        return token

    async def user_for_session(self, token: str | None) -> User | None:
        if not token:
            return None
        row = await self._one(
            "SELECT u.username, u.role FROM sessions s JOIN users u ON u.username = s.username "
            "WHERE s.token_hash = ? AND s.expires_at > ? AND u.disabled = 0", hash_token(token), time.time())
        return User(row["username"], row["role"]) if row else None

    async def end_session(self, token: str):
        await self.db.execute("DELETE FROM sessions WHERE token_hash = ?", (hash_token(token),))

    async def end_sessions(self, username: str):
        await self.db.execute("DELETE FROM sessions WHERE username = ?", (username,))

    # ---------------------------------------------------------------- bots

    async def grant(self, username: str, bot_id: str):
        await self._require_user(username)
        await self.db.execute("INSERT OR IGNORE INTO grants VALUES (?, ?)", (username, bot_id))

    async def revoke(self, username: str, bot_id: str):
        await self.db.execute("DELETE FROM grants WHERE username = ? AND bot_id = ?", (username, bot_id))

    async def can_use(self, user: User, bot_id: str) -> bool:
        if user.is_admin:
            return True
        return await self._one("SELECT 1 FROM grants WHERE username = ? AND bot_id = ?",
                               user.username, bot_id) is not None

    # ---------------------------------------------------------------- conversations

    async def add_thread(self, thread_id: str, bot_id: str, username: str):
        await self.db.execute("INSERT INTO threads VALUES (?, ?, ?, ?)", (thread_id, bot_id, username, now_iso()))

    async def owns_thread(self, user: User, bot_id: str, thread_id: str) -> bool | None:
        """True if it is theirs, False if it is someone else's, None if there is no such thread."""
        row = await self._one("SELECT bot_id, username FROM threads WHERE thread_id = ?", thread_id)
        if row is None:
            return None
        return row["bot_id"] == bot_id and row["username"] == user.username

    async def threads_for(self, username: str, bot_id: str) -> list[dict]:
        rows = await self._all("SELECT thread_id, created_at FROM threads WHERE username = ? AND bot_id = ? "
                               "ORDER BY created_at DESC", username, bot_id)
        return [dict(r) for r in rows]

    # ---------------------------------------------------------------- quotas

    async def usage_today(self, username: str) -> dict:
        row = await self._one("SELECT turns, tokens FROM usage WHERE username = ? AND day = ?", username, today())
        turns, tokens = (row["turns"], row["tokens"]) if row else (0, 0)
        return {"turns": turns, "tokens": tokens, "turn_limit": QUOTA_TURNS, "token_limit": QUOTA_TOKENS}

    async def over_quota(self, username: str) -> str | None:
        u = await self.usage_today(username)
        if u["turns"] >= u["turn_limit"]:
            return f"Daily limit of {u['turn_limit']} messages reached. It resets at midnight UTC."
        if u["tokens"] >= u["token_limit"]:
            return f"Daily limit of {u['token_limit']:,} tokens reached. It resets at midnight UTC."
        return None

    async def add_usage(self, username: str, turns: int, tokens: int):
        await self.db.execute(
            "INSERT INTO usage VALUES (?, ?, ?, ?) ON CONFLICT (username, day) "
            "DO UPDATE SET turns = turns + excluded.turns, tokens = tokens + excluded.tokens",
            (username, today(), turns, tokens))

    # ---------------------------------------------------------------- audit log

    async def audit(self, actor: str, action: str, *, bot_id: str | None = None, thread_id: str | None = None,
                    detail: dict | None = None, ip: str | None = None):
        fields = {"ts": now_iso(), "actor": actor, "action": action, "bot_id": bot_id, "thread_id": thread_id,
                  "detail": json.dumps(detail, sort_keys=True) if detail else None, "ip": ip}
        # IMMEDIATE: take the write lock before reading the last hash, so two
        # writers (the API and the CLI) cannot both chain onto the same row.
        await self.db.execute("BEGIN IMMEDIATE")
        try:
            last = await self._one("SELECT hash FROM audit ORDER BY id DESC LIMIT 1")
            prev = last["hash"] if last else GENESIS
            await self.db.execute(
                "INSERT INTO audit (ts, actor, action, bot_id, thread_id, detail, ip, prev_hash, hash) "
                "VALUES (:ts, :actor, :action, :bot_id, :thread_id, :detail, :ip, :prev_hash, :hash)",
                {**fields, "prev_hash": prev, "hash": row_hash(prev, fields)})
            await self.db.execute("COMMIT")
        except Exception:
            await self.db.execute("ROLLBACK")
            raise

    async def audit_entries(self, limit: int = 50) -> list[dict]:
        rows = await self._all("SELECT id, ts, actor, action, bot_id, thread_id, detail, ip FROM audit "
                               "ORDER BY id DESC LIMIT ?", limit)
        return [{**dict(r), "detail": json.loads(r["detail"]) if r["detail"] else None} for r in rows]

    async def verify_audit(self) -> tuple[bool, int, int | None]:
        """(intact, rows checked, id of the first row that does not match)."""
        prev, count = GENESIS, 0
        for r in await self._all("SELECT * FROM audit ORDER BY id"):
            fields = {k: r[k] for k in ("ts", "actor", "action", "bot_id", "thread_id", "detail", "ip")}
            if r["prev_hash"] != prev or r["hash"] != row_hash(prev, fields):
                return False, count, r["id"]
            prev, count = r["hash"], count + 1
        return True, count, None
