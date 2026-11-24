# 0017. React frontend: one theme variable per bot, drafts shown as drafts, approvals as cards, no HTML from the model

- Status: Accepted
- Date: 2026-11-24
- Step: 12

## Context

The platform (ADR 0007) promised bots with their own look. The API (ADR 0016) streams progress, preview tokens, approvals and a final answer. A frontend has to turn these into something people trust:

- **Speed:** the answer takes several seconds, so the user needs to see progress.
- **Drafts:** a streamed draft may be withdrawn by the fact check.
- **Approvals:** some tool calls need a decision, which may be made after a reload.
- **Untrusted text:** model output may contain anything, including text from an injected document.
- **Theming:** every bot looks different without a separate stylesheet per bot.

## Decision

**React, TypeScript and Vite**, as chosen in the roadmap. All versions are pinned exactly, with `package-lock.json` committed (ADR 0004).

**One origin in development.** Vite forwards `/api/*` to the API on `127.0.0.1:8000`. The browser never calls the API directly, and the API stays on localhost.

**Streaming over POST, read by hand.** The browser's `EventSource` only supports GET, so `api.ts` reads the `fetch` response stream and splits it into events itself. It is about 25 lines, with no dependency. The request is aborted when the user leaves the chat, and the server then repairs the conversation (ADR 0016).

**What the user sees, in this order:**
1. The steps, ticked off as they happen: safety check, the search and its query, the fact check.
2. The draft, labelled "Draft, checking before it is final" in muted text. A new message id (a revision after a failed fact check) replaces it.
3. On `done`, the draft is replaced by the final answer. The answer shows its sources, and the steps fold into "How this answer was made".

**Approvals are cards, not chat messages.** The card shows:
- the tool's name and every argument, in full
- the message "Nothing happens unless you approve"
- Approve and Reject buttons

The message box is locked while a decision is pending. The pending approval comes from the server, so it survives a reload.

**Model text is never HTML.** Answers are rendered as React text: paragraphs, plus `code` spans split out by hand. There is no Markdown library and no `dangerouslySetInnerHTML`, so an answer containing `<script>` or an injected link shows up as text.

**Theming through one variable.** A bot's `primary_color` sets `--primary` on the chat. Tints, borders and text accents are derived with `color-mix()`. The builder already guarantees 4.5:1 contrast against white text (ADR 0015), so buttons and user bubbles are always readable. A dark mode follows the system setting and derives lighter accents from the same colour.

**State:**
- **The open bot:** kept in the URL (`#/bot/<id>`), so the back button and bookmarks work.
- **The current thread id per bot:** kept in `localStorage`, guarded because storage can be unavailable.
- **Everything else** (messages, sources, pending approvals) comes from the server. The API now also returns each answer's sources when a conversation is reloaded.

**Accessibility:**
- The message list is a polite live region, so finished answers are announced.
- The streaming draft is excluded, so screen readers are not flooded token by token.
- Focus outlines are visible, motion respects reduced-motion settings, and the layout works at 375 px wide.

## Alternatives considered

- **Next.js, or another server-rendered framework.** It adds a Node server, routing and hydration concerns. This app is one client talking to an API that already exists. Vite is a static build that any web server, or FastAPI itself, can host later.
- **A Markdown renderer for answers.** Nicer lists and bold text, but it is the usual path for injected HTML and links into a page. Revisit with a sanitising renderer and a strict link policy, once the safety benefit of plain text is weighed against readability.
- **Hiding the answer until it has been fact-checked.** Safe, but the user stares at a spinner for 5–10 s. Labelled drafts give both.
- **A stylesheet or component set per bot.** Unlimited freedom, but every bot would then need design review. One validated colour and a fixed avatar set keep every generated bot readable.
- **The `@microsoft/fetch-event-source` library.** It handles retries and visibility changes. For one endpoint, the hand-written reader is short, has no dependency and makes the protocol visible.

## Consequences

**Tested in the browser pane, against the real API:**
- the bot list
- the themed chat (blue compass for the guide, green owl for the study assistant)
- steps and a draft during a turn, then the final answer with three sources and five steps
- a pending approval that survived a page reload, then was rejected, with nothing saved
- phone width (no sideways scrolling) and dark mode

The production build is 73 kB gzipped.

**The draft and the final answer can differ.** If the fact check fails, the user sees one draft replaced by another. That is honest, but it can surprise people. The label explains it.

**Limits:**
- **Steps are not saved with the conversation.** After a reload, answers show their sources but not how they were made. Saving the steps would need them stored in the checkpoint.
- **A reload during a turn shows the turn as unfinished**, until the next message reveals the repaired history.
- **No authentication:** anyone who opens the page can use every bot (Step 13).
- **Production hosting is not set up.** `npm run build` makes static files. Serving them from FastAPI, or from a web server in front of it, is a deployment step still to come.
