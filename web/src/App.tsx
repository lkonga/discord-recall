import { lazy, Suspense, useCallback, useEffect, useMemo, useState } from 'react'
import { Loader2Icon, RefreshCwIcon, ServerIcon } from 'lucide-react'
import { toast, Toaster } from 'sonner'

import { ActivityChart } from '@/components/activity-chart'
import { ChannelCombobox } from '@/components/channel-combobox'
import { JobProgressCard } from '@/components/job-progress'
import { JobsPanel } from '@/components/jobs-panel'
import { RunPanel, type LastRun, type RangeSelection } from '@/components/run-panel'
import { ServersSidebar } from '@/components/servers-sidebar'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader } from '@/components/ui/card'
import { Separator } from '@/components/ui/separator'
import { Skeleton } from '@/components/ui/skeleton'
import { useAsync } from '@/hooks/use-async'
import { useDebouncedValue } from '@/hooks/use-debounced-value'
import { useJobWatcher, useRecentJobs } from '@/hooks/use-jobs'
import {
  fetchChannelActivity,
  fetchChannelDigests,
  isJobActive,
  listChannels,
  listServers,
  queueCapture,
  queueDigest,
  queueDiscover,
  type ChannelSummary,
  type JobKind,
  type JobStatus,
  type Period,
} from '@/lib/api'
import { formatCompact, formatDateTime, relativeFromNow, toIsoDay } from '@/lib/format'
import { scopeRangeLabel, summariseScope, windowPresetFor, type Scope } from '@/lib/job-scope'
import { jobHeadline } from '@/lib/job-text'

// The markdown renderer is only needed once a channel is open, so it is split
// out of the initial bundle (react-markdown + remark-gfm are both chunky).
const AskCard = lazy(() =>
  import('@/components/ask-card').then((module) => ({ default: module.AskCard })),
)
const DigestBrowser = lazy(() =>
  import('@/components/digest-browser').then((module) => ({ default: module.DigestBrowser })),
)

const DEFAULT_ACTIVITY_WINDOW = 90

/** The run this tab is following, with everything needed to describe it. */
interface WatchedRun {
  jobId: number
  kind: JobKind
  channelId: string | null
  channelName: string | null
  /** Scope captured when the run was queued; null for jobs adopted from the list. */
  scope: Scope | null
  period: Period
}

