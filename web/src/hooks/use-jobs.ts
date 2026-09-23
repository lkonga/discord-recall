import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import { getJob, isJobActive, listJobs, type JobStatus } from '@/lib/api'
import { useAsync, type AsyncState } from '@/hooks/use-async'

/** Progress poll while a job is queued/running. */
export const JOB_POLL_MS = 1500
/** "Recent jobs" refresh interval, used only while some job is active. */
export const JOBS_LIST_POLL_MS = 5000
/**
 * Give up following one job after roughly an hour of polling. Work that is
 * genuinely stuck should surface as a message, not as an endless spinner.
 */
const MAX_WATCH_POLLS = Math.ceil((60 * 60_000) / JOB_POLL_MS)

export interface JobWatcherState {
  /** Latest snapshot of the watched job, null before the first poll. */
  job: JobStatus | null
  /** Set when a poll failed; polling stops at that point. */
  error: Error | null
  /** True while the 1.5s poll loop is running. */
  polling: boolean
}

/**
 * Follows one job until it reaches a terminal state.
 *
 * Polls `GET /api/jobs/<id>` every JOB_POLL_MS while the job is queued or
 * running, then stops and hands the final snapshot to `onSettled`. A failed
 * poll stops the loop as well and surfaces the error once, so a broken endpoint
 * cannot produce a stream of identical toasts.
 */
export function useJobWatcher(
  jobId: number | null,
  onSettled?: (job: JobStatus) => void,
): JobWatcherState {
  const [job, setJob] = useState<JobStatus | null>(null)
  const [error, setError] = useState<Error | null>(null)
  const [polling, setPolling] = useState(false)

  // The callback changes on every render (it closes over the current scope);
  // keep it in a ref so the poll loop never has to restart for it.
  const settledRef = useRef(onSettled)
  useEffect(() => {
    settledRef.current = onSettled
  })

  useEffect(() => {
    if (jobId === null) {
      setJob(null)
      setError(null)
      setPolling(false)
      return
    }

    let cancelled = false
    let timer: number | undefined
    let attempts = 0
    const controller = new AbortController()

    setJob(null)
    setError(null)
    setPolling(true)

    const tick = async () => {
      attempts += 1
      let next: JobStatus
      try {
        next = await getJob(jobId, controller.signal)
      } catch (caught) {
        if (cancelled) return
        if (caught instanceof DOMException && caught.name === 'AbortError') return
        setError(caught instanceof Error ? caught : new Error(String(caught)))
        setPolling(false)
        return
      }
      if (cancelled) return

      setJob(next)
      if (!isJobActive(next)) {
        setPolling(false)
        settledRef.current?.(next)
        return
      }
      if (attempts >= MAX_WATCH_POLLS) {
        setError(
          new Error(
            `stopped following job ${jobId} after an hour; it may still be running, check Recent jobs`,
          ),
        )
        setPolling(false)
        return
      }
      timer = window.setTimeout(() => void tick(), JOB_POLL_MS)
    }

    void tick()

    return () => {
      cancelled = true
      controller.abort()
      if (timer !== undefined) window.clearTimeout(timer)
    }
  }, [jobId])

  return { job, error, polling }
}

export interface RecentJobsState extends AsyncState<JobStatus[]> {
  jobs: JobStatus[]
  /** True while the 5s refresh loop runs (some job is queued or running). */
  polling: boolean
  /** True while at least one listed job is queued or running. */
  hasActive: boolean
}

/**
 * The "Recent jobs" list.
 *
 * Loads on mount and reloads every JOBS_LIST_POLL_MS while any listed job is
 * active; when everything is terminal it stays put until `reload()` is called
 * (after enqueuing a job, for example). A failed refresh stops the loop and
 * leaves the error for the caller to show once.
 */
export function useRecentJobs(limit = 8): RecentJobsState {
  const state = useAsync((signal) => listJobs({ limit }, signal), [limit])
  const jobs = useMemo(() => state.data ?? [], [state.data])
  const hasActive = useMemo(() => jobs.some(isJobActive), [jobs])
  const { reload, error } = state
  const [polling, setPolling] = useState(false)

  const reloadSoon = useCallback(() => {
    // Reloading immediately after enqueuing closes the gap between "job started
    // in this tab" and "job shows up in the list".
    reload()
  }, [reload])

  useEffect(() => {
    if (error !== null || !hasActive) {
      setPolling(false)
      return
    }
    setPolling(true)
    const timer = window.setInterval(reloadSoon, JOBS_LIST_POLL_MS)
    return () => {
      window.clearInterval(timer)
      setPolling(false)
    }
  }, [error, hasActive, reloadSoon])

  return { ...state, jobs, polling, hasActive }
}
