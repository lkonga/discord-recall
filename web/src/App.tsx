import { lazy, Suspense, useCallback, useEffect, useMemo, useState } from 'react'
import { Loader2Icon, RefreshCwIcon, ServerIcon } from 'lucide-react'
import { toast, Toaster } from 'sonner'

import { ActivityChart } from '@/components/activity-chart'
import { ChannelCombobox } from '@/components/channel-combobox'
import { RunPanel, type RangeSelection } from '@/components/run-panel'
import { ServersSidebar } from '@/components/servers-sidebar'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader } from '@/components/ui/card'
import { Separator } from '@/components/ui/separator'
import { Skeleton } from '@/components/ui/skeleton'
import { useAsync } from '@/hooks/use-async'
import { useDebouncedValue } from '@/hooks/use-debounced-value'
import {
  captureMessages,
  discoverChannels,
  fetchChannelActivity,
  fetchChannelDigests,
  generateDigest,
  listChannels,
  listServers,
  type ChannelSummary,
  type Period,
} from '@/lib/api'
import { formatCompact, formatDateTime, relativeFromNow, toIsoDay } from '@/lib/format'

// The markdown renderer is only needed once a channel is open, so it is split
// out of the initial bundle (react-markdown + remark-gfm are both chunky).
const AskCard = lazy(() =>
  import('@/components/ask-card').then((module) => ({ default: module.AskCard })),
)
const DigestBrowser = lazy(() =>
  import('@/components/digest-browser').then((module) => ({ default: module.DigestBrowser })),
)

const DEFAULT_ACTIVITY_WINDOW = 90

export default function App() {
  const [selectedServerId, setSelectedServerId] = useState<string | null>(null)
  const [selectedChannelId, setSelectedChannelId] = useState<string | null>(null)
  const [channelQuery, setChannelQuery] = useState('')
  const [windowDays, setWindowDays] = useState(DEFAULT_ACTIVITY_WINDOW)
  const [range, setRange] = useState<RangeSelection>({ from: null, to: null })
  const [period, setPeriod] = useState<Period>('daily')
  const [force, setForce] = useState(false)
  const [maxMessages, setMaxMessages] = useState(600)
  const [busy, setBusy] = useState<'capture' | 'digest' | null>(null)
  const [discovering, setDiscovering] = useState(false)
  const [digestNonce, setDigestNonce] = useState(0)

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
    (signal) => fetchChannelActivity(selectedChannelId ?? '', windowDays, signal),
    [selectedChannelId, windowDays],
    { enabled: selectedChannelId !== null },
  )

  const digestsState = useAsync(
    (signal) => fetchChannelDigests(selectedChannelId ?? '', signal),
    [selectedChannelId, digestNonce],
    { enabled: selectedChannelId !== null },
  )
  const digests = digestsState.data ?? []

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

  /** Selecting a channel resets the range so the user starts from real activity. */
  const selectChannel = useCallback((channel: ChannelSummary | null) => {
    setSelectedChannelId(channel?.id ?? null)
    setRange({ from: null, to: null })
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

  const runDiscover = async () => {
    setDiscovering(true)
    const toastId = toast.loading('Asking Discord for every server and channel…')
    try {
      const result = await discoverChannels()
      serversState.reload()
      if (selectedServerId) channelsState.reload()
      if (result.ok) toast.success(result.status, { id: toastId })
      else toast.error(result.status, { id: toastId })
    } catch (error) {
      toast.error(error instanceof Error ? error.message : 'Discover failed', { id: toastId })
    } finally {
      setDiscovering(false)
    }
  }

  const runCapture = async () => {
    if (!selectedChannel) return
    setBusy('capture')
    const toastId = toast.loading(`Capturing #${selectedChannel.name}…`)
    try {
      const result = await captureMessages({
        channelId: selectedChannel.id,
        since: range.from ?? undefined,
        until: range.to ?? undefined,
        maxMessages,
      })
      toast[result.ok ? 'success' : 'error'](result.status, { id: toastId })
      if (result.ok) {
        channelsState.reload()
        activityState.reload()
        serversState.reload()
      }
    } catch (error) {
      toast.error(error instanceof Error ? error.message : 'Capture failed', { id: toastId })
    } finally {
      setBusy(null)
    }
  }

  const runDigest = async () => {
    if (!selectedChannel || !range.from) return
    setBusy('digest')
    const toastId = toast.loading(`Building ${period} digest for #${selectedChannel.name}…`)
    try {
      const result = await generateDigest({
        channelId: selectedChannel.id,
        from: range.from,
        to: range.to ?? undefined,
        period,
        force,
      })
      toast[result.ok ? 'success' : 'error'](result.status, { id: toastId })
      if (result.ok) {
        setDigestNonce((value) => value + 1)
        channelsState.reload()
        serversState.reload()
      }
    } catch (error) {
      toast.error(error instanceof Error ? error.message : 'Digest failed', { id: toastId })
    } finally {
      setBusy(null)
    }
  }

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
          {serversState.refreshing && (
            <span className="flex items-center gap-1 text-[11px] text-muted-foreground">
              <Loader2Icon className="size-3 animate-spin" /> reloading
            </span>
          )}
          <Button
            variant="outline"
            size="sm"
            onClick={() => void runDiscover()}
            disabled={discovering}
          >
            {discovering ? (
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
                    onClearRange={() => setRange({ from: null, to: null })}
                  />
                </CardContent>
              </Card>

              <RunPanel
                channel={selectedChannel}
                activity={activityState.data}
                range={range}
                onRangeChange={setRange}
                period={period}
                onPeriodChange={setPeriod}
                force={force}
                onForceChange={setForce}
                maxMessages={maxMessages}
                onMaxMessagesChange={setMaxMessages}
                busy={busy}
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
