# 0032. The API under /v1, idempotency keys for resent requests, and signed webhooks to schools

- Status: Accepted
- Date: 2026-11-26
- Step: Lampus Phase 1, Step 10

## Context

Lampus's API was written for its own web app. Two kinds of new client are coming: phone apps on unreliable connections, and the schools' own systems.

Three problems follow (course topic 9, API design):

1. **Paths without a version.** The first incompatible change to a route would break every client at once.
2. **A resent message runs twice.** A phone sends a message, the answer comes back, the connection drops before the app sees the end of it, and the app sends the message again. The server can't tell a resend from a new message. Measured with 10 students who each resend one message (`loadtest/bench_idempotency.py`): all 10 messages ran twice, 20 turns were charged for 10 questions, and each question sits in its conversation twice.
3. **Schools must poll.** A school that wants to know when a bot is ready, or that a student's message was flagged as a crisis, has to keep asking. An HTTP POST from Lampus to the school (a webhook) fixes that. But the school's endpoint is public, so it must be able to tell Lampus's messages from anyone else's.

## Decision

**Versioning.**

- Every route is served under `/v1`. The old paths still work, for clients written before, but they answer with `Deprecation: true` and a `Link: </v1/...>; rel="successor-version"` header (the Deprecation header is RFC 9745).
- An incompatible change goes under `/v2`, while `/v1` keeps working until its clients have moved. Compatible changes, such as a new optional field, stay in `/v1`.
- The web app now calls `/v1`. `/health` stays unversioned, for load balancers.

**Idempotency keys** (`engine/idempotency.py`), on `POST .../threads` and `POST .../messages`:

- **The client side:** the client sends a random `Idempotency-Key` header with each new request, and the same key when resending it. The web app sends a new key with each message.
- **Claiming a key:** the first request with a key inserts a row into Postgres. The primary key `(username, key)` makes "first one wins" exact across API copies.
- **A resend:**
  - Same request, finished: it gets the stored final event back, marked `Idempotent-Replayed: true`. Nothing runs, nothing is charged, nothing is added to the conversation.
  - Same request, still running: 409, with `Retry-After: 5`.
  - The same key on a different request: 422.
- **A request that fails gives its key back,** so the resend runs normally. That covers an error, a refusal (quota, busy conversation) and a client gone mid-turn.
- **A running claim older than the longest turn** belongs to a copy that crashed, and can be taken over. Keys are kept for 24 hours.
- **Keyed on the user,** so one student's key can never return another student's answer.

**Signed webhooks** (`engine/webhooks.py`), following the Standard Webhooks spec v1.0.0:

- **Events, ids only, never message text:** `build.finished`, `build.decided`, and `conversation.flagged` (the safety check routed a message as a crisis).
- **Each message carries three headers:**
  - `webhook-id`: the same on every retry;
  - `webhook-timestamp`;
  - `webhook-signature`: `v1,` + base64(HMAC-SHA256(secret, `id.timestamp.body`)).
- **The secret** is 32 random bytes (`whsec_...`, from `python -m engine.webhooks secret`).
- **What a receiver checks,** with `verify()` plus a set of seen ids:
  - the signature, compared in constant time;
  - that the timestamp is within 5 minutes;
  - that the id hasn't been handled before.
- **Delivery goes through the Step 8 Redis queue.** A slow or broken school server never slows a turn. A worker retries failures on the spec's schedule (5 s, 5 min, 30 min, 2 h, 5 h, with jitter) and stops at 410 Gone.
- **Delivery is at least once:** a worker that dies mid-schedule hands the job to another, which starts again. That's why receivers drop repeated ids.

## Measurements

GitHub Codespace (2 cores), one API copy and one worker, the fake model.

**Resent messages**, 10 students each sending one message and then resending it with the same key:

| | Before: no keys | After: Idempotency-Key |
| --- | --- | --- |
| Messages in their conversation twice | 10 of 10 | 0 of 10 |
| Turns run | 20 | 10 |
| Turns charged | 20 | 10 |
| Resends answered | 10 (by running again) | 10 (played back) |

**Webhooks** (`loadtest/bench_webhooks.py`):

- **Traffic:** 50 genuine events went the real way (queue, worker, signing). The school's stand-in server was down for its first 3 seconds, so deliveries had to retry. An attacker who knows the URL but not the secret sent 200 messages: 50 forged, 50 genuine with the body changed, 50 genuine sent again, and 50 genuine-looking ones recorded 10 minutes earlier.
- **Judged by two receivers:**

| Receiver | Genuine events acted on | Attack messages acted on |
| --- | --- | --- |
| Accepts every POST (no checks) | 50 (plus 20 duplicates from the retries) | 200 of 200 |
| Checks signature, timestamp and id | 50 of 50 | 0 of 200 |

- **Retries:** delivering the 50 events took 70 attempts, because 20 hit the 3-second outage and succeeded on retry.

108 tests pass, including 13 new ones:

- **`/v1` and deprecation:** the old paths answer the same, with the deprecation headers.
- **Idempotency:** a resent new-conversation request returns the same conversation. A resent message is played back without being charged. A key in use gets 409, a reused key gets 422, and a refused request gives its key back.
- **Webhooks:** a genuine message passes. A changed body, another secret, a changed id or timestamp, and a message older than 5 minutes all fail. Both signatures pass during a secret rotation. Delivery retries until it arrives, with the same id each time, and stops at 410.

## Alternatives considered

- **Version in a header** (`Accept: application/vnd.lampus.v1+json`): keeps URLs clean, but it is invisible in logs, harder to try in a browser, and easy for a client to forget. A path prefix is what most public APIs use.
- **No versioning, only additive changes:** works until the first change that isn't additive, and then every client breaks at once.
- **Idempotency keys in Redis:** faster, but Redis here keeps nothing on disk (Step 7). A key lost in a restart would let a resend run twice, the exact failure this prevents. Postgres also makes the claim and its outcome durable together.
- **Deduplicating on the message text** instead of a key: a student may honestly ask the same question twice ("and again?"). Only the client knows whether a request is a resend.
- **Unsigned webhooks, or a shared token in the URL:** the 200-message attack above is accepted in full. A token in the URL also leaks into logs, and doesn't stop a replay or a changed body.
- **Asymmetric signatures** (the spec's `v2a`, Ed25519): the school only needs a public key, so a leaked receiver secret can't be used to forge messages. Worth it once there are many schools. HMAC with one secret per school is simpler to start with.
- **Delivering the webhook inside the turn:** a slow school server would delay the student's reply, and a down server would lose the event.

## Consequences

- Clients that resend safely must send keys. The web app does; old clients without keys behave as before.
- A finished turn's final event is stored for a day per key: a few hundred bytes each.
- Webhooks need a per-school URL and secret. For now there is one of each (`WEBHOOK_URL`, `WEBHOOK_SECRET`); per-school endpoints come with schools as tenants. The spec also says each endpoint should have its own secret.
- Receivers must keep the ids they have seen for at least the 5-minute window, or a replay inside that window gets through. The spec suggests Redis for this.
- The flagged-conversation webhook tells a school "this student, this conversation, crisis", never what was written. What the school does with it is its own safeguarding process.
