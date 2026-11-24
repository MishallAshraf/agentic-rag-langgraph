# 0020. The bot builder as a service: uploads, a background job with resumable progress, review in the browser, bots served without a restart

- Status: Accepted
- Date: 2026-11-24
- Step: 15

## Context

Step 10 built the bot builder ([ADR 0015](0015-bot-builder-and-multi-tenancy.md)), but only as a command line tool. To make a bot, someone had to copy documents into the project, run `step10_builder.py`, review the proposal in a terminal, then restart the API so it would load the new bot file.

The long-term goal ([ADR 0007](0007-platform-bots-as-configuration.md)) is a chatbot creator. Someone describes a use case, uploads documents and gets a bot, without a terminal. That raises four questions the command line never had to answer:

- **Uploads:** what may come in, and where does it go?
- **Long work:** a build takes one to a few minutes. A request that long breaks on a page reload or a proxy timeout.
- **Review:** the human approval step from Step 10 must stay, now in the browser.
- **Serving:** a new bot has to be usable without restarting the server.

## Decision

The same builder graph (`step10_builder.build_builder`) runs as a **background job** inside the API (`builds.py`, `BuildManager`):

| Route (admins only) | Does |
|---|---|
| `POST /admin/builds` | checks the form and files, stores them, starts the job, answers at once |
| `GET /admin/builds/{id}/events` | progress as Server-Sent Events, numbered, resumable |
| `GET /admin/builds/{id}` | the state, and the proposal once it waits for review |
| `POST /admin/builds/{id}/decision` | approve (saves and serves the bot) or reject (deletes the uploads) |

**Uploads are checked before anything is stored.**
- The admin check runs before the form is parsed. The request body is read inside the route, not by a framework parameter, so nobody else can make the server spool large uploads.
- The declared size is checked first: at most 60 MB in all, 20 MB per file, 20 files.
- Only `.pdf`, `.docx` and `.md`, and each file's first bytes must match its extension: `%PDF`, the ZIP header, or valid UTF-8 text.
- File names are reduced to a safe last path part. `../../bots/evil.md` becomes `evil.md`, always inside `documents/uploads/<bot id>/`, which is gitignored.
- The bot id must be new: no overwriting an existing bot, and no second build of an id that is waiting for review.

**Progress is a list of numbered events kept on the server.** Each event has an `id`. The browser follows with the standard `EventSource`, which reconnects by itself and sends the last id it saw (`Last-Event-ID`). The server then sends only what came after. A page reload, or another admin opening the same build, picks up exactly where it was. When the build stops running, the server sends an `end` event and the client closes the stream. Otherwise `EventSource` would reconnect forever.

**Review stays human.** The job pauses at the builder's existing `interrupt`. The browser shows a preview in the bot's own colour and avatar: welcome, suggested questions, topics, tools and what needs approval, the system prompt (rendered by code from the template) and the generated tests. Approving resumes the graph, which saves `bots/<id>.yaml` and `evals/bots/<id>.yaml`. Then `Platform.add_bot` loads the new file into the running server. Admins can use the bot at once. Members need a grant (Step 13), so publishing never widens anyone's access by itself.

**One build at a time.** Indexing is CPU- and memory-heavy on this 8 GB machine. A second build gets 409 until the first finishes.

**Builds live in memory.** A failed or rejected build deletes its uploads. At start-up, the server deletes uploads that never became a bot. Saved bots are plain files, so they survive.

## Alternatives considered

- **One long request that streams until the bot is ready** (as chat turns do in Step 11). A chat turn takes seconds, but a build takes minutes. Closing the tab would cancel the work, and only the page that started it could see its progress. A job with its own id can be left, reopened and shared between admins.
- **Polling `GET /admin/builds/{id}` every second.** Simpler on the client, but slower to show progress, and it makes many requests for nothing. Resumable SSE gives updates as they happen with one connection, and `EventSource` handles reconnecting for free. The detail route is still there for the first load and after the stream ends.
- **A job queue with workers (Celery, RQ, Arq) and a database of builds.** That is the right design with several servers, or builds that must survive restarts. Here it would add Redis and a worker process for one admin building a bot now and then. The `BuildManager` interface (start, follow, decide) is the seam where a queue would go.
- **Upload first, build later in a separate step.** It makes more round trips, and it leaves files around when the second step never happens. One form, checked as a whole, either starts a build or stores nothing.
- **Publish automatically when every check passes.** The checks catch broken configs and ungrounded questions. They don't catch a bot that is wrong for its audience, or a document that should not be shared. The human review from Step 10 stays.
- **Restart the server to load new bots, or watch the `bots/` folder.** A restart drops every streaming chat. Watching files would also load half-written or hand-edited files nobody approved. Loading exactly the bot that was just approved is simpler and deliberate.

## Consequences

Measured on a test build: one Markdown handbook (2 KB, 6 chunks), builder on Haiku 5.5.
- Indexing and the survey were done within a few seconds. The draft passed every check on the first try, and 4 tests were written.
- The proposal was ready in under half a minute.
- A full page reload during the build resumed the progress list where it was.
- After approval, the bot answered its suggested question with the right section cited. No restart was needed.

The default builder model is still Sonnet 5.5 (`BUILDER_MODEL`), because drafting quality matters more than its cost per build.

Other consequences:
- `tests/test_builds.py` (8 tests) covers the rules with a stand-in builder graph and no LLM: admin only, every upload check, file names that try to escape, the full approve flow serving the bot at once (members still need a grant), reject and failure clean-up, resuming events after `Last-Event-ID`, and one build at a time. A shared `tests/_env.py` lets the API test suites run together in one process.
- The Vite proxy reads `API_PORT`, the same setting as `api.py`, so a second copy of the API can run on another port.
- Research bots (Step 14) are not built here yet: they have no documents to upload, only sources to choose. Granting members from the browser is also not here yet (`manage_users.py grant` does it).
- Revisit with more than one server or frequent builds: move builds into a queue and a table, and keep progress events in the database so any server can resume a stream. Also add malware scanning of uploads before this is opened to people who are not trusted admins.
