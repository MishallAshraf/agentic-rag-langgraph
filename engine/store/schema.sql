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

-- Bot builds (Lampus Phase 1, Step 8). Until then a build lived in the memory of
-- the API copy that started it: invisible to other copies, lost if that copy
-- stopped. Now the build and its progress events are rows; the work itself runs
-- in a worker process (engine/worker.py), handed over through a Redis queue.
CREATE TABLE IF NOT EXISTS builds (
    id        TEXT PRIMARY KEY,
    bot_id    TEXT NOT NULL,
    use_case  TEXT NOT NULL,
    folder    TEXT NOT NULL,                -- uploads, relative to the project root
    files     JSONB NOT NULL,
    by_user   TEXT NOT NULL,
    -- queued -> running -> review -> deciding -> saved | rejected; or failed
    status    TEXT NOT NULL,
    thread    TEXT,                         -- the builder graph's checkpoint thread (one per attempt)
    attempts  INT NOT NULL DEFAULT 0,       -- times a worker started it (a crashed worker's job is retried)
    worker    TEXT,
    proposal  JSONB,
    problems  JSONB NOT NULL DEFAULT '[]',
    saved     JSONB NOT NULL DEFAULT '[]',
    started   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS builds_started ON builds (started DESC);

CREATE TABLE IF NOT EXISTS build_events (
    build_id TEXT NOT NULL REFERENCES builds (id) ON DELETE CASCADE,
    n        INT NOT NULL,                  -- 1, 2, 3...: a page that reconnects asks for "after n"
    event    JSONB NOT NULL,
    PRIMARY KEY (build_id, n)
);
