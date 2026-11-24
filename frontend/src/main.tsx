import { lazy, StrictMode, Suspense } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'
import { REPLAY } from './api'

const ReplayBanner = REPLAY ? lazy(() => import('./demo/ReplayBanner.tsx')) : null

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    {ReplayBanner && <Suspense><ReplayBanner /></Suspense>}
    <App />
  </StrictMode>,
)
