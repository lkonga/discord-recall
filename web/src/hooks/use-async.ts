import { useCallback, useEffect, useRef, useState } from 'react'

export interface AsyncState<T> {
  data: T | null
  error: Error | null
  loading: boolean
  /** True while a reload is in flight after the first successful load. */
  refreshing: boolean
  reload: () => void
}

/**
 * Runs an async loader whenever `deps` change.
 *
 * The previous data stays visible during a reload (only `refreshing` flips), a
 * failed load keeps the stale data and surfaces `error`, and in-flight requests
 * are aborted when the deps change or the component unmounts.
 */
export function useAsync<T>(
  loader: (signal: AbortSignal) => Promise<T>,
  deps: readonly unknown[],
  options: { enabled?: boolean } = {},
): AsyncState<T> {
  const enabled = options.enabled ?? true
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<Error | null>(null)
  const [loading, setLoading] = useState(enabled)
  const [refreshing, setRefreshing] = useState(false)
  const [nonce, setNonce] = useState(0)
  const settledOnceRef = useRef(false)

  const reload = useCallback(() => setNonce((value) => value + 1), [])

  useEffect(() => {
    if (!enabled) {
      settledOnceRef.current = false
      setData(null)
      setError(null)
      setLoading(false)
      setRefreshing(false)
      return
    }

    const controller = new AbortController()
    let cancelled = false

    Promise.resolve()
      .then(() => {
        if (cancelled) return undefined
        if (settledOnceRef.current) setRefreshing(true)
        else setLoading(true)
        return loader(controller.signal)
      })
      .then((result) => {
        if (cancelled || result === undefined) return
        settledOnceRef.current = true
        setData(result)
        setError(null)
      })
      .catch((caught: unknown) => {
        if (cancelled) return
        if (caught instanceof DOMException && caught.name === 'AbortError') return
        setError(caught instanceof Error ? caught : new Error(String(caught)))
      })
      .finally(() => {
        if (cancelled) return
        setLoading(false)
        setRefreshing(false)
      })

    return () => {
      cancelled = true
      controller.abort()
    }
    // `deps` is the caller-supplied dependency list; the loader closure always
    // matches it, so the loader itself is intentionally not a dependency.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, nonce, ...deps])

  return { data, error, loading, refreshing, reload }
}
