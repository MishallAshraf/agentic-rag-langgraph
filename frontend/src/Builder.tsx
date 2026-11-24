import { useEffect, useState } from 'react'
import type { CSSProperties, FormEvent } from 'react'
import { ApiError, BUILD_LIMITS, decideBuild, followBuild, getBuild, listBuilds, startBuild } from './api'
import type { BuildDetail, BuildEvent, BuildSummary, Proposal } from './api'
import { Avatar } from './Avatar'

// Step 15: the bot builder for admins. #/build shows the form and recent
// builds; #/build/<id> follows one build and shows its proposal for review.

const STATUS_TEXT: Record<string, string> = {
  running: 'Building…', review: 'Waiting for your review', deciding: 'Saving…',
  saved: 'Saved', rejected: 'Rejected', failed: 'Failed',
}

type Props = { buildId: string | null; onBotSaved: () => void; onSignedOut: () => void }

export function Builder({ buildId, onBotSaved, onSignedOut }: Props) {
  return (
    <div className="page builder">
      <header className="page-header">
        <div>
          <a className="small" href="#/">← All bots</a>
          <h1>Create a bot</h1>
          <p className="muted">Upload documents and describe who the bot is for. You review the bot before anyone can use it.</p>
        </div>
      </header>
      {buildId
        ? <BuildView key={buildId} id={buildId} onBotSaved={onBotSaved} onSignedOut={onSignedOut} />
        : <NewBuild onSignedOut={onSignedOut} />}
    </div>
  )
}

/** Problems with the form, found before anything is uploaded. The server checks again. */
function formProblems(botId: string, useCase: string, files: File[]): string[] {
  const L = BUILD_LIMITS
  const problems = []
  if (!L.id.test(botId)) problems.push('Bot id: 3-40 characters, lowercase letters, digits and underscores, starting with a letter.')
  const n = useCase.trim().length
  if (n < L.useCase[0] || n > L.useCase[1]) problems.push(`Describe the use case in ${L.useCase[0]} to ${L.useCase[1]} characters.`)
  if (files.length < 1 || files.length > L.files) problems.push(`Choose 1 to ${L.files} files.`)
  for (const f of files) {
    const ext = f.name.slice(f.name.lastIndexOf('.')).toLowerCase()
    if (!L.types.includes(ext)) problems.push(`${f.name}: only ${L.types.join(', ')} files are accepted.`)
    if (f.size > L.fileMB * 2 ** 20) problems.push(`${f.name}: larger than ${L.fileMB} MB.`)
  }
  if (files.reduce((s, f) => s + f.size, 0) > L.totalMB * 2 ** 20) problems.push(`The files add up to more than ${L.totalMB} MB.`)
  return problems
}

function size(bytes: number): string {
  return bytes < 2 ** 20 ? `${Math.max(1, Math.round(bytes / 1024))} KB` : `${(bytes / 2 ** 20).toFixed(1)} MB`
}

