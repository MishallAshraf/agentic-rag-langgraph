// The Step 11 API, typed. Everything the server sends is data to display,
// never HTML to inject: React escapes it, so a model reply cannot run script.

const BASE = '/api' // forwarded to the Python server by Vite (vite.config.ts)

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
  return (await check(await fetch(`${BASE}/bots`))).json()
}

export async function newThread(botId: string): Promise<string> {
  const res = await check(await fetch(`${BASE}/bots/${botId}/threads`, { method: 'POST' }))
  return (await res.json()).thread_id
}

export async function getThread(botId: string, threadId: string): Promise<Thread> {
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

export function sendMessage(botId: string, threadId: string, text: string,
                            onEvent: (e: TurnEvent) => void, signal?: AbortSignal) {
  return streamPost(`/bots/${botId}/threads/${threadId}/messages`, { text }, onEvent, signal)
}

export function sendApproval(botId: string, threadId: string, decision: 'approve' | 'reject',
                             onEvent: (e: TurnEvent) => void, signal?: AbortSignal) {
  return streamPost(`/bots/${botId}/threads/${threadId}/approval`, { decision }, onEvent, signal)
}
