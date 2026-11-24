import { useEffect, useRef, useState } from 'react'
import type { CSSProperties, FormEvent, KeyboardEvent } from 'react'
import { ApiError, getThread, newThread, sendApproval, sendMessage } from './api'
import type { Approval, Bot, TurnEvent } from './api'
import { Avatar } from './Avatar'
import { describeStep } from './steps'

type Message = {
  role: 'user' | 'assistant'
  text: string
  sources?: string[]
  steps?: string[]
  notice?: string
}

// The turn in progress: the steps so far and the draft being written.
type Live = { steps: string[]; draft: string; draftId: string | null }

type Start = (onEvent: (e: TurnEvent) => void, signal: AbortSignal) => Promise<void>

const MAX_CHARS = 4000

// The open conversation per bot is remembered in this browser only.
// Storage can be unavailable (private windows), so every access is guarded.
const storageKey = (botId: string) => `thread:${botId}`
function loadThreadId(botId: string): string | null {
  try { return localStorage.getItem(storageKey(botId)) } catch { return null }
}
function saveThreadId(botId: string, id: string) {
  try { localStorage.setItem(storageKey(botId), id) } catch { /* not remembered; still works */ }
}

function describeError(e: unknown): string {
  if (e instanceof ApiError) {
    if (e.status === 429) return `Too many messages. Try again in ${e.retryAfter ?? 60} seconds.`
    return e.message
  }
  if (e instanceof TypeError) return "Can't reach the server. Is api.py running?"
  return String(e)
}

export function Chat({ bot, onBack }: { bot: Bot; onBack: () => void }) {
  const [threadId, setThreadId] = useState<string | null>(null)
  const [messages, setMessages] = useState<Message[]>([])
  const [live, setLive] = useState<Live | null>(null)
  const [pending, setPending] = useState<Approval | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [input, setInput] = useState('')
  const abortRef = useRef<AbortController | null>(null)
  const pausedSteps = useRef<string[]>([]) // steps before an approval pause, continued after it
  const endRef = useRef<HTMLDivElement>(null)
  const busy = live !== null

  // Open this bot's saved conversation, or start a new one.
  useEffect(() => {
    let ignore = false
    async function open() {
      const saved = loadThreadId(bot.id)
      if (saved) {
        try {
          const thread = await getThread(bot.id, saved)
          if (ignore) return
          setThreadId(saved)
          setMessages(thread.messages)
          setPending(thread.pending_approval)
          return
        } catch {
          /* unknown or invalid id: start a new conversation */
        }
      }
      const id = await newThread(bot.id)
      if (ignore) return
      saveThreadId(bot.id, id)
      setThreadId(id)
    }
    open().catch((e) => !ignore && setError(describeError(e)))
    return () => {
      ignore = true
      abortRef.current?.abort() // leaving mid-turn: the server repairs the conversation
    }
  }, [bot.id])

  useEffect(() => {
    endRef.current?.scrollIntoView({ block: 'end' })
  }, [messages, live, pending, error])

  /** Run one streamed request and turn its events into screen state. */
  async function runTurn(start: Start, onRejected: () => void, steps: string[] = []) {
    const controller = new AbortController()
    abortRef.current = controller
    let current: Live = { steps, draft: '', draftId: null }
    const update = (next: Partial<Live>) => {
      current = { ...current, ...next }
      setLive(current)
    }
    let ended = false
    let notice: string | undefined
    setLive(current)
    setError(null)
    try {
      await start((e) => {
        switch (e.type) {
          case 'status': {
            const step = describeStep(e.data)
            if (step) update({ steps: [...current.steps, step] })
            break
          }
          case 'token':
            // A new message id is a new draft (for example after a failed fact check).
            if (e.data.message_id !== current.draftId) update({ draftId: e.data.message_id, draft: e.data.text })
            else update({ draft: current.draft + e.data.text })
            break
          case 'approval':
            pausedSteps.current = current.steps
            setPending(e.data)
            ended = true
            break
          case 'error':
            notice = 'Something went wrong while answering. The conversation was kept, so you can try again.'
            break
          case 'done':
            // The final answer replaces the draft: only this text passed every check.
            setMessages((m) => [...m, { role: 'assistant', text: e.data.answer, sources: e.data.sources,
                                        steps: current.steps, notice }])
            ended = true
            break
        }
      }, controller.signal)
      if (!ended && !controller.signal.aborted) {
        setError('The connection closed before the answer finished. Reload the page to see what was saved.')
      }
    } catch (e) {
      if (controller.signal.aborted) return
      if (e instanceof ApiError) onRejected() // refused before it started: undo the optimistic update
      setError(describeError(e))
    } finally {
      if (abortRef.current === controller) setLive(null)
    }
  }

  function send(text: string) {
    text = text.trim()
    if (!text || busy || pending || !threadId) return
    setMessages((m) => [...m, { role: 'user', text }])
    setInput('')
    runTurn((onEvent, signal) => sendMessage(bot.id, threadId, text, onEvent, signal), () => {
      setMessages((m) => m.slice(0, -1))
      setInput(text)
    })
  }

  function decide(decision: 'approve' | 'reject') {
    if (!pending || !threadId) return
    const waiting = pending
    setPending(null)
    runTurn((onEvent, signal) => sendApproval(bot.id, threadId, decision, onEvent, signal),
            () => setPending(waiting), pausedSteps.current)
  }

  async function startOver() {
    abortRef.current?.abort()
    try {
      const id = await newThread(bot.id)
      saveThreadId(bot.id, id)
      setThreadId(id)
      setMessages([])
      setPending(null)
      setError(null)
    } catch (e) {
      setError(describeError(e))
    }
  }

  function onSubmit(e: FormEvent) {
    e.preventDefault()
    send(input)
  }

  function onKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault()
      send(input)
    }
  }

  // The bot's colour drives the theme through one CSS variable.
  const theme = { '--primary': bot.theme.primary_color } as CSSProperties

  return (
    <div className="chat" style={theme}>
      <header className="chat-header">
        <button className="ghost" onClick={onBack} aria-label="Back to all bots">←</button>
        <Avatar name={bot.theme.avatar} />
        <div className="chat-title">
          <h1>{bot.name}</h1>
          <p>{bot.description}</p>
        </div>
        <button className="ghost" onClick={startOver} disabled={busy}>New chat</button>
      </header>

      <main className="messages" role="log" aria-live="polite" aria-busy={busy}>
        {messages.length === 0 && !busy && (
          <div className="welcome">
            <Avatar name={bot.theme.avatar} size={56} />
            <p>{bot.welcome || `Ask me about ${bot.topics.join(', ')}.`}</p>
            {bot.suggested_questions.length > 0 && (
              <div className="suggestions">
                {bot.suggested_questions.map((q) => (
                  <button key={q} className="chip" onClick={() => send(q)} disabled={!threadId}>{q}</button>
                ))}
              </div>
            )}
          </div>
        )}

        {messages.map((m, i) => (m.role === 'user'
          ? <div key={i} className="bubble user">{m.text}</div>
          : <AssistantMessage key={i} message={m} avatar={bot.theme.avatar} />))}

        {/* The draft is not announced to screen readers token by token; the final answer is. */}
        {live && <LiveTurn live={live} avatar={bot.theme.avatar} />}

        {pending && <ApprovalCard approval={pending} botName={bot.name} onDecide={decide} />}

        {error && <div className="alert" role="alert">{error}</div>}
        <div ref={endRef} />
      </main>

      <form className="composer" onSubmit={onSubmit}>
        <textarea
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={onKeyDown}
          placeholder={pending ? 'Answer the approval request first' : `Message ${bot.name}`}
          aria-label="Your message"
          maxLength={MAX_CHARS}
          rows={1}
          disabled={!threadId || !!pending}
        />
        <button type="submit" className="primary" disabled={busy || !!pending || !input.trim()}>Send</button>
      </form>
    </div>
  )
}

