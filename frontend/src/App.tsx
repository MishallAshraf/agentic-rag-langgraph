import { useEffect, useState } from 'react'
import type { CSSProperties } from 'react'
import { listBots } from './api'
import type { Bot } from './api'
import { Avatar } from './Avatar'
import { Chat } from './Chat'

// The open bot lives in the URL (#/bot/<id>), so a chat can be bookmarked or
// shared, and the browser's back button returns to the list.
function botFromHash(): string | null {
  const match = window.location.hash.match(/^#\/bot\/([a-z0-9_]+)$/)
  return match ? match[1] : null
}

export default function App() {
  const [bots, setBots] = useState<Bot[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [botId, setBotId] = useState<string | null>(botFromHash)

  function fetchBots() {
    listBots().then(setBots).catch(() => setError("Can't reach the server. Start it with: python api.py"))
  }

  function retry() {
    setError(null)
    fetchBots()
  }

  useEffect(() => {
    fetchBots()
    const onHash = () => setBotId(botFromHash())
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [])

  const bot = bots?.find((b) => b.id === botId)

  useEffect(() => {
    document.title = bot ? bot.name : 'Bots'
  }, [bot])

  if (error) {
    return (
      <div className="page">
        <div className="alert" role="alert">{error}</div>
        <button className="ghost" onClick={retry}>Try again</button>
      </div>
    )
  }
  if (!bots) return <div className="page"><p className="muted">Loading…</p></div>
  if (bot) return <Chat key={bot.id} bot={bot} onBack={() => { window.location.hash = '' }} />

  return (
    <div className="page">
      <header className="page-header">
        <h1>Bots</h1>
        <p className="muted">Each bot answers only from its own documents.</p>
      </header>
      {botId && <div className="alert" role="alert">No bot called "{botId}".</div>}
      <ul className="bot-grid">
        {bots.map((b) => (
          <li key={b.id}>
            <a className="bot-card" href={`#/bot/${b.id}`}
               style={{ '--primary': b.theme.primary_color } as CSSProperties}>
              <Avatar name={b.theme.avatar} size={48} />
              <h2>{b.name}</h2>
              <p>{b.description}</p>
              {b.topics.length > 0 && <span className="muted small">{b.topics.length} topics</span>}
            </a>
          </li>
        ))}
      </ul>
    </div>
  )
}
