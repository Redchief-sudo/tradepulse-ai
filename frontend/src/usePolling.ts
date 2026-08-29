import { useCallback, useEffect, useRef, useState } from 'react'

interface PollingState<T> {
  data: T | null
  error: string | null
  loading: boolean
}

/** Fetches `fetcher` immediately, then every `intervalMs` -- the dashboard's
 * only "live update" mechanism (deliberately plain polling, no WebSocket/SSE
 * infrastructure in this pass). A failed poll keeps the last-good `data`
 * on screen and surfaces `error` alongside it, rather than blanking the
 * panel on a single transient broker hiccup. */
export function usePolling<T>(fetcher: () => Promise<T>, intervalMs: number): PollingState<T> & { refresh: () => void } {
  const [state, setState] = useState<PollingState<T>>({ data: null, error: null, loading: true })
  const fetcherRef = useRef(fetcher)
  fetcherRef.current = fetcher

  const run = useCallback(() => {
    fetcherRef
      .current()
      .then((data) => setState({ data, error: null, loading: false }))
      .catch((err: unknown) => setState((prev) => ({ data: prev.data, error: err instanceof Error ? err.message : String(err), loading: false })))
  }, [])

  useEffect(() => {
    run()
    const id = setInterval(run, intervalMs)
    return () => clearInterval(id)
  }, [run, intervalMs])

  return { ...state, refresh: run }
}