/** Model text, shown safely: paragraphs and `code`, never interpreted as HTML. */
function Text({ text }: { text: string }) {
  return (
    <>
      {text.split(/\n{2,}/).map((para, i) => (
        <p key={i}>
          {para.split(/(`[^`]+`)/).map((part, j) =>
            part.startsWith('`') && part.endsWith('`') && part.length > 2
              ? <code key={j}>{part.slice(1, -1)}</code>
              : part)}
        </p>
      ))}
    </>
  )
}

function AssistantMessage({ message, avatar }: { message: Message; avatar: string }) {
  return (
    <div className="assistant">
      <Avatar name={avatar} size={32} />
      <div className="bubble bot">
        <Text text={message.text} />
        {message.notice && <p className="notice">{message.notice}</p>}
        {message.sources && message.sources.length > 0 && (
          <div className="sources">
            <span className="label">Sources</span>
            <ul>{message.sources.map((s) => <li key={s}>{s}</li>)}</ul>
          </div>
        )}
        {message.steps && message.steps.length > 0 && (
          <details className="steps">
            <summary>How this answer was made ({message.steps.length} {message.steps.length === 1 ? 'step' : 'steps'})</summary>
            <ol>{message.steps.map((s, i) => <li key={i}>{s}</li>)}</ol>
          </details>
        )}
      </div>
    </div>
  )
}

function LiveTurn({ live, avatar }: { live: Live; avatar: string }) {
  return (
    <div className="assistant" aria-live="off">
      <Avatar name={avatar} size={32} />
      <div className="bubble bot live">
        <ol className="live-steps">
          {live.steps.map((s, i) => (
            <li key={i} className={i === live.steps.length - 1 && !live.draft ? 'active' : 'done'}>{s}</li>
          ))}
          {live.steps.length === 0 && <li className="active">Sending…</li>}
        </ol>
        {live.draft && (
          <div className="draft">
            <span className="label">Draft, checking before it is final</span>
            <Text text={live.draft} />
          </div>
        )}
      </div>
    </div>
  )
}

function ApprovalCard({ approval, botName, onDecide }: {
  approval: Approval
  botName: string
  onDecide: (d: 'approve' | 'reject') => void
}) {
  return (
    <div className="approval" role="group" aria-label="Approval needed">
      <strong>{botName} wants to use a tool that changes something</strong>
      {approval.tool_calls.map((call) => (
        <div key={call.id} className="tool-call">
          <code>{call.name}</code>
          <dl>
            {Object.entries(call.args).map(([k, v]) => (
              <div key={k}><dt>{k}</dt><dd>{typeof v === 'string' ? v : JSON.stringify(v)}</dd></div>
            ))}
          </dl>
        </div>
      ))}
      <p className="hint">Nothing happens unless you approve.</p>
      <div className="actions">
        <button className="primary" onClick={() => onDecide('approve')}>Approve</button>
        <button className="ghost" onClick={() => onDecide('reject')}>Reject</button>
      </div>
    </div>
  )
}
