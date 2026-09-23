import { useMemo } from 'react'
import { CalendarDaysIcon } from 'lucide-react'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Separator } from '@/components/ui/separator'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { Skeleton } from '@/components/ui/skeleton'
import { cn } from 'cn'
import type { ChannelActivity } from '@/lib/api'
import { enumerateDays, formatCount, formatDay, toIsoDay } from '@/lib/format'

const WINDOWS = [
  { value: '30', label: 'Last 30 days' },
  { value: '90', label: 'Last 90 days' },
  { value: '180', label: 'Last 180 days' },
  { value: '365', label: 'Last 365 days' },
  { value: '1825', label: 'Last 5 years' },
]

interface ActivityChartProps {
  activity: ChannelActivity | null
  loading: boolean
  windowDays: number
  onWindowChange: (days: number) => void
  /** Inclusive selected range as ISO days, or null endpoints. */
  fromDay: string | null
  toDay: string | null
  /** Called with the clicked day; the parent decides whether it starts or ends a range. */
  onPickDay: (day: string) => void
  onClearRange: () => void
}

/**
 * Per-day message activity for the selected channel. Picking a day starts a
 * range, picking a second day closes it, so the date range the user later
 * captures or digests is guaranteed to contain messages.
 */
export function ActivityChart({
  activity,
  loading,
  windowDays,
  onWindowChange,
  fromDay,
  toDay,
  onPickDay,
  onClearRange,
}: ActivityChartProps) {
  const counts = useMemo(() => {
    const map = new Map<string, number>()
    for (const day of activity?.days ?? []) map.set(day.date, day.messages)
    return map
  }, [activity])

  const timeline = useMemo(() => {
    const today = new Date()
    const start = new Date(today.getFullYear(), today.getMonth(), today.getDate())
    start.setDate(start.getDate() - (windowDays - 1))
    return enumerateDays(start, today)
  }, [windowDays])

  const peak = useMemo(() => {
    let max = 0
    let total = 0
    let activeDays = 0
    for (const day of timeline) {
      const count = counts.get(day) ?? 0
      if (count > max) max = count
      total += count
      if (count > 0) activeDays += 1
    }
    return { max, total, activeDays }
  }, [timeline, counts])

  const inRange = useMemo(() => {
    if (!fromDay || !toDay) return null
    let messages = 0
    let days = 0
    for (const day of timeline) {
      if (day >= fromDay && day <= toDay && (counts.get(day) ?? 0) > 0) {
        messages += counts.get(day) ?? 0
        days += 1
      }
    }
    return { messages, days }
  }, [timeline, counts, fromDay, toDay])

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <CalendarDaysIcon className="size-4 text-muted-foreground" />
          <span className="text-sm font-medium">Message activity per day</span>
          {loading && activity === null ? (
            <Skeleton className="h-5 w-24" />
          ) : (
            <span className="text-xs text-muted-foreground">
              {formatCount(peak.activeDays)} active days · {formatCount(peak.total)} messages · peak{' '}
              {formatCount(peak.max)}/day
            </span>
          )}
        </div>
        <div className="flex items-center gap-2">
          {(fromDay || toDay) && (
            <Button variant="ghost" size="sm" onClick={onClearRange}>
              Clear range
            </Button>
          )}
          <Select
            value={String(windowDays)}
            onValueChange={(value) => onWindowChange(Number(value))}
          >
            <SelectTrigger size="sm" aria-label="Activity window" className="min-w-[9.5rem]">
              <SelectValue />
            </SelectTrigger>
            <SelectContent align="end">
              {WINDOWS.map((option) => (
                <SelectItem key={option.value} value={option.value}>
                  {option.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
      </div>

      {loading && activity === null ? (
        <Skeleton className="h-24 w-full" />
      ) : timeline.length === 0 ? (
        <p className="text-xs text-muted-foreground">No activity data yet.</p>
      ) : (
        <div className="flex flex-col gap-1.5">
          <div
            className="flex h-24 items-end gap-px rounded-md border border-border/60 bg-muted/20 p-1.5"
            role="group"
            aria-label="Messages per day"
          >
            {timeline.map((day) => {
              const count = counts.get(day) ?? 0
              const heightPct = count > 0 ? Math.max(6, Math.round((count / peak.max) * 100)) : 2
              const selected = Boolean(fromDay && toDay && day >= fromDay && day <= toDay)
              const edge = day === fromDay || day === toDay
              return (
                <button
                  key={day}
                  type="button"
                  onClick={() => onPickDay(day)}
                  title={`${formatDay(day)} · ${formatCount(count)} message${count === 1 ? '' : 's'}`}
                  aria-label={`${day}: ${count} messages`}
                  aria-pressed={edge}
                  className={cn(
                    'group/bar relative flex-1 rounded-[2px] transition-colors',
                    count === 0
                      ? 'bg-muted-foreground/25 hover:bg-muted-foreground/40'
                      : selected
                        ? 'bg-primary'
                        : 'bg-primary/40 hover:bg-primary/70',
                    edge && 'ring-1 ring-primary-foreground/70',
                  )}
                  style={{ height: `${heightPct}%` }}
                />
              )
            })}
          </div>
          <div className="flex items-center justify-between text-[11px] text-muted-foreground">
            <span>{formatDay(timeline[0])}</span>
            <span className="flex items-center gap-2">
              <span className="flex items-center gap-1">
                <span className="inline-block size-2 rounded-[2px] bg-primary/40" /> has messages
              </span>
              <span className="flex items-center gap-1">
                <span className="inline-block size-2 rounded-[2px] bg-muted-foreground/25" /> no
                messages
              </span>
            </span>
            <span>{formatDay(timeline[timeline.length - 1])}</span>
          </div>
        </div>
      )}

      <Separator />
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <span className="text-muted-foreground">Captured span:</span>
        <span>
          {activity?.first ? formatDay(activity.first) : '—'} →{' '}
          {activity?.last ? formatDay(activity.last) : '—'}
        </span>
        {inRange && (
          <>
            <span className="text-muted-foreground">·</span>
            <Badge variant="secondary">
              selected range holds {formatCount(inRange.messages)} messages on {inRange.days} active
              day{inRange.days === 1 ? '' : 's'}
            </Badge>
          </>
        )}
        {fromDay && !toDay && (
          <Badge variant="outline">start {formatDay(fromDay)} · pick an end day</Badge>
        )}
        {!fromDay && activity?.days && activity.days.length > 0 && (
          <Button
            variant="link"
            size="sm"
            className="h-auto px-0"
            onClick={() => onPickDay(toIsoDay(new Date()))}
          >
            jump to today
          </Button>
        )}
      </div>
    </div>
  )
}
