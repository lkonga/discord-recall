import { useMemo, useState } from 'react'
import { ChevronRightIcon, HashIcon, RefreshCwIcon, SearchIcon } from 'lucide-react'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { ScrollArea } from '@/components/ui/scroll-area'
import { Separator } from '@/components/ui/separator'
import { Skeleton } from '@/components/ui/skeleton'
import { cn } from 'cn'
import type { ServerSummary } from '@/lib/api'
import { formatCompact } from '@/lib/format'

interface ServerGroup {
  id: string
  label: string
  hint: string
  items: ServerSummary[]
  defaultOpen: boolean
}

interface ServersSidebarProps {
  servers: ServerSummary[]
  loading: boolean
  error: Error | null
  refreshing: boolean
  selectedServerId: string | null
  onSelectServer: (serverId: string) => void
  onRefresh: () => void
}

/** First level of the picker: every Discord server, grouped by capture state. */
export function ServersSidebar({
  servers,
  loading,
  error,
  refreshing,
  selectedServerId,
  onSelectServer,
  onRefresh,
}: ServersSidebarProps) {
  const [filter, setFilter] = useState('')
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({})

  const needle = filter.trim().toLowerCase()
  const filtered = useMemo(() => {
    if (!needle) return servers
    return servers.filter(
      (server) =>
        server.name.toLowerCase().includes(needle) || server.id.toLowerCase().includes(needle),
    )
  }, [servers, needle])

  const groups = useMemo<ServerGroup[]>(() => {
    const captured: ServerSummary[] = []
    const ready: ServerSummary[] = []
    const empty: ServerSummary[] = []
    for (const server of filtered) {
      if (server.captured > 0) captured.push(server)
      else if (server.channels > 0) ready.push(server)
      else empty.push(server)
    }
    return [
      {
        id: 'captured',
        label: 'Has captured messages',
        hint: 'channels with stored history',
        items: captured,
        defaultOpen: true,
      },
      {
        id: 'ready',
        label: 'Channels known, nothing captured',
        hint: 'run Capture to backfill',
        items: ready,
        defaultOpen: true,
      },
      {
        id: 'empty',
        label: 'No channels stored',
        hint: 'run Discover first',
        items: empty,
        defaultOpen: false,
      },
    ].filter((group) => group.items.length > 0)
  }, [filtered])

  const totals = useMemo(() => {
    let channels = 0
    let captured = 0
    let digests = 0
    for (const server of servers) {
      channels += server.channels
      captured += server.captured
      digests += server.digests
    }
    return { channels, captured, digests }
  }, [servers])

  const visibleGroups = needle ? groups.map((group) => ({ ...group, defaultOpen: true })) : groups

  return (
    <aside className="flex h-full w-full flex-col gap-3 border-r border-border bg-card/40 p-3 md:w-[330px] md:shrink-0">
      <div className="flex items-center justify-between gap-2">
        <div className="flex flex-col">
          <span className="text-sm font-semibold">Servers</span>
          <span className="text-xs text-muted-foreground">
            {servers.length} servers · {formatCompact(totals.channels)} channels ·{' '}
            {formatCompact(totals.captured)} captured · {formatCompact(totals.digests)} digests
          </span>
        </div>
        <Button
          variant="ghost"
          size="icon-sm"
          onClick={onRefresh}
          disabled={refreshing}
          aria-label="Reload server list"
          title="Reload server list"
        >
          <RefreshCwIcon className={cn(refreshing && 'animate-spin')} />
        </Button>
      </div>

      <div className="relative">
        <SearchIcon className="pointer-events-none absolute top-1/2 left-2 size-3.5 -translate-y-1/2 text-muted-foreground" />
        <Input
          value={filter}
          onChange={(event) => setFilter(event.target.value)}
          placeholder="Filter servers"
          className="pl-7"
          aria-label="Filter servers"
        />
      </div>

      <Separator />

      <ScrollArea className="-mr-2 h-0 min-h-0 flex-1 pr-2">
        {loading && servers.length === 0 ? (
          <div className="flex flex-col gap-2 pr-1">
            {Array.from({ length: 8 }).map((_, index) => (
              <Skeleton key={index} className="h-9 w-full" />
            ))}
          </div>
        ) : error && servers.length === 0 ? (
          <p className="pr-1 text-xs text-destructive">{error.message}</p>
        ) : visibleGroups.length === 0 ? (
          <p className="pr-1 text-xs text-muted-foreground">
            {needle ? `No server matches “${filter.trim()}”.` : 'No servers stored yet.'}
          </p>
        ) : (
          <div className="flex flex-col gap-3">
            {visibleGroups.map((group) => {
              const isCollapsed = needle ? false : (collapsed[group.id] ?? !group.defaultOpen)
              return (
                <section key={group.id} className="flex flex-col gap-1">
                  <button
                    type="button"
                    onClick={() =>
                      setCollapsed((previous) => ({ ...previous, [group.id]: !isCollapsed }))
                    }
                    className="flex items-center gap-1.5 rounded-md px-1 py-0.5 text-left text-xs font-medium text-muted-foreground hover:text-foreground"
                    aria-expanded={!isCollapsed}
                    title={group.hint}
                  >
                    <ChevronRightIcon
                      className={cn('size-3.5 transition-transform', !isCollapsed && 'rotate-90')}
                    />
                    <span>{group.label}</span>
                    <Badge variant="secondary" className="ml-auto">
                      {group.items.length}
                    </Badge>
                  </button>
                  {!isCollapsed && (
                    <ul className="flex flex-col gap-0.5">
                      {group.items.map((server) => {
                        const selected = server.id === selectedServerId
                        return (
                          <li key={server.id}>
                            <button
                              type="button"
                              onClick={() => onSelectServer(server.id)}
                              aria-current={selected ? 'true' : undefined}
                              title={`${server.name} · ${server.id}`}
                              className={cn(
                                'flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-sm transition-colors',
                                selected ? 'bg-accent text-accent-foreground' : 'hover:bg-muted/60',
                              )}
                            >
                              <span
                                className={cn(
                                  'block size-2 shrink-0 rounded-full',
                                  server.captured > 0
                                    ? 'bg-emerald-500'
                                    : server.channels > 0
                                      ? 'bg-amber-500'
                                      : 'bg-muted-foreground/40',
                                )}
                                aria-hidden
                              />
                              <span className="min-w-0 flex-1 truncate">{server.name}</span>
                              <span className="flex shrink-0 items-center gap-1">
                                <Badge variant="outline" className="tabular-nums">
                                  {formatCompact(server.channels)}
                                </Badge>
                                {server.captured > 0 && (
                                  <Badge variant="secondary" className="tabular-nums">
                                    {formatCompact(server.captured)}
                                  </Badge>
                                )}
                                {server.digests > 0 && (
                                  <Badge className="tabular-nums">
                                    {formatCompact(server.digests)}
                                  </Badge>
                                )}
                              </span>
                            </button>
                          </li>
                        )
                      })}
                    </ul>
                  )}
                </section>
              )
            })}
          </div>
        )}
      </ScrollArea>

      <p className="flex items-center gap-1.5 text-[11px] leading-tight text-muted-foreground">
        <HashIcon className="size-3" />
        Pick a server, then a channel. Nothing here needs a pasted id.
      </p>
    </aside>
  )
}