function NewBuild({ onSignedOut }: { onSignedOut: () => void }) {
  const [botId, setBotId] = useState('')
  const [useCase, setUseCase] = useState('')
  const [files, setFiles] = useState<File[]>([])
  const [problems, setProblems] = useState<string[]>([])
  const [sending, setSending] = useState(false)
  const [recent, setRecent] = useState<BuildSummary[]>([])

  useEffect(() => { listBuilds().then(setRecent).catch(() => {}) }, [])

  async function submit(e: FormEvent) {
    e.preventDefault()
    const found = formProblems(botId, useCase, files)
    setProblems(found)
    if (found.length) return
    setSending(true)
    try {
      const build = await startBuild(botId, useCase, files)
      window.location.hash = `#/build/${build.id}`
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) return onSignedOut()
      setProblems([err instanceof ApiError ? err.message : 'Could not reach the server.'])
      setSending(false)
    }
  }

  return (
    <>
      <form className="build-form" onSubmit={submit} noValidate>
        <label>
          Bot id
          <input value={botId} onChange={(e) => setBotId(e.target.value.trim())} placeholder="staff_handbook"
                 autoComplete="off" spellCheck={false} />
          <span className="muted small">Lowercase letters, digits and underscores. Used in links and file names.</span>
        </label>
        <label>
          Use case
          <textarea value={useCase} onChange={(e) => setUseCase(e.target.value)} rows={4}
                    placeholder="Help new staff find answers about leave, expenses and IT in the staff handbook." />
          <span className="muted small">Who uses the bot and what for. The builder drafts the bot from this and the documents.</span>
        </label>
        <label>
          Documents
          <input type="file" multiple accept={BUILD_LIMITS.types.join(',')}
                 onChange={(e) => setFiles(Array.from(e.target.files ?? []))} />
          <span className="muted small">
            PDF, Word (.docx) or Markdown; up to {BUILD_LIMITS.files} files, {BUILD_LIMITS.fileMB} MB each.
          </span>
        </label>
        {files.length > 0 && (
          <ul className="file-list small">
            {files.map((f) => <li key={f.name}>{f.name} <span className="muted">{size(f.size)}</span></li>)}
          </ul>
        )}
        {problems.length > 0 && (
          <div className="alert" role="alert">
            {problems.length === 1 ? problems[0] : <ul>{problems.map((p) => <li key={p}>{p}</li>)}</ul>}
          </div>
        )}
        <div className="actions">
          <button className="primary" type="submit" disabled={sending}>{sending ? 'Uploading…' : 'Build the bot'}</button>
        </div>
        <p className="muted small">Building takes one to a few minutes and uses model credit. You can leave this page and come back.</p>
      </form>
      {recent.length > 0 && (
        <section className="recent-builds">
          <h2>Recent builds</h2>
          <ul>
            {recent.map((b) => (
              <li key={b.id}>
                <a href={`#/build/${b.id}`}>{b.bot_id}</a>{' '}
                <span className={`status status-${b.status}`}>{STATUS_TEXT[b.status] ?? b.status}</span>{' '}
                <span className="muted small">by {b.by}, {new Date(b.started * 1000).toLocaleString()}</span>
              </li>
            ))}
          </ul>
        </section>
      )}
    </>
  )
}

function BuildView({ id, onBotSaved, onSignedOut }: { id: string; onBotSaved: () => void; onSignedOut: () => void }) {
  const [build, setBuild] = useState<BuildDetail | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [deciding, setDeciding] = useState(false)

  function load() {
    getBuild(id).then(setBuild).catch((e) => {
      if (e instanceof ApiError && e.status === 401) onSignedOut()
      else setError(e instanceof ApiError && e.status === 404
        ? 'This build is not on the server any more (builds are kept in memory until a restart).'
        : 'Could not reach the server.')
    })
  }

  // eslint-disable-next-line react-hooks/exhaustive-deps -- load once per build id (the component is keyed by it)
  useEffect(load, [id])

  const running = build?.status === 'running'
  const seen = build?.events.length ?? 0
  useEffect(() => {
    if (!running) return
    // Continue after the events already loaded; when the stream ends, load the final state.
    const add = (e: BuildEvent) => setBuild((b) => (b && e.n > b.events.length ? { ...b, events: [...b.events, e] } : b))
    return followBuild(id, seen, add, load)
    // eslint-disable-next-line react-hooks/exhaustive-deps -- follow once per running build, not per event
  }, [id, running])

  async function decide(decision: 'approve' | 'reject') {
    setDeciding(true)
    try {
      const result = await decideBuild(id, decision)
      setBuild(result)
      if (result.status === 'saved') onBotSaved()
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Could not reach the server.')
    } finally {
      setDeciding(false)
    }
  }

  if (error) return <div className="alert" role="alert">{error}</div>
  if (!build) return <p className="muted">Loading…</p>

  return (
    <div className="build-view">
      <p>
        <strong>{build.bot_id}</strong>{' '}
        <span className={`status status-${build.status}`}>{STATUS_TEXT[build.status] ?? build.status}</span>
      </p>
      <p className="muted small">{build.use_case}</p>
      <p className="muted small">Files: {build.files.join(', ')}</p>
      <ol className="live-steps build-steps" aria-live="polite">
        {build.events.filter((e) => e.message || e.stage === 'started').map((e, i, all) => (
          <li key={e.n} className={running && i === all.length - 1 ? 'active' : 'done'}>
            {e.stage === 'started' ? `Started with ${e.files} ${e.files === 1 ? 'file' : 'files'}` : e.message}
          </li>
        ))}
        {running && build.events.length === 0 && <li className="active">Starting…</li>}
      </ol>
      {build.status === 'failed' && (
        <div className="alert" role="alert">
          <p>The builder could not make a bot that passes its checks:</p>
          <ul>{build.problems.map((p) => <li key={p}>{p}</li>)}</ul>
          <p>The uploaded files were deleted. Try clearer documents or a use case that matches them.</p>
        </div>
      )}
      {build.status === 'rejected' && <p className="muted">Rejected. Nothing was saved and the uploaded files were deleted.</p>}
      {build.status === 'saved' && (
        <div className="saved-note">
          <p><strong>Saved.</strong> The bot is live for admins now. <a href={`#/bot/${build.bot_id}`}>Open it</a></p>
          <p className="muted small">
            Members need access first: <code>python manage_users.py grant &lt;user&gt; {build.bot_id}</code>.
            Its test questions are in <code>evals/bots/{build.bot_id}.yaml</code>.
          </p>
        </div>
      )}
      {build.proposal && build.status !== 'saved' && build.status !== 'rejected' && (
        <ProposalReview proposal={build.proposal} deciding={deciding} onDecide={decide} />
      )}
      {build.proposal && build.status === 'saved' && <ProposalReview proposal={build.proposal} />}
    </div>
  )
}

