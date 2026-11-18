import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import { bootstrapToken } from './api/token'
import App from './App.tsx'

function applyBootTheme(): void {
  const stored = localStorage.getItem('gaia.theme')
  const theme =
    stored === 'light' || stored === 'dark' || stored === 'system'
      ? stored
      : 'dark'
  const dark =
    theme === 'dark' ||
    (theme === 'system' &&
      window.matchMedia('(prefers-color-scheme: dark)').matches)
  document.documentElement.classList.toggle('dark', dark)
}

applyBootTheme()
bootstrapToken()

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
