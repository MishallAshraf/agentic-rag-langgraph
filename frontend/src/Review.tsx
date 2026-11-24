import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { ApiError, approveTaxonomy, assignModes, getAnalysis, listAnalyses, saveLabel } from './api'
import type { Analysis, AnalysisSummary, Step, Taxonomy, TraceItem } from './api'

// Step 17: error analysis for admins. #/review lists bots with conversations to
// review; #/review/<bot> is where a domain expert labels them (open coding),
// corrects the failure modes (axial coding) and reads how well the judges agree.

const STEP_TEXT: Record<Step, string> = {
  input_check: 'Input check: refused or misread the message',
  search: 'Search: missed or found the wrong passages',
  answer: 'Answer: the reply itself',
  fact_check: 'Fact check: let an unsupported claim through',
  other: 'Other',
}

type Props = { botId: string | null; onSignedOut: () => void }

export function Review({ botId, onSignedOut }: Props) {
  return (
    <div className="page review">
      <header className="page-header">
        <div>
          <a className="small" href={botId ? '#/review' : '#/'}>{botId ? '← All analyses' : '← All bots'}</a>
          <h1>Error analysis{botId ? `: ${botId}` : ''}</h1>
          <p className="muted">Read each conversation as the business owner would. Your notes become the bot's failure modes and its tests.</p>
        </div>
      </header>
      {botId ? <AnalysisView key={botId} botId={botId} onSignedOut={onSignedOut} /> : <AnalysisList onSignedOut={onSignedOut} />}
    </div>
  )
}

function useErrors(onSignedOut: () => void) {
  const [error, setError] = useState<string | null>(null)
  const signOut = useRef(onSignedOut)
  useEffect(() => { signOut.current = onSignedOut }, [onSignedOut])
  // Stable across renders, so effects that load data can depend on it without reloading.
  const fail = useCallback((e: unknown) => {
    if (e instanceof ApiError && e.status === 401) signOut.current()
    else setError(e instanceof ApiError ? e.message : "Can't reach the server.")
  }, [])
  return { error, setError, fail }
}

function AnalysisList({ onSignedOut }: { onSignedOut: () => void }) {
  const [items, setItems] = useState<AnalysisSummary[] | null>(null)
  const { error, fail } = useErrors(onSignedOut)
  useEffect(() => { listAnalyses().then(setItems).catch(fail) }, [fail])
  if (error) return <div className="alert" role="alert">{error}</div>
  if (!items) return <p className="muted">Loading…</p>
  if (items.length === 0) {
    return <p className="muted">No conversations to review yet. Create them with <code>python analysis.py run --bot bots/&lt;bot&gt;.yaml</code>.</p>
  }
  return (
    <ul className="analysis-list">
      {items.map((a) => (
        <li key={a.bot_id}>
          <a className="bot-card" href={`#/review/${a.bot_id}`}>
            <h2>{a.bot_id}</h2>
            <p>{a.labelled} of {a.traces} labelled, {a.failed} failed</p>
            <span className="muted small">Failure modes: {a.taxonomy_status ?? 'not yet'} · Judges: {a.judges_built ? 'built' : 'not yet'}</span>
          </a>
        </li>
      ))}
    </ul>
  )
}

type Tab = 'label' | 'modes' | 'judges'
type Filter = 'all' | 'todo' | 'fail' | 'pass'

