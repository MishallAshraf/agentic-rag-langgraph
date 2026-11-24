import { useEffect, useState } from 'react'
import type { CSSProperties } from 'react'
import { ApiError, listBots, logout, me } from './api'
import type { Bot, Me } from './api'
import { Avatar } from './Avatar'
import { Chat } from './Chat'
import { Login } from './Login'

// The open bot lives in the URL (#/bot/<id>), so a chat can be bookmarked or
// shared, and the browser's back button returns to the list.
function botFromHash(): string | null {
  const match = window.location.hash.match(/^#\/bot\/([a-z0-9_]+)$/)
  return match ? match[1] : null
}

export default function App() {
  const [user, setUser] = useState<Me | null | undefined>(undefined) // undefined: still checking
  const [bots, setBots] = useState<Bot[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [botId, setBotId] = useState<string | null>(botFromHash)

  function signedOut() {
    setUser(null)
    setBots(null)
  }

  function fetchBots() {
    listBots().then(setBots).catch((e) => {
      if (e instanceof ApiError && e.status === 401) signedOut()
      else setError("Can't reach the server. Start it with: python api.py")
    })
  }

  function start() {
    me().then((u) => {
      setUser(u)
      if (u) fetchBots()
    }).catch(() => setError("Can't reach the server. Start it with: python api.py"))
  }

  function retry() {
    setError(null)
    start()
  }

  function onSignedIn(u: Me) {
    setUser(u)
    fetchBots()
  }

  async function signOut() {
    await logout()
    signedOut()
  }

  // Keeps the usage figures current after each answer.
  function refreshUsage() {
    me().then((u) => (u ? setUser(u) : signedOut())).catch(() => {})
  }

  useEffect(() => {
    start()
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
  if (user === null) return <Login onSignedIn={onSignedIn} />
  if (user === undefined || !bots) return <div className="page"><p className="muted">Loading…</p></div>
  if (bot) {
    return <Chat key={bot.id} bot={bot} onBack={() => { window.location.hash = '' }}
                 onSignedOut={signedOut} onTurnEnd={refreshUsage} />
  }

  const { usage } = user
  return (
    <div className="page">
      <header className="page-header">
        <div>
          <h1>Bots</h1>
          <p className="muted">Each bot answers only from its own documents.</p>
        </div>
        <div className="account">
          <span>
            Signed in as <strong>{user.username}</strong>
            {user.role === 'admin' && <span className="badge">admin</span>}
          </span>
          <span className="muted small">Today: {usage.turns} of {usage.turn_limit} messages</span>
          <button className="ghost" onClick={signOut}>Sign out</button>
        </div>
      </header>
      {botId && !bot && <div className="alert" role="alert">No bot called "{botId}" for this account.</div>}
      {bots.length === 0 && <p className="muted">No bots have been shared with you yet. Ask an administrator.</p>}
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