export default function App() {
  const [selectedServerId, setSelectedServerId] = useState<string | null>(null)
  const [selectedChannelId, setSelectedChannelId] = useState<string | null>(null)
  const [nudgeChannelPicker, setNudgeChannelPicker] = useState(false)
  const [channelQuery, setChannelQuery] = useState('')
  const [windowDays, setWindowDays] = useState(DEFAULT_ACTIVITY_WINDOW)
  const [range, setRange] = useState<RangeSelection>({ from: null, to: null })
  const [period, setPeriod] = useState<Period>('daily')
  const [force, setForce] = useState(false)
  const [maxMessages, setMaxMessages] = useState(600)
  /** Only set while the enqueue POST is in flight: jobs never block a button. */
  const [enqueuing, setEnqueuing] = useState<'capture' | 'digest' | 'discover' | null>(null)
  const [watched, setWatched] = useState<WatchedRun | null>(null)
  const [lastRun, setLastRun] = useState<LastRun | null>(null)
  /** Inline refusal from the API (e.g. a digest range with no messages). */
  const [notice, setNotice] = useState<string | null>(null)
  const [channelNames, setChannelNames] = useState<Record<string, string>>({})

  const debouncedQuery = useDebouncedValue(channelQuery, 220)

  const serversState = useAsync((signal) => listServers(signal), [])
  const servers = useMemo(() => serversState.data ?? [], [serversState.data])

  const channelsState = useAsync(
    (signal) =>
      listChannels(
        { server: selectedServerId ?? undefined, q: debouncedQuery || undefined, limit: 2000 },
        signal,
      ),
    [selectedServerId, debouncedQuery],
    { enabled: selectedServerId !== null },
  )
  const channels = useMemo(() => channelsState.data ?? [], [channelsState.data])

  const activityState = useAsync(
    (signal) => fetchChannelActivitySafe(selectedChannelId, windowDays, signal),
    [selectedChannelId, windowDays],
    { enabled: selectedChannelId !== null },
  )

  const digestsState = useAsync(
    (signal) => fetchChannelDigestsSafe(selectedChannelId, signal),
    [selectedChannelId],
    { enabled: selectedChannelId !== null },
  )
  const digests = digestsState.data ?? []

  // Queue state: the list refreshes itself every 5s while anything is active.
  const recentJobs = useRecentJobs(8)

  const selectedChannel = useMemo<ChannelSummary | null>(() => {
    if (selectedChannelId === null) return null
    return channels.find((channel) => channel.id === selectedChannelId) ?? null
  }, [channels, selectedChannelId])

  const selectedServerName = useMemo(() => {
    if (selectedServerId === null) return null
    return (
      servers.find((server) => server.id === selectedServerId)?.name ??
      channels[0]?.serverName ??
      selectedChannel?.serverName ??
      null
    )
  }, [servers, channels, selectedServerId, selectedChannel])

  // Job rows only carry a channel id, so remember every name we have seen.
  useEffect(() => {
    const seen = [selectedChannel, ...channels].filter(
      (channel): channel is ChannelSummary => channel !== null,
    )
    if (seen.length === 0) return
    setChannelNames((previous) => {
      let changed = false
      const next = { ...previous }
      for (const channel of seen) {
        if (next[channel.id] !== channel.name) {
          next[channel.id] = channel.name
          changed = true
        }
      }
      return changed ? next : previous
    })
  }, [channels, selectedChannel])

  const channelLabel = useCallback(
    (channelId: string | null) => {
      if (channelId === null) return '—'
      return channelNames[channelId] ?? channelId
    },
    [channelNames],
  )

  /** Selecting a channel resets the range so the user starts from real activity. */
  const selectChannel = useCallback((channel: ChannelSummary | null) => {
    setSelectedChannelId(channel?.id ?? null)
    setRange({ from: null, to: null })
    setNotice(null)
  }, [])

  /** First click starts a range, second click closes it (clicking earlier flips it). */
  const pickDay = useCallback((day: string) => {
    setRange((previous) => {
      if (!previous.from || previous.to) return { from: day, to: null }
      return day >= previous.from
        ? { from: previous.from, to: day }
        : { from: day, to: previous.from }
    })
  }, [])

  const changeRange = useCallback((next: RangeSelection) => {
    setRange(next)
    setNotice(null)
  }, [])

  const scopeFor = useCallback(
    (from: string | null, to: string | null, selectedPeriod: Period) =>
      summariseScope({
        activity: activityState.data,
        from,
        to,
        period: selectedPeriod,
        windowDays,
      }),
    [activityState.data, windowDays],
  )

  /**
   * Completion path: stop polling (the hook already did), refresh everything the
   * run touched, keep the per-period outcome in the panel and toast once.
   */
  const handleSettled = useCallback(
    (job: JobStatus) => {
      recentJobs.reload()
      serversState.reload()
      if (job.kind === 'discover') {
        if (selectedServerId !== null) channelsState.reload()
      } else {
        channelsState.reload()
        if (job.channelId !== null && job.channelId === selectedChannelId) {
          activityState.reload()
          digestsState.reload()
        }
      }

      if (watched && watched.scope && job.kind !== 'discover' && job.channelId !== null) {
        setLastRun({ job, scope: watched.scope, channelId: job.channelId, period: watched.period })
      }

      const headline = jobHeadline(
        job,
        job.channelId === null ? null : (channelNames[job.channelId] ?? null),
      )
      if (headline.tone === 'error') {
        toast.error(headline.title, { description: headline.description })
      } else {
        toast.success(headline.title, { description: headline.description })
      }
    },
    [
      activityState,
      channelNames,
      channelsState,
      digestsState,
      recentJobs,
      selectedChannelId,
      selectedServerId,
      serversState,
      watched,
    ],
  )

  const watcher = useJobWatcher(watched?.jobId ?? null, handleSettled)

  const activeJobs = useMemo(() => recentJobs.jobs.filter(isJobActive), [recentJobs.jobs])
  /** A job queued in this tab is "active" before the list has caught up. */
  const trackedActive = watched !== null && (watcher.job === null || isJobActive(watcher.job))

  const isRunningFor = useCallback(
    (kind: JobKind, channelId: string | null) =>
      activeJobs.some((job) => job.kind === kind && job.channelId === channelId) ||
      (trackedActive && watched?.kind === kind && watched.channelId === channelId),
    [activeJobs, trackedActive, watched],
  )

  const captureRunning = isRunningFor('capture', selectedChannelId)
  const digestRunning = isRunningFor('digest', selectedChannelId)
  const discoverRunning = isRunningFor('discover', null)

  useEffect(() => {
    if (serversState.error) toast.error(`Could not load servers: ${serversState.error.message}`)
  }, [serversState.error])

  useEffect(() => {
    if (channelsState.error) toast.error(`Could not load channels: ${channelsState.error.message}`)
  }, [channelsState.error])

  useEffect(() => {
    if (activityState.error) toast.error(`Could not load activity: ${activityState.error.message}`)
  }, [activityState.error])

  useEffect(() => {
    if (digestsState.error) toast.error(`Could not load digests: ${digestsState.error.message}`)
  }, [digestsState.error])

  // A failed poll is reported once; the hooks stop polling at that point.
  useEffect(() => {
    if (recentJobs.error) {
      toast.error(`Could not read the job list: ${recentJobs.error.message}`)
    }
  }, [recentJobs.error])

  useEffect(() => {
    if (watcher.error) {
      toast.error(`Lost track of the running job: ${watcher.error.message}`)
    }
  }, [watcher.error])

  /** Shared tail of every enqueue: adopt the job, refresh the list, announce it. */
  const adoptJob = useCallback(
    (jobId: number, run: Omit<WatchedRun, 'jobId'>) => {
      setWatched({ jobId, ...run })
      recentJobs.reload()
    },
    [recentJobs],
  )

  const reportRefusal = useCallback((reason: string) => {
    setNotice(reason)
    toast.error('Nothing was queued', { description: reason })
  }, [])

  const runCapture = async () => {
    if (!selectedChannel) return
    setEnqueuing('capture')
    setNotice(null)
    const scope = scopeFor(range.from, range.to, period)
    try {
      const result = await queueCapture({
        channelId: selectedChannel.id,
        since: range.from ?? undefined,
        until: range.to ?? undefined,
        maxMessages,
      })
      if (result.jobId === null) {
        reportRefusal(result.status)
        return
      }
      adoptJob(result.jobId, {
        kind: 'capture',
        channelId: selectedChannel.id,
        channelName: selectedChannel.name,
        scope,
        period,
      })
      toast.message('Capture queued', {
        description: `${scopeRangeLabel(scope)} · progress is shown above, the buttons stay usable`,
      })
    } catch (error) {
      toast.error(error instanceof Error ? error.message : 'Capture could not be queued')
    } finally {
      setEnqueuing(null)
    }
  }

  const runDigest = async () => {
    if (!selectedChannel || !range.from) return
    setEnqueuing('digest')
    setNotice(null)
    const scope = scopeFor(range.from, range.to, period)
    try {
      const result = await queueDigest({
        channelId: selectedChannel.id,
        from: range.from,
        to: range.to ?? undefined,
        period,
        force,
      })
      if (result.jobId === null) {
        reportRefusal(result.status)
        return
      }
      adoptJob(result.jobId, {
        kind: 'digest',
        channelId: selectedChannel.id,
        channelName: selectedChannel.name,
        scope,
        period,
      })
      toast.message('Digest queued', {
        description: `${scopeRangeLabel(scope)} · ${period} · progress is shown above`,
      })
    } catch (error) {
      toast.error(error instanceof Error ? error.message : 'Digest could not be queued')
    } finally {
      setEnqueuing(null)
    }
  }

  const runDiscover = async () => {
    setEnqueuing('discover')
    try {
      const result = await queueDiscover()
      if (result.jobId === null) {
        reportRefusal(result.status)
        return
      }
      adoptJob(result.jobId, {
        kind: 'discover',
        channelId: null,
        channelName: null,
        scope: null,
        period,
      })
      toast.message('Discover queued', {
        description: 're-reading servers and channels from Discord in the background',
      })
    } catch (error) {
      toast.error(error instanceof Error ? error.message : 'Discover could not be queued')
    } finally {
      setEnqueuing(null)
    }
  }

  const widenWindow = useCallback((days: number) => setWindowDays(windowPresetFor(days)), [])

  /** Shown until the first poll lands, so a queue action has instant feedback. */
  const progressJob: JobStatus | null =
    watcher.job ??
    (watched
      ? {
          id: watched.jobId,
          kind: watched.kind,
          status: 'queued',
          phase: 'queued',
          messages: 0,
          percent: null,
          message: '',
          channelId: watched.channelId,
          createdAt: null,
          updatedAt: null,
          finishedAt: null,
        }
      : null)

  const progressScopeNote = watched?.scope
    ? `${scopeRangeLabel(watched.scope)} · ${watched.period} · ${watched.scope.dayCount} day${watched.scope.dayCount === 1 ? '' : 's'} · ${formatCompact(watched.scope.activeDays)} with messages`
    : null

  const today = toIsoDay(new Date())

  return (
    <div className="flex min-h-screen flex-col bg-background text-foreground">
      <header className="flex flex-wrap items-center gap-3 border-b border-border px-4 py-3">
        <div className="flex items-center gap-2">
          <span className="grid size-7 place-items-center rounded-md bg-primary text-primary-foreground">
            <ServerIcon className="size-4" />
          </span>
          <div className="flex flex-col">
            <h1 className="text-sm font-semibold leading-tight">Discord Recall</h1>
            <span className="text-[11px] text-muted-foreground">
              server → channel → activity → capture → digest
            </span>
          </div>
        </div>
        <div className="ml-auto flex items-center gap-2">
          {activeJobs.length > 0 && (
            <span className="flex items-center gap-1 text-[11px] text-muted-foreground">
              <Loader2Icon className="size-3 animate-spin" />
              {activeJobs.length} job{activeJobs.length === 1 ? '' : 's'} running
            </span>
          )}
          {serversState.refreshing && (
            <span className="flex items-center gap-1 text-[11px] text-muted-foreground">
              <Loader2Icon className="size-3 animate-spin" /> reloading
            </span>
          )}
          <Button
            variant="outline"
            size="sm"
            onClick={() => void runDiscover()}
            disabled={enqueuing === 'discover' || discoverRunning}
            title={
              discoverRunning
                ? 'a discover job is already queued or running'
                : 'queue a background discover run'
            }
          >
            {enqueuing === 'discover' ? (
              <Loader2Icon className="animate-spin" />
            ) : (
              <RefreshCwIcon className="size-3.5" />
            )}
            Refresh channel list from Discord
          </Button>
        </div>
      </header>

      <div className="flex flex-1 flex-col md:flex-row">
        <ServersSidebar
          servers={servers}
          loading={serversState.loading}
          error={serversState.error}
          refreshing={serversState.refreshing}
          selectedServerId={selectedServerId}
          onSelectServer={(serverId) => {
            setSelectedServerId(serverId)
            setChannelQuery('')
            setSelectedChannelId(null)
            setRange({ from: null, to: null })
            setNudgeChannelPicker(true)
            setNotice(null)
          }}
          onRefresh={serversState.reload}
        />

        <main className="flex min-w-0 flex-1 flex-col gap-4 p-4">
          <div className="flex flex-col gap-2">
            <div className="flex flex-wrap items-center gap-2">
              <ChannelCombobox
                channels={channels}
                selectedChannelId={selectedChannelId}
                serverName={selectedServerName}
                loading={channelsState.loading || channelsState.refreshing}
                query={channelQuery}
                onQueryChange={setChannelQuery}
                onSelect={selectChannel}
                autoOpen={nudgeChannelPicker}
                onAutoOpen={() => setNudgeChannelPicker(false)}
              />
              {selectedServerId === null && (
                <span className="text-xs text-muted-foreground">
                  Step 1: choose a server in the sidebar.
                </span>
              )}
            </div>

            {selectedChannel && (
              <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                <Badge variant="secondary" className="tabular-nums">
                  {formatCompact(selectedChannel.messages)} messages
                </Badge>
                <Badge variant="outline" className="tabular-nums">
                  {formatCompact(selectedChannel.digests)} digests
                </Badge>
                <span>
                  {selectedChannel.messages > 0
                    ? `latest message ${relativeFromNow(selectedChannel.lastMessage)}`
                    : 'nothing captured for this channel yet'}
                </span>
                {selectedChannel.latestDigest && (
                  <span>· newest digest {formatDateTime(selectedChannel.latestDigest)}</span>
                )}
              </div>
            )}
          </div>

          {progressJob && (
            <JobProgressCard
              job={progressJob}
              channelName={
                watched?.channelName ??
                (watched?.channelId ? channelLabel(watched.channelId) : null)
              }
              scopeNote={progressScopeNote}
              polling={watcher.polling}
              error={watcher.error}
              onDismiss={() => setWatched(null)}
            />
          )}

          {selectedChannel === null ? (
            <Card className="flex-1">
              <CardHeader>
                <Skeleton className="h-5 w-48" />
              </CardHeader>
              <CardContent className="flex flex-col items-center gap-2 py-12 text-center">
                <ServerIcon className="size-5 text-muted-foreground" />
                <p className="text-sm font-medium">
                  {selectedServerId
                    ? 'Pick a channel to see its per-day activity'
                    : 'Pick a server, then a channel'}
                </p>
                <p className="max-w-md text-xs text-muted-foreground">
                  The activity bars show which days actually contain messages, so you can choose a
                  date range that will produce a useful digest. No ids are ever typed by hand.
                </p>
              </CardContent>
            </Card>
          ) : (
            <>
              <Card>
                <CardContent className="py-0">
                  <ActivityChart
                    activity={activityState.data}
                    loading={activityState.loading || activityState.refreshing}
                    windowDays={windowDays}
                    onWindowChange={setWindowDays}
                    fromDay={range.from}
                    toDay={range.to}
                    onPickDay={pickDay}
                    onClearRange={() => changeRange({ from: null, to: null })}
                  />
                </CardContent>
              </Card>

              <RunPanel
                channel={selectedChannel}
                activity={activityState.data}
                range={range}
                onRangeChange={changeRange}
                period={period}
                onPeriodChange={(next) => {
                  setPeriod(next)
                  setNotice(null)
                }}
                force={force}
                onForceChange={setForce}
                maxMessages={maxMessages}
                onMaxMessagesChange={setMaxMessages}
                windowDays={windowDays}
                onWidenWindow={widenWindow}
                enqueuing={enqueuing === 'capture' || enqueuing === 'digest' ? enqueuing : null}
                captureRunning={captureRunning}
                digestRunning={digestRunning}
                lastRun={lastRun}
                onDismissLastRun={() => setLastRun(null)}
                notice={notice}
                onCapture={() => void runCapture()}
                onGenerate={() => void runDigest()}
              />

              <Suspense fallback={<Skeleton className="h-40 w-full" />}>
                <AskCard channel={selectedChannel} />
              </Suspense>

              <Suspense fallback={<Skeleton className="h-64 w-full" />}>
                <DigestBrowser
                  channel={selectedChannel}
                  digests={digests}
                  loading={digestsState.loading || digestsState.refreshing}
                  error={digestsState.error}
                  onReload={digestsState.reload}
                />
              </Suspense>
            </>
          )}

          <JobsPanel
            jobs={recentJobs.jobs}
            loading={recentJobs.loading || recentJobs.refreshing}
            polling={recentJobs.polling}
            error={recentJobs.error}
            channelLabel={channelLabel}
            onReload={recentJobs.reload}
            watchingJobId={watched?.jobId ?? null}
            onWatch={(jobId) => {
              const job = recentJobs.jobs.find((candidate) => candidate.id === jobId)
              if (!job) return
              setWatched({
                jobId,
                kind: job.kind,
                channelId: job.channelId,
                channelName: job.channelId === null ? null : channelLabel(job.channelId),
                scope: null,
                period,
              })
            }}
          />

          <Separator />
          <footer className="flex flex-wrap items-center gap-2 pb-2 text-[11px] text-muted-foreground">
            <span>today is {today}</span>
            <span>·</span>
            <span>{servers.length} servers indexed</span>
            <span>·</span>
            <span>API: relative /api/* on this origin</span>
          </footer>
        </main>
      </div>

      <Toaster theme="dark" position="bottom-right" closeButton richColors />
    </div>
  )
}

/* Thin wrappers keep the hook dependencies honest (channel id, never a closure). */
function fetchChannelActivitySafe(
  channelId: string | null,
  windowDays: number,
  signal: AbortSignal,
) {
  return fetchChannelActivity(channelId ?? '', windowDays, signal)
}

function fetchChannelDigestsSafe(channelId: string | null, signal: AbortSignal) {
  return fetchChannelDigests(channelId ?? '', signal)
}