function AnalysisView({ botId, onSignedOut }: { botId: string; onSignedOut: () => void }) {
  const [data, setData] = useState<Analysis | null>(null)
  const [tab, setTab] = useState<Tab>('label')
  const [selected, setSelected] = useState<string | null>(null)
  const [filter, setFilter] = useState<Filter>('all')
  const { error, setError, fail } = useErrors(onSignedOut)

  useEffect(() => {
    getAnalysis(botId).then((d) => {
      setData(d)
      setSelected((d.items.find((i) => !i.label) ?? d.items[0])?.id ?? null)
    }).catch(fail)
  }, [botId, fail])

  const shown = useMemo(() => (data?.items ?? []).filter((i) =>
    filter === 'all' ? true : filter === 'todo' ? !i.label : i.label?.verdict === filter), [data, filter])

  if (!data) return error ? <div className="alert" role="alert">{error}</div> : <p className="muted">Loading…</p>
  const item = data.items.find((i) => i.id === selected) ?? null

  function update(changes: Partial<Analysis>) {
    setData((d) => (d ? { ...d, ...changes } : d))
  }

  async function onSave(it: TraceItem, verdict: 'pass' | 'fail', note: string, first: Step | null) {
    setError(null)
    try {
      const r = await saveLabel(botId, it.id, verdict, note, first)
      const items = data!.items.map((i) => (i.id === it.id ? { ...i, label: r.label } : i))
      update({ ...r.summary, items, taxonomy: r.taxonomy })
      const order = items.map((i) => i.id)
      const next = items.find((i, n) => n > order.indexOf(it.id) && !i.label) ?? items.find((i) => !i.label)
      if (next) {
        setSelected(next.id)
        window.scrollTo({ top: 0 })  // the next conversation starts at its first message
      }
    } catch (e) {
      fail(e)
    }
  }

  return (
    <div className="analysis">
      <div className="analysis-bar">
        <div className="tabs" role="tablist">
          {(['label', 'modes', 'judges'] as Tab[]).map((t) => (
            <button key={t} role="tab" aria-selected={tab === t} className={tab === t ? 'tab active' : 'tab'}
                    onClick={() => setTab(t)}>
              {t === 'label' ? 'Label conversations' : t === 'modes' ? 'Failure modes' : 'Judges'}
            </button>
          ))}
        </div>
        <span className="muted small">{data.labelled} of {data.traces} labelled · {data.failed} failed</span>
      </div>
      {error && <div className="alert" role="alert">{error}</div>}

      {tab === 'label' && (
        <div className="label-layout">
          <aside className="trace-list">
            <select value={filter} onChange={(e) => setFilter(e.target.value as Filter)} aria-label="Show">
              <option value="all">All ({data.items.length})</option>
              <option value="todo">Not labelled ({data.traces - data.labelled})</option>
              <option value="fail">Failed ({data.failed})</option>
              <option value="pass">Passed ({data.labelled - data.failed})</option>
            </select>
            <ul>
              {shown.map((i) => (
                <li key={i.id}>
                  <button className={i.id === selected ? 'trace-row active' : 'trace-row'} onClick={() => setSelected(i.id)}
                          aria-current={i.id === selected} aria-label={`${i.id}, ${i.label?.verdict ?? 'not labelled'}: ${i.message}`}>
                    <span className={`mark ${i.label?.verdict ?? 'todo'}`} aria-hidden="true">
                      {i.label ? (i.label.verdict === 'pass' ? '✓' : '✗') : '•'}
                    </span>
                    <span className="trace-row-text">{i.id} · {i.message}</span>
                  </button>
                </li>
              ))}
            </ul>
          </aside>
          <section className="trace-main">
            <details className="guide">
              <summary>How to label</summary>
              <ul>
                <li>Judge as the business owner: would you be happy for a customer to receive this reply?</li>
                <li>Fail it for anything that matters: a wrong fact, a missed allergy warning, a bent policy, a refusal it shouldn't make, an unhelpful or rude tone.</li>
                <li>Write what went wrong in your own words, specifically ("said the carrot cake is nut-free"), not a category. Categories come later, from your notes.</li>
                <li>Pick the first step that went wrong: if the search found nothing useful, the bad answer is a symptom.</li>
              </ul>
            </details>
            {item ? <TraceCard key={item.id} item={item} steps={data.steps} onSave={onSave} /> : <p className="muted">Nothing to show.</p>}
          </section>
        </div>
      )}
      {tab === 'modes' && <Modes data={data} botId={botId} onChange={(taxonomy) => update({ taxonomy, taxonomy_status: taxonomy.status })} onError={fail} />}
      {tab === 'judges' && <JudgesView data={data} />}
    </div>
  )
}

