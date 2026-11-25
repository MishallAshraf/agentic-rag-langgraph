-- Users, sessions, grants, conversations, quotas and the audit log (Postgres).
-- Applied at start-up by engine/db.py; every statement is safe to run again.
--
-- Why SQL for this data (course topic 4): it is relational (grants and threads
-- point at users), every write must be all-or-nothing, and quotas and the audit
-- chain need strict ordering under concurrent requests (topic 6). Conversation
-- checkpoints, which are documents read and written whole, move in Step 4.

CREATE TABLE IF NOT EXISTS users (
    username      TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL CHECK (role IN ('admin', 'member')),
    disabled      BOOLEAN NOT NULL DEFAULT FALSE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,  -- sha256 of the token: a stolen database holds no usable tokens
    username   TEXT NOT NULL REFERENCES users (username) ON DELETE CASCADE,
    expires_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_username ON sessions (username);

CREATE TABLE IF NOT EXISTS grants (
    username TEXT NOT NULL REFERENCES users (username) ON DELETE CASCADE,
    bot_id   TEXT NOT NULL,
    PRIMARY KEY (username, bot_id)
);

CREATE TABLE IF NOT EXISTS threads (
    thread_id  TEXT PRIMARY KEY,
    bot_id     TEXT NOT NULL,
    username   TEXT NOT NULL REFERENCES users (username) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS threads_owner ON threads (username, bot_id, created_at DESC);

CREATE TABLE IF NOT EXISTS usage (
    username TEXT NOT NULL,
    day      DATE NOT NULL,
    turns    INTEGER NOT NULL DEFAULT 0 CHECK (turns >= 0),
    tokens   BIGINT NOT NULL DEFAULT 0 CHECK (tokens >= 0),
    PRIMARY KEY (username, day)
);

-- Append-only, hash-chained. ts and detail are kept as the exact text that was
-- hashed, so the chain can be re-checked byte for byte.
CREATE TABLE IF NOT EXISTS audit (
    id        BIGSERIAL PRIMARY KEY,
    ts        TEXT NOT NULL,
    actor     TEXT NOT NULL,
    action    TEXT NOT NULL,
    bot_id    TEXT,
    thread_id TEXT,
    detail    TEXT,
    ip        TEXT,
    prev_hash TEXT NOT NULL,
    hash      TEXT NOT NULL UNIQUE
);
