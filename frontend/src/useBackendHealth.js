import { useEffect, useState } from 'react'
import { api } from './api.js'

// Discovery only reads backend metadata. Retry until it succeeds without
// choosing a configuration store while the backend is unavailable.
export function useBackendHealth() {
  const [health, setHealth] = useState(null)
  const [error, setError] = useState('')
  const [attempt, setAttempt] = useState(0)
  useEffect(() => {
    let alive = true
    let timer
    const load = async () => {
      try {
        const data = await api.health()
        if (!alive) return
        setHealth(data)
        setError('')
      } catch (err) {
        if (!alive) return
        setError(err.message)
        timer = window.setTimeout(load, 3000)
      }
    }
    load()
    return () => { alive = false; window.clearTimeout(timer) }
  }, [attempt])
  return { health, error, retry: () => setAttempt(value => value + 1) }
}
