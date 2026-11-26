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

Storage: Postgres through the shared connection pool in engine/db.py (Lampus
Phase 1, Step 3; before that, one SQLite connection shared by every request).
Each method borrows a connection for one unit of work; writes that must happen
together run in one transaction. Two rules keep concurrent requests consistent
(course topic 6):
  - a turn is reserved against the daily quota in ONE conditional statement, so
    two parallel conversations can't both take the last turn;
  - audit rows are chained under a transaction-scoped lock, so two writers
    can't both link to the same previous row.
"""

import hashlib
import json
import os
import re
import secrets
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import bcrypt
from psycopg import errors
from psycopg_pool import AsyncConnectionPool

from engine.db import DATABASE_URL, open_pool

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

GENESIS = "0" * 64
AUDIT_LOCK = "hashtext('lampus.audit')"  # one writer at a time extends the chain


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def today() -> date:
    return datetime.now(timezone.utc).date()


def _iso(value) -> str:
    return value.isoformat(timespec="seconds") if isinstance(value, datetime) else value


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
    def __init__(self, url: str = DATABASE_URL):
        self.url = url
        self.pool: AsyncConnectionPool | None = None

    async def open(self):
        self.pool = await open_pool(self.url)
        return self

    async def close(self):
        if self.pool:
            await self.pool.close()

    @staticmethod
    def _params(args: tuple):
        """Positional values for %s, or one dict for %(name)s."""
        return args[0] if len(args) == 1 and isinstance(args[0], dict) else args

    async def _run(self, sql: str, *args):
        async with self.pool.connection() as conn:
            await conn.execute(sql, self._params(args))

    async def _one(self, sql: str, *args):
        async with self.pool.connection() as conn:
            return await (await conn.execute(sql, self._params(args))).fetchone()

    async def _all(self, sql: str, *args):
        async with self.pool.connection() as conn:
            return await (await conn.execute(sql, self._params(args))).fetchall()

    # ---------------------------------------------------------------- users

    async def create_user(self, username: str, password: str, role: str = "member"):
        check_username(username)
        check_password(password)
        if role not in ROLES:
            raise ValueError(f"role must be one of {ROLES}")
        hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12)).decode()
        try:
            await self._run("INSERT INTO users (username, password_hash, role) VALUES (%s, %s, %s)",
                            username, hashed, role)
        except errors.UniqueViolation:
            raise ValueError(f"user '{username}' already exists") from None

    async def set_password(self, username: str, password: str):
        check_password(password)
        hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12)).decode()
        await self._require_user(username)
        async with self.pool.connection() as conn, conn.transaction():
            await conn.execute("UPDATE users SET password_hash = %s WHERE username = %s", (hashed, username))
            await conn.execute("DELETE FROM sessions WHERE username = %s", (username,))  # sign out everywhere

    async def set_disabled(self, username: str, disabled: bool):
        await self._require_user(username)
        async with self.pool.connection() as conn, conn.transaction():
            await conn.execute("UPDATE users SET disabled = %s WHERE username = %s", (disabled, username))
            if disabled:
                await conn.execute("DELETE FROM sessions WHERE username = %s", (username,))

    async def _require_user(self, username: str):
        if not await self._one("SELECT 1 FROM users WHERE username = %s", username):
            raise ValueError(f"no user '{username}'")

    async def authenticate(self, username: str, password: str) -> User | None:
        row = await self._one("SELECT * FROM users WHERE username = %s", username)
        candidate = password.encode()[:MAX_PASSWORD_BYTES]
        if row is None:
            bcrypt.checkpw(candidate, _DUMMY_HASH)  # same work as a real check
            return None
        if not bcrypt.checkpw(candidate, row["password_hash"].encode()) or row["disabled"]:
            return None
        return User(row["username"], row["role"])

    async def list_users(self) -> list[dict]:
        rows = await self._all(
            "SELECT u.username, u.role, u.disabled::int AS disabled, u.created_at, "
            "COALESCE(array_agg(g.bot_id ORDER BY g.bot_id) FILTER (WHERE g.bot_id IS NOT NULL), '{}') AS bots "
            "FROM users u LEFT JOIN grants g ON g.username = u.username "
            "GROUP BY u.username ORDER BY u.username")
        return [{**r, "created_at": _iso(r["created_at"]), "bots": list(r["bots"])} for r in rows]

    # ---------------------------------------------------------------- sessions

    async def start_session(self, username: str) -> str:
        token = secrets.token_urlsafe(32)
        async with self.pool.connection() as conn, conn.transaction():
            await conn.execute("DELETE FROM sessions WHERE expires_at < now()")
            await conn.execute("INSERT INTO sessions VALUES (%s, %s, now() + %s)",
                               (hash_token(token), username, timedelta(seconds=SESSION_TTL_S)))
        return token

    async def user_for_session(self, token: str | None) -> User | None:
        if not token:
            return None
        row = await self._one(
            "SELECT u.username, u.role FROM sessions s JOIN users u ON u.username = s.username "
            "WHERE s.token_hash = %s AND s.expires_at > now() AND NOT u.disabled", hash_token(token))
        return User(row["username"], row["role"]) if row else None

    async def end_session(self, token: str):
        await self._run("DELETE FROM sessions WHERE token_hash = %s", hash_token(token))

    async def end_sessions(self, username: str):
        await self._run("DELETE FROM sessions WHERE username = %s", username)

    # ---------------------------------------------------------------- bots

    async def grant(self, username: str, bot_id: str):
        await self._require_user(username)
        await self._run("INSERT INTO grants VALUES (%s, %s) ON CONFLICT DO NOTHING", username, bot_id)

    async def revoke(self, username: str, bot_id: str):
        await self._run("DELETE FROM grants WHERE username = %s AND bot_id = %s", username, bot_id)

    async def can_use(self, user: User, bot_id: str) -> bool:
        if user.is_admin:
            return True
        return await self._one("SELECT 1 FROM grants WHERE username = %s AND bot_id = %s",
                               user.username, bot_id) is not None

    # ---------------------------------------------------------------- conversations

    async def add_thread(self, thread_id: str, bot_id: str, username: str):
        await self._run("INSERT INTO threads (thread_id, bot_id, username) VALUES (%s, %s, %s)",
                        thread_id, bot_id, username)

    async def owns_thread(self, user: User, bot_id: str, thread_id: str) -> bool | None:
        """True if it is theirs, False if it is someone else's, None if there is no such thread."""
        row = await self._one("SELECT bot_id, username FROM threads WHERE thread_id = %s", thread_id)
        if row is None:
            return None
        return row["bot_id"] == bot_id and row["username"] == user.username

    async def threads_for(self, username: str, bot_id: str) -> list[dict]:
        rows = await self._all("SELECT thread_id, created_at FROM threads WHERE username = %s AND bot_id = %s "
                               "ORDER BY created_at DESC", username, bot_id)
        return [{**r, "created_at": _iso(r["created_at"])} for r in rows]

    # ---------------------------------------------------------------- quotas

    async def usage_today(self, username: str) -> dict:
        row = await self._one("SELECT turns, tokens FROM usage WHERE username = %s AND day = %s", username, today())
        turns, tokens = (row["turns"], row["tokens"]) if row else (0, 0)
        return {"turns": turns, "tokens": tokens, "turn_limit": QUOTA_TURNS, "token_limit": QUOTA_TOKENS}

    @staticmethod
    def _quota_message(usage: dict) -> str | None:
        if usage["turns"] >= usage["turn_limit"]:
            return f"Daily limit of {usage['turn_limit']} messages reached. It resets at midnight UTC."
        if usage["tokens"] >= usage["token_limit"]:
            return f"Daily limit of {usage['token_limit']:,} tokens reached. It resets at midnight UTC."
        return None

    async def over_quota(self, username: str) -> str | None:
        """A quick early check, before any work. Not enough on its own: see reserve_turn."""
        return self._quota_message(await self.usage_today(username))

    async def reserve_turn(self, username: str) -> str | None:
        """Count one turn, only if the user is still under quota. None if reserved, else the reason.

        Checking in one statement and adding in another lets two parallel conversations
        both see "one turn left" and both go ahead. Here the check is the WHERE of the
        write itself: Postgres locks the row, so the second request sees the first one's turn.
        """
        row = await self._one(
            "INSERT INTO usage (username, day, turns, tokens) "
            "SELECT %(u)s, %(d)s, 1, 0 WHERE %(turns)s > 0 "
            "ON CONFLICT (username, day) DO UPDATE SET turns = usage.turns + 1 "
            "WHERE usage.turns < %(turns)s AND usage.tokens < %(tokens)s "
            "RETURNING turns",
            {"u": username, "d": today(), "turns": QUOTA_TURNS, "tokens": QUOTA_TOKENS})
        if row:
            return None
        return self._quota_message(await self.usage_today(username)) or "Daily limit reached."

    async def add_usage(self, username: str, turns: int, tokens: int):
        await self._run(
            "INSERT INTO usage VALUES (%s, %s, %s, %s) ON CONFLICT (username, day) "
            "DO UPDATE SET turns = usage.turns + excluded.turns, tokens = usage.tokens + excluded.tokens",
            username, today(), turns, tokens)

    # ---------------------------------------------------------------- audit log

    async def audit(self, actor: str, action: str, *, bot_id: str | None = None, thread_id: str | None = None,
                    detail: dict | None = None, ip: str | None = None):
        fields = {"ts": now_iso(), "actor": actor, "action": action, "bot_id": bot_id, "thread_id": thread_id,
                  "detail": json.dumps(detail, sort_keys=True) if detail else None, "ip": ip}
        # Own connection, own transaction. The lock lasts until COMMIT; concurrent
        # writers queue for it instead of reading the same "last hash".
        async with self.pool.connection() as conn, conn.transaction():
            await conn.execute(f"SELECT pg_advisory_xact_lock({AUDIT_LOCK})")
            last = await (await conn.execute("SELECT hash FROM audit ORDER BY id DESC LIMIT 1")).fetchone()
            prev = last["hash"] if last else GENESIS
            await conn.execute(
                "INSERT INTO audit (ts, actor, action, bot_id, thread_id, detail, ip, prev_hash, hash) "
                "VALUES (%(ts)s, %(actor)s, %(action)s, %(bot_id)s, %(thread_id)s, %(detail)s, %(ip)s, "
                "%(prev_hash)s, %(hash)s)",
                {**fields, "prev_hash": prev, "hash": row_hash(prev, fields)})

    async def audit_entries(self, limit: int = 50) -> list[dict]:
        rows = await self._all("SELECT id, ts, actor, action, bot_id, thread_id, detail, ip FROM audit "
                               "ORDER BY id DESC LIMIT %s", limit)
        return [{**r, "detail": json.loads(r["detail"]) if r["detail"] else None} for r in rows]

    async def verify_audit(self) -> tuple[bool, int, int | None]:
        """(intact, rows checked, id of the first row that does not match)."""
        prev, count = GENESIS, 0
        for r in await self._all("SELECT * FROM audit ORDER BY id"):
            fields = {k: r[k] for k in ("ts", "actor", "action", "bot_id", "thread_id", "detail", "ip")}
            if r["prev_hash"] != prev or r["hash"] != row_hash(prev, fields):
                return False, count, r["id"]
            prev, count = r["hash"], count + 1
        return True, count, None