function TraceCard({ item, steps, onSave }: { item: TraceItem; steps: Step[];
                    onSave: (it: TraceItem, verdict: 'pass' | 'fail', note: string, first: Step | null) => Promise<void> }) {
  const [verdict, setVerdict] = useState<'pass' | 'fail' | null>(item.label?.verdict ?? null)
  const [note, setNote] = useState(item.label?.note ?? '')
  const [first, setFirst] = useState<Step | ''>(item.label?.first_failure ?? '')
  const [saving, setSaving] = useState(false)
  const ready = verdict === 'pass' || (verdict === 'fail' && note.trim().length >= 3 && first !== '')

  async function save() {
    if (!verdict) return
    setSaving(true)
    await onSave(item, verdict, note, verdict === 'fail' ? (first || null) : null)
    setSaving(false)
  }

  return (
    <article className="trace-card">
      <div className="profile">
        {Object.entries(item.profile).map(([k, v]) => <span key={k} className="chip" title={k}>{v}</span>)}
      </div>
      <div className="bubble user">{item.message}</div>
      <div className="bubble bot reply">{item.answer || <em>No reply: {item.error}</em>}</div>
      <details className="steps">
        <summary>What the bot did ({item.seconds} s, {item.models.join(', ') || 'no model recorded'})</summary>
        <ol>
          {item.steps.map((s, n) => (
            <li key={n}>
              {s.step === 'input_check' && <>Input check: <strong>{s.result}</strong></>}
              {s.step === 'tool_call' && <>Called <code>{s.tool}</code> with <code>{JSON.stringify(s.args)}</code></>}
              {s.step === 'tool_result' && (
                <details><summary><code>{s.tool}</code> returned {s.text.length} characters</summary><pre>{s.text}</pre></details>
              )}
              {s.step === 'fact_check' && <>Fact check: {s.revisions === 0 ? 'passed first time' : `sent the answer back ${s.revisions} time(s)`}</>}
            </li>
          ))}
        </ol>
      </details>
      <div className="label-form">
        <div className="verdict" role="radiogroup" aria-label="Verdict">
          <button role="radio" aria-checked={verdict === 'pass'} className={verdict === 'pass' ? 'pass active' : 'pass'}
                  onClick={() => setVerdict('pass')}>✓ Pass</button>
          <button role="radio" aria-checked={verdict === 'fail'} className={verdict === 'fail' ? 'fail active' : 'fail'}
                  onClick={() => setVerdict('fail')}>✗ Fail</button>
        </div>
        <label>
          {verdict === 'fail' ? 'What went wrong?' : 'Note (optional)'}
          <textarea rows={3} maxLength={1000} value={note} onChange={(e) => setNote(e.target.value)}
                    placeholder={verdict === 'fail' ? 'In your own words, e.g. "promised the soup is vegan without checking"' : ''} />
        </label>
        {verdict === 'fail' && (
          <label>
            Where did it first go wrong?
            <select value={first} onChange={(e) => setFirst(e.target.value as Step)}>
              <option value="">Choose…</option>
              {steps.map((s) => <option key={s} value={s}>{STEP_TEXT[s]}</option>)}
            </select>
          </label>
        )}
        <button className="primary" disabled={!ready || saving} onClick={save}>{saving ? 'Saving…' : 'Save and next'}</button>
        {item.label && <span className="muted small">Saved by {item.label.by} at {item.label.at.replace('T', ' ')}</span>}
      </div>
    </article>
  )
}

