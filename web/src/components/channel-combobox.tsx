import { useEffect, useState } from 'react'
import { ChevronsUpDownIcon, HashIcon, SearchIcon } from 'lucide-react'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from '@/components/ui/command'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { Skeleton } from '@/components/ui/skeleton'
import { cn } from 'cn'
import type { ChannelSummary } from '@/lib/api'
import { formatCompact, relativeFromNow } from '@/lib/format'

interface ChannelComboboxProps {
  channels: ChannelSummary[]
  selectedChannelId: string | null
  serverName: string | null
  loading: boolean
  query: string
  onQueryChange: (query: string) => void
  onSelect: (channel: ChannelSummary | null) => void
  /** Open the list once after a server is picked, so the next step is obvious. */
  autoOpen?: boolean
  onAutoOpen?: () => void
}

/**
 * Second level of the picker: a searchable combobox of the selected server's
 * channels. The search text is sent to GET /api/channels?q= so channels that
 * were never loaded locally are still findable.
 */
export function ChannelCombobox({
  channels,
  selectedChannelId,
  serverName,
  loading,
  query,
  onQueryChange,
  onSelect,
  autoOpen = false,
  onAutoOpen,
}: ChannelComboboxProps) {
  const [open, setOpen] = useState(false)

  // Choosing a server should not leave the user staring at a closed dropdown.
  useEffect(() => {
    if (!autoOpen || !serverName || open) return
    setOpen(true)
    onAutoOpen?.()
  }, [autoOpen, serverName, open, onAutoOpen])
  const selected = channels.find((channel) => channel.id === selectedChannelId) ?? null
  const active = channels.filter((channel) => channel.messages > 0)
  const inactive = channels.filter((channel) => channel.messages === 0)

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger
        render={
          <Button
            variant="outline"
            role="combobox"
            aria-expanded={open}
            aria-label="Choose a channel"
            disabled={!serverName}
            className="h-10 w-full max-w-xl justify-between gap-2 text-left"
          />
        }
      >
        {selected ? (
          <span className="flex min-w-0 items-center gap-2">
            <HashIcon className="size-3.5 shrink-0 text-muted-foreground" />
            <span className="truncate font-medium">{selected.name}</span>
            <span className="shrink-0 text-xs text-muted-foreground">
              {formatCompact(selected.messages)} messages · {formatCompact(selected.digests)}{' '}
              digests
            </span>
          </span>
        ) : (
          <span className="flex min-w-0 items-center gap-2 text-muted-foreground">
            <SearchIcon className="size-3.5 shrink-0" />
            <span className="truncate">
              {serverName
                ? `Choose a channel in ${serverName} (${channels.length})`
                : 'Select a server first'}
            </span>
          </span>
        )}
        <ChevronsUpDownIcon className="size-4 shrink-0 opacity-60" />
      </PopoverTrigger>
      <PopoverContent className="w-[min(38rem,calc(100vw-2rem))] p-0" align="start" sideOffset={6}>
        <Command shouldFilter={false} className="rounded-lg bg-popover">
          <CommandInput
            value={query}
            onValueChange={onQueryChange}
            placeholder={serverName ? `Search channels in ${serverName}…` : 'Search channels…'}
            autoFocus
          />
          <CommandList className="max-h-80">
            {loading && channels.length === 0 && (
              <div className="flex flex-col gap-1.5 p-2">
                {Array.from({ length: 5 }).map((_, index) => (
                  <Skeleton key={index} className="h-8 w-full" />
                ))}
              </div>
            )}
            {!loading && channels.length === 0 && (
              <CommandEmpty>
                {query.trim()
                  ? `No channel matches “${query.trim()}” in this server.`
                  : 'No channels stored for this server yet.'}
              </CommandEmpty>
            )}
            {active.length > 0 && (
              <CommandGroup heading={`Has captured messages (${active.length})`}>
                {active.map((channel) => (
                  <ChannelRow
                    key={channel.id}
                    channel={channel}
                    selected={channel.id === selectedChannelId}
                    onSelect={() => {
                      onSelect(channel)
                      setOpen(false)
                    }}
                  />
                ))}
              </CommandGroup>
            )}
            {inactive.length > 0 && (
              <CommandGroup heading={`No captured messages yet (${inactive.length})`}>
                {inactive.map((channel) => (
                  <ChannelRow
                    key={channel.id}
                    channel={channel}
                    selected={channel.id === selectedChannelId}
                    onSelect={() => {
                      onSelect(channel)
                      setOpen(false)
                    }}
                  />
                ))}
              </CommandGroup>
            )}
          </CommandList>
          <div className="flex items-center justify-between border-t border-border px-2.5 py-1.5 text-[11px] text-muted-foreground">
            <span>
              {channels.length} channel{channels.length === 1 ? '' : 's'}
              {loading ? ' · refreshing…' : ''}
            </span>
            <span>Click a channel to load its activity</span>
          </div>
        </Command>
      </PopoverContent>
    </Popover>
  )
}

interface ChannelRowProps {
  channel: ChannelSummary
  selected: boolean
  onSelect: () => void
}

function ChannelRow({ channel, selected, onSelect }: ChannelRowProps) {
  return (
    <CommandItem
      value={channel.id}
      onSelect={onSelect}
      className={cn('gap-2', selected && 'bg-muted')}
    >
      <HashIcon className="size-3.5 text-muted-foreground" />
      <span className="truncate font-medium">{channel.name}</span>
      <span className="ml-auto flex shrink-0 items-center gap-1 text-[11px] text-muted-foreground">
        {channel.messages > 0 ? (
          <Badge variant="secondary" className="tabular-nums">
            {formatCompact(channel.messages)} msgs
          </Badge>
        ) : (
          <Badge variant="outline">empty</Badge>
        )}
        {channel.digests > 0 && (
          <Badge className="tabular-nums">{formatCompact(channel.digests)} digests</Badge>
        )}
        <span className="hidden sm:inline">{relativeFromNow(channel.lastMessage)}</span>
      </span>
    </CommandItem>
  )
}
