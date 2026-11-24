// The Step 11 API, typed. Everything the server sends is data to display,
// never HTML to inject: React escapes it, so a model reply cannot run script.

const BASE = '/api' // forwarded to the Python server by Vite (vite.config.ts)

/** Replay build (npm run build:demo): recorded answers instead of the server. */
export const REPLAY = import.meta.env.VITE_REPLAY === '1'
// Loaded only in the replay build, so the normal build carries no recordings.
const replay = () => import('./demo/replay')

export type Theme = { primary_color: string; avatar: string }

export type Bot = {
  id: string
  name: string
  description: string
  welcome: string
  suggested_questions: string[]
  topics: string[]
  theme: Theme
}

export type ToolCall = { id: string; name: string; args: Record<string, unknown> }
export type Approval = { tool_calls: ToolCall[] }
export type ChatMessage = { role: 'user' | 'assistant'; text: string; sources?: string[] }
export type Thread = { thread_id: string; messages: ChatMessage[]; pending_approval: Approval | null }

export type StatusEvent = { stage: string; [key: string]: unknown }
export type DoneEvent = { answer: string; sources: string[]; guard: string }

// One event from a streamed turn (see api.py for the protocol).
export type TurnEvent =
  | { type: 'status'; data: StatusEvent }
  | { type: 'token'; data: { message_id: string; text: string } }
  | { type: 'approval'; data: Approval }
  | { type: 'error'; data: { type: string } }
  | { type: 'done'; data: DoneEvent }

export class ApiError extends Error {
  status: number
  retryAfter: number | null
  constructor(status: number, message: string, retryAfter: number | null = null) {
    super(message)
    this.status = status
    this.retryAfter = retryAfter
  }
}

async function check(res: Response): Promise<Response> {
  if (res.ok) return res
  let detail = res.statusText
  try {
    const body = await res.json()
    if (typeof body.detail === 'string') detail = body.detail
  } catch {
    /* not JSON: keep the status text */
  }
  const retry = res.headers.get('Retry-After')
  throw new ApiError(res.status, detail, retry ? Number(retry) : null)
}

export async function listBots(): Promise<Bot[]> {
  if (REPLAY) return (await replay()).listBots()
  return (await check(await fetch(`${BASE}/bots`))).json()
}

export async function newThread(botId: string): Promise<string> {
  if (REPLAY) return (await replay()).newThread()
  const res = await check(await fetch(`${BASE}/bots/${botId}/threads`, { method: 'POST' }))
  return (await res.json()).thread_id
}

export async function getThread(botId: string, threadId: string): Promise<Thread> {
  if (REPLAY) return (await replay()).getThread(botId, threadId)
  return (await check(await fetch(`${BASE}/bots/${botId}/threads/${threadId}`))).json()
}

/**
 * POST a body and read the reply as Server-Sent Events.
 *
 * The browser's EventSource only does GET, so the stream is read by hand:
 * events are separated by a blank line; each has "event:" and "data:" lines;
 * lines starting with ":" are keep-alive comments.
 */