function Modes({ data, botId, onChange, onError }: { data: Analysis; botId: string;
                 onChange: (t: Taxonomy) => void; onError: (e: unknown) => void }) {
  const tax = data.taxonomy
  const failed = data.items.filter((i) => i.label?.verdict === 'fail')
  if (!tax) {
    return (
      <div className="notice">
        <p>Label the conversations first, then group your notes into failure modes:</p>
        <code>python analysis.py taxonomy --bot bots/{botId}.yaml</code>
        <p className="muted small">{failed.length} failure notes so far. More notes give clearer modes.</p>
      </div>
    )
  }
  const counts: Record<string, number> = {}
  for (const modes of Object.values(tax.assignments)) for (const m of modes) counts[m] = (counts[m] ?? 0) + 1
  const unassigned = failed.filter((i) => !(tax.assignments[i.id] ?? []).length)

  async function toggle(traceId: string, mode: string, on: boolean) {
    const current = tax!.assignments[traceId] ?? []
    try {
      onChange(await assignModes(botId, traceId, on ? [...current, mode] : current.filter((m) => m !== mode)))
    } catch (e) { onError(e) }
  }

  async function approve() {
    try { onChange(await approveTaxonomy(botId)) } catch (e) { onError(e) }
  }

  return (
    <div className="modes">
      <div className="modes-head">
        <p>
          Status: <span className={`badge status-${tax.status}`}>{tax.status}</span>
          {tax.approved_by && <span className="muted small"> by {tax.approved_by}</span>}
        </p>
        <button className="primary" disabled={tax.status === 'approved' || unassigned.length > 0} onClick={approve}>
          Approve failure modes
        </button>
      </div>
      {unassigned.length > 0 && <div className="alert">{unassigned.length} failed conversation(s) have no failure mode yet.</div>}
      <dl className="mode-list">
        {tax.modes.map((m) => (
          <div key={m.id}>
            <dt>{m.name} <span className="muted small">({counts[m.id] ?? 0})</span></dt>
            <dd>{m.definition}</dd>
          </div>
        ))}
      </dl>
      <h2>Failed conversations</h2>
      <ul className="assignments">
        {failed.map((i) => (
          <li key={i.id} className={(tax.assignments[i.id] ?? []).length ? '' : 'unassigned'}>
            <p><strong>{i.id}</strong> · {i.message}</p>
            <p className="muted small">Your note: {i.label!.note}</p>
            <div className="mode-boxes">
              {tax.modes.map((m) => (
                <label key={m.id}>
                  <input type="checkbox" checked={(tax.assignments[i.id] ?? []).includes(m.id)}
                         onChange={(e) => toggle(i.id, m.id, e.target.checked)} />
                  {m.name}
                </label>
              ))}
            </div>
          </li>
        ))}
      </ul>
    </div>
  )
}

function fmt(x: number | null | undefined) {
  return x === null || x === undefined ? '–' : x.toFixed(2)
}

function JudgesView({ data }: { data: Analysis }) {
  const j = data.judges
  if (!j) {
    return (
      <div className="notice">
        <p>After you approve the failure modes, build one judge per mode and measure it on your labels:</p>
        <code>python analysis.py judges --bot bots/{data.bot_id}.yaml</code>
      </div>
    )
  }
  return (
    <div className="judges">
      <p className="muted small">Judge model {j.model}. Labels split into {Object.entries(j.splits).map(([k, v]) => `${k} ${v}`).join(', ')}: the judges saw examples from train only.</p>
      <table>
        <thead>
          <tr><th>Failure mode</th><th>Dev TPR</th><th>Dev TNR</th><th>Test TPR</th><th>Test TNR</th>
            <th>Test: your rate</th><th>Judge's rate</th><th>Corrected</th></tr>
        </thead>
        <tbody>
          {Object.entries(j.modes).map(([id, m]) => (
            <tr key={id}>
              <td>{m.name}</td><td>{fmt(m.dev.tpr)}</td><td>{fmt(m.dev.tnr)}</td><td>{fmt(m.test.tpr)}</td>
              <td>{fmt(m.test.tnr)}</td><td>{fmt(m.test_rates?.expert)}</td><td>{fmt(m.test_rates?.judge)}</td>
              <td>{fmt(m.test_rates?.corrected)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="muted small">TPR: of the failures you found, the share the judge also finds. TNR: of the conversations without that failure, the share it leaves alone. Both should be high before the judge's numbers are trusted.</p>
    </div>
  )
}
