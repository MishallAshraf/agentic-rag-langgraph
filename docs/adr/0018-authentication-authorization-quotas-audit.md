# 0018. Server-side sessions, per-bot grants, private conversations, daily quotas, hash-chained audit log

- Status: Accepted
- Date: 2026-11-24
- Step: 13

## Context

After Step 12, anyone who could reach the server could:
- use every bot, and so search every bot's documents
- read any conversation whose id they knew
- spend unlimited model credit
- leave no trace

ADR 0016 named the gaps: no authentication, rate limits keyed by IP address, and the server held to localhost.

A chatbot creator serves many people and many bots, so Step 13 has to answer four questions in code:
- **Authentication:** who is asking?
- **Authorization:** what may they reach?
- **Quotas:** how much may they spend?
- **Audit:** what happened, and can that record be trusted?

## Decision

**Accounts** are in `data/auth.sqlite`, managed by `auth.py` and the `manage_users.py` command line.

**Users and passwords:**
- Passwords are hashed with bcrypt (cost 12). They must be 10–72 bytes, because bcrypt silently ignores bytes beyond 72.
- An unknown username is checked against a dummy hash, so response time does not reveal which accounts exist.
- Wrong username and wrong password get the same reply.
- Accounts are created by an admin. There is no self sign-up.
- The CLI asks for passwords with hidden input, never as arguments, which would land in shell history.

**Sessions:**
- A random 256-bit token, stored only as its SHA-256 hash, so a copy of the database holds no usable sessions.
- Sent in a cookie that is `HttpOnly` (scripts cannot read it), `SameSite=Strict` (other sites cannot send it) and `Secure` behind HTTPS.
- Sign-out deletes the row, so a replayed token stops working at once. Changing a password or disabling a user ends all their sessions.
- **No `allow_credentials` in CORS:** a cross-origin page can never send the session cookie.

**Authorization, checked on every request:**

| Resource | Rule |
|---|---|
| Bots | Members use only bots granted to them; admins use all. |
| Documents | Each bot's tools are bound to its own collection (ADR 0015), so a grant to a bot is a grant to exactly its documents. |
| Conversations | A thread belongs to the user who created it. Others get `404`, as for a thread that does not exist. |
| Notes | One folder per bot and user. A bot with per-user tools gets one graph per user, with that user's folder. |
| Admin routes | `GET /admin/audit` requires the admin role (`403` otherwise). |

A bot that isn't granted and a bot that doesn't exist get the same `404`, so nobody can discover other bots or conversations. Each such refusal is audited.

**Quotas and limits, per user:**
- **Daily quotas:** turns (counted when a turn starts, so many parallel chats cannot overrun the limit) and tokens (counted when the turn ends). Defaults are 100 turns and 300,000 tokens per day. A breach gets `429` with a reason, and is audited.
- **Token counting:** `UsageMetadataCallbackHandler` sees every model call in a turn, including the guard, the grader inside the search tool and the fact checker.
- **Rate limit:** the per-minute limit from Step 11 is now keyed by user, not IP address.
- **Sign-in attempts:** at most 5 failures per address and username per minute. Successful sign-ins don't count.

**Audit log**, one row per security-relevant event:
- sign-in, failed sign-in, blocked sign-in, sign-out
- access denied, quota exceeded
- turn start (message length) and turn end (result, tokens, guard verdict)
- approval decisions (decision and tool names)
- admin changes from the CLI

**Message text and tool arguments are never logged.** The log records who, what, when and how much. That is enough to investigate an incident without becoming a second copy of everyone's conversations.

**Each audit row carries the hash of the previous row plus its own content.** `audit --verify` and `GET /admin/audit` recompute the chain, so a changed or deleted row is detected. Writes take SQLite's write lock before reading the last hash, so the API and the CLI cannot fork the chain.

## Alternatives considered

- **JWTs in `localStorage` with an `Authorization` header.** Stateless and common, but:
  - any injected script can read the token
  - a token can't be revoked before it expires without a server-side list, which brings back sessions anyway

  The server here is stateful already (checkpoints), so revocable server-side sessions cost nothing extra. JWTs fit better between services, or when a separate identity provider issues the tokens.
- **An external identity provider (OAuth/OIDC, for example Auth0, Entra ID or Keycloak).** This is the right end state for real users: single sign-on, multi-factor authentication, password resets. Building it locally first shows what an identity provider takes off your hands. The session layer stays the same; only the sign-in route would change.
- **Document-level access control inside one collection** (a metadata filter by user group on every search). Needed when one bot must show different documents to different people. It complicates retrieval: the filter must apply to vector search and BM25 alike, the cache key must include the groups, and every new query path must remember the filter. One collection per bot (ADR 0015) gives the same isolation structurally. Revisit when a single bot needs restricted documents.
- **Logging full messages for audit.** Useful for debugging, but a privacy liability and a breach target. Traces in Langfuse (ADR 0012) already serve debugging, with their own retention.
- **A signed or external audit store (WORM storage, a SIEM).** Stronger than a hash chain: a chain only detects edits. Anyone with write access can still rewrite the whole chain, or drop rows from its end. Publishing the latest hash elsewhere, or shipping events to a write-once store, closes that gap.

## Consequences

**Tested:**
- 13 automated tests in `tests/test_auth.py`, none of which needs the LLM: public and protected routes, identical failures, cookie flags, hashed tokens, blocking of repeated failures, revocation on sign-out and on disabling, bot visibility per role, `404` for ungranted bots, private conversations (read, message and approve), admin-only routes, per-user notes folders, the quota refusal, and detection of an edited audit row.
- **End to end in the browser:**
  - sign-in, with a wrong password first
  - a member sees only the granted bot, and a direct link to another bot says it doesn't exist
  - `document.cookie` is empty, because the session cookie is invisible to scripts
  - sign-out survives a reload
  - the audit trail recorded all of it, and the chain verifies
- **Token counting:** all 5 model calls in a turn were counted, including the grader inside the search tool (778 of 5,466 tokens).

**Migration:** conversations started before this step have no owner, so the API hides them. The terminal agents can still open them. Existing notes in `notes/study_assistant/` stay with the terminal user; API users get `notes/study_assistant/<username>/`.

**Limits to revisit:**
- **The server still listens on localhost.** Exposing it needs HTTPS (`API_COOKIE_SECURE=true`) and a reverse proxy, and then the client address must come from a trusted proxy header.
- **Rate limits and graph caches live in memory in one process.** A per-user graph is cached for each user of a notes bot, so many users mean many graphs; an LRU limit or stateless tools would be needed.
- **No password reset, multi-factor authentication or account lockout notification.** These belong with an identity provider.
- **Quotas count usage but not money.** Tokens are a proxy; a cost-weighted quota would use per-model prices (`evals/common.PRICES`).
- **The audit log has no retention policy or export.**