async function streamPost(path: string, body: object, onEvent: (e: TurnEvent) => void,
                          signal?: AbortSignal): Promise<void> {
  const res = await check(await fetch(`${BASE}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
    body: JSON.stringify(body),
    signal,
  }))
  const reader = res.body!.pipeThrough(new TextDecoderStream()).getReader()
  let buffer = ''
  for (;;) {
    const { value, done } = await reader.read()
    if (done) break
    buffer = (buffer + value).replace(/\r\n/g, '\n') // the server ends lines with \r\n
    let end: number
    while ((end = buffer.indexOf('\n\n')) >= 0) {
      const block = buffer.slice(0, end)
      buffer = buffer.slice(end + 2)
      let type = 'message'
      const data: string[] = []
      for (const line of block.split('\n')) {
        if (line.startsWith('event:')) type = line.slice(6).trim()
        else if (line.startsWith('data:')) data.push(line.slice(5).replace(/^ /, ''))
      }
      if (data.length) onEvent({ type, data: JSON.parse(data.join('\n')) } as TurnEvent)
    }
  }
}

export async function sendMessage(botId: string, threadId: string, text: string,
                            onEvent: (e: TurnEvent) => void, signal?: AbortSignal) {
  if (REPLAY) return (await replay()).sendMessage(botId, threadId, text, onEvent, signal)
  return streamPost(`/bots/${botId}/threads/${threadId}/messages`, { text }, onEvent, signal)
}

export async function sendApproval(botId: string, threadId: string, decision: 'approve' | 'reject',
                             onEvent: (e: TurnEvent) => void, signal?: AbortSignal) {
  if (REPLAY) return (await replay()).sendApproval()
  return streamPost(`/bots/${botId}/threads/${threadId}/approval`, { decision }, onEvent, signal)
}

// ---------------------------------------------------------------- Step 13: signing in
// The session lives in an HttpOnly cookie that scripts cannot read; the browser
// sends it with every same-origin request, including the streamed ones above.

export type Usage = { turns: number; tokens: number; turn_limit: number; token_limit: number }
export type Me = { username: string; role: 'admin' | 'member'; usage: Usage }

export async function login(username: string, password: string): Promise<Me> {
  const res = await check(await fetch(`${BASE}/auth/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, password }),
  }))
  return res.json()
}

export async function logout(): Promise<void> {
  await fetch(`${BASE}/auth/logout`, { method: 'POST' })
}

/** The signed-in user, or null if there is no valid session. */
export async function me(): Promise<Me | null> {
  if (REPLAY) return (await replay()).me()
  const res = await fetch(`${BASE}/auth/me`)
  if (res.status === 401) return null
  return (await check(res)).json()
}

// ---------------------------------------------------------------- Step 15: building bots (admins)
// A build is a job on the server: start it with an upload, follow its events,
// then approve or reject the proposal. Reloading the page loses nothing.

export type BuildStatus = 'running' | 'review' | 'deciding' | 'saved' | 'rejected' | 'failed'
export type BuildEvent = { n: number; stage: string; status: BuildStatus; message?: string;
                           files?: number; problems?: string[] }
export type BuildSummary = { id: string; bot_id: string; status: BuildStatus; by: string; started: number;
                             files: string[]; use_case: string }
export type BotProposal = Bot & {
  system_prompt: string
  tools: string[]
  approval_required: string[]
  knowledge: { folder: string; collection: string; subject: string }
}
export type TestCase = { id: string; category?: string; turns: string[]; expected_behaviour?: string }
export type Proposal = { config: BotProposal; tests: TestCase[]; log: string[] }
export type BuildDetail = BuildSummary & { events: BuildEvent[]; proposal: Proposal | null; problems: string[];
                                           saved: string[] }

/** The same limits as builds.py, checked here first so mistakes show before a long upload. */
export const BUILD_LIMITS = { files: 20, fileMB: 20, totalMB: 60, types: ['.pdf', '.docx', '.md'],
                              useCase: [20, 1000], id: /^[a-z][a-z0-9_]{2,39}$/ }

export async function startBuild(botId: string, useCase: string, files: File[]): Promise<BuildSummary> {
  const form = new FormData()
  form.append('bot_id', botId)
  form.append('use_case', useCase)
  for (const f of files) form.append('files', f, f.name)
  // No Content-Type header: the browser sets multipart/form-data with its boundary.
  return (await check(await fetch(`${BASE}/admin/builds`, { method: 'POST', body: form }))).json()
}

export async function listBuilds(): Promise<BuildSummary[]> {
  return (await check(await fetch(`${BASE}/admin/builds`))).json()
}

export async function getBuild(id: string): Promise<BuildDetail> {
  return (await check(await fetch(`${BASE}/admin/builds/${id}`))).json()
}

export async function decideBuild(id: string, decision: 'approve' | 'reject'): Promise<BuildDetail> {
  return (await check(await fetch(`${BASE}/admin/builds/${id}/decision`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ decision }),
  }))).json()
}