function ProposalReview({ proposal, deciding, onDecide }:
  { proposal: Proposal; deciding?: boolean; onDecide?: (d: 'approve' | 'reject') => void }) {
  const bot = proposal.config
  return (
    <section className="proposal" style={{ '--primary': bot.theme.primary_color } as CSSProperties}>
      <h2>{onDecide ? 'Review the proposed bot' : 'The bot as saved'}</h2>
      <div className="preview">
        <div className="preview-head">
          <Avatar name={bot.theme.avatar} size={48} />
          <div>
            <h3>{bot.name}</h3>
            <p className="muted">{bot.description}</p>
          </div>
        </div>
        <p>{bot.welcome}</p>
        <div className="suggestions left">
          {bot.suggested_questions.map((q) => <span key={q} className="chip">{q}</span>)}
        </div>
      </div>
      <dl className="facts">
        <div><dt>Answers from</dt><dd>{bot.knowledge.subject}</dd></div>
        <div><dt>Topics</dt><dd>{bot.topics.join('; ')}</dd></div>
        <div><dt>Tools</dt><dd>{bot.tools.join(', ')}</dd></div>
        <div><dt>Needs approval</dt><dd>{bot.approval_required.length ? bot.approval_required.join(', ') : 'nothing'}</dd></div>
        <div><dt>Colour</dt><dd><span className="swatch" /> {bot.theme.primary_color}</dd></div>
      </dl>
      <details>
        <summary>System prompt (filled in by code from a fixed template)</summary>
        <pre className="prompt">{bot.system_prompt}</pre>
      </details>
      <details>
        <summary>{proposal.tests.length} test questions, saved with the bot</summary>
        <ul className="small">
          {proposal.tests.map((t) => (
            <li key={t.id}><span className="muted">[{t.category ?? 'test'}]</span> {t.turns[t.turns.length - 1]}</li>
          ))}
        </ul>
      </details>
      {onDecide && (
        <div className="actions">
          <button className="primary" disabled={deciding} onClick={() => onDecide('approve')}>Approve and publish</button>
          <button className="ghost" disabled={deciding} onClick={() => onDecide('reject')}>Reject</button>
        </div>
      )}
    </section>
  )
}
