// Replay mode (VITE_REPLAY=1): the same frontend, served as static files, plays
// back conversations recorded from the real API (scripts/record_demo.py).
// No server, no model and no key: every answer and step is a real recording.

import { ApiError } from '../api'
import type { Bot, Me, Thread, TurnEvent } from '../api'
import data from './recordings.json'

type Recorded = { t: number } & TurnEvent
const recordings = data as unknown as { recorded_at: string; bots: Bot[]; turns: Record<string, Recorded[]> }

export const recordedAt = recordings.recorded_at

const MAX_GAP_S = 1.5 // long waits for the model are shortened, the order and steps are not
const visitor: Me = { username: 'visitor', role: 'member', usage: { turns: 0, tokens: 0, turn_limit: 0, token_limit: 0 } }
const threads = new Map<string, Thread>()

export async function me(): Promise<Me> {
  return visitor
}

export async function listBots(): Promise<Bot[]> {
  return recordings.bots
}

export async function newThread(): Promise<string> {
  const id = crypto.randomUUID()
  threads.set(id, { thread_id: id, messages: [], pending_approval: null })
  return id
}

export async function getThread(_botId: string, threadId: string): Promise<Thread> {
  const thread = threads.get(threadId)
  if (!thread) throw new ApiError(404, 'Not found') // the page was reloaded: start a new conversation
  return thread
}

const wait = (ms: number, signal?: AbortSignal) => new Promise<void>((resolve, reject) => {
  const timer = setTimeout(resolve, ms)
  signal?.addEventListener('abort', () => { clearTimeout(timer); reject(new DOMException('Aborted', 'AbortError')) })
})

export async function sendMessage(botId: string, threadId: string, text: string,
                                  onEvent: (e: TurnEvent) => void, signal?: AbortSignal) {
  const events = recordings.turns[`${botId}|${text}`]
  if (!events) throw new ApiError(400, 'This demo replays recorded answers only. Pick one of the questions.')
  let last = 0
  for (const { t, ...e } of events) {
    await wait(Math.min(t - last, MAX_GAP_S) * 1000, signal)
    last = t
    onEvent(e as TurnEvent)
  }
  const done = events.find((e) => e.type === 'done')
  if (done?.type === 'done') {
    threads.get(threadId)?.messages.push({ role: 'user', text },
                                         { role: 'assistant', text: done.data.answer, sources: done.data.sources })
  }
}

export async function sendApproval(): Promise<void> {
  throw new ApiError(400, 'Approvals are not part of this demo.')
}