/**
 * Follow a build's progress. A GET stream, so the browser's own EventSource
 * works: it reconnects by itself and sends Last-Event-ID, and the server
 * continues after that event. Returns a function that stops following.
 */
export function followBuild(id: string, after: number, onEvent: (e: BuildEvent) => void,
                            onEnd: () => void): () => void {
  const source = new EventSource(`${BASE}/admin/builds/${id}/events?after=${after}`)
  source.addEventListener('build', (e) => onEvent(JSON.parse((e as MessageEvent).data)))
  // The server closes the stream when the build stops running; without close()
  // EventSource would treat that as a dropped connection and reconnect forever.
  source.addEventListener('end', () => { source.close(); onEnd() })
  // A refused stream (signed out, unknown build) is not retried by the browser: reload the state instead.
  source.onerror = () => { if (source.readyState === EventSource.CLOSED) onEnd() }
  return () => source.close()
}

// ---------------------------------------------------------------- Step 17: error analysis (admins)
// A domain expert labels a bot's synthetic conversations; the failure modes and
// judges are built from those labels by analysis.py.

export type Step = 'input_check' | 'search' | 'answer' | 'fact_check' | 'other'
export type TraceStep =
  | { step: 'input_check'; result: string; detail: string }
  | { step: 'tool_call'; tool: string; args: Record<string, unknown> }
  | { step: 'tool_result'; tool: string; text: string }
  | { step: 'fact_check'; revisions: number }
export type Label = { verdict: 'pass' | 'fail'; note: string; first_failure: Step | null; by: string; at: string }
export type TraceItem = { id: string; profile: Record<string, string>; message: string; answer: string;
                          steps: TraceStep[]; models: string[]; error: string; seconds: number;
                          label: Label | null }
export type FailureMode = { id: string; name: string; definition: string }
export type Taxonomy = { status: 'proposed' | 'approved'; modes: FailureMode[]; assignments: Record<string, string[]>;
                         approved_by?: string }
export type Rates = { tp: number; fn: number; tn: number; fp: number; tpr: number | null; tnr: number | null }
export type ModeJudge = { name: string; examples: string[]; dev: Rates; test: Rates;
                          test_rates?: { expert: number; judge: number; corrected: number | null } }
export type Judges = { model: string; splits: Record<string, number>; modes: Record<string, ModeJudge> }
export type AnalysisSummary = { bot_id: string; traces: number; labelled: number; failed: number;
                                taxonomy_status: 'proposed' | 'approved' | null; judges_built: boolean }
export type Analysis = AnalysisSummary & { dimensions: { dimensions?: Record<string, string[]> }; steps: Step[];
                                           items: TraceItem[]; taxonomy: Taxonomy | null; judges: Judges | null }

export async function listAnalyses(): Promise<AnalysisSummary[]> {
  return (await check(await fetch(`${BASE}/admin/analysis`))).json()
}

export async function getAnalysis(botId: string): Promise<Analysis> {
  return (await check(await fetch(`${BASE}/admin/analysis/${botId}`))).json()
}

async function send<T>(method: string, path: string, body?: unknown): Promise<T> {
  return (await check(await fetch(`${BASE}${path}`, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  }))).json()
}

export function saveLabel(botId: string, traceId: string, verdict: 'pass' | 'fail', note: string,
                          firstFailure: Step | null) {
  return send<{ label: Label; summary: AnalysisSummary; taxonomy: Taxonomy | null }>(
    'PUT', `/admin/analysis/${botId}/labels/${traceId}`, { verdict, note, first_failure: firstFailure })
}

export function assignModes(botId: string, traceId: string, modes: string[]) {
  return send<Taxonomy>('PUT', `/admin/analysis/${botId}/assignments/${traceId}`, { modes })
}

export function approveTaxonomy(botId: string) {
  return send<Taxonomy>('POST', `/admin/analysis/${botId}/taxonomy/approve`)
}
