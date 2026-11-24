import { useState } from 'react'
import type { FormEvent } from 'react'
import { ApiError, login } from './api'
import type { Me } from './api'

export function Login({ onSignedIn }: { onSignedIn: (me: Me) => void }) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  async function submit(e: FormEvent) {
    e.preventDefault()
    setBusy(true)
    setError(null)
    try {
      onSignedIn(await login(username.trim(), password))
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Can't reach the server. Is api.py running?")
      setPassword('')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="page login-page">
      <form className="login" onSubmit={submit}>
        <h1>Sign in</h1>
        <p className="muted">Your account is created by an administrator.</p>
        <label>
          Username
          <input value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="username"
                 autoCapitalize="none" spellCheck={false} required autoFocus />
        </label>
        <label>
          Password
          <input type="password" value={password} onChange={(e) => setPassword(e.target.value)}
                 autoComplete="current-password" required />
        </label>
        {error && <div className="alert" role="alert">{error}</div>}
        <button type="submit" className="primary" disabled={busy || !username || !password}>
          {busy ? 'Signing in…' : 'Sign in'}
        </button>
      </form>
    </div>
  )
}
