import { CalendarIcon, DownloadIcon, Loader2Icon, RefreshCcwIcon, SparklesIcon } from 'lucide-react'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { Calendar } from '@/components/ui/calendar'
import { cn } from 'cn'
import type { ChannelActivity, ChannelSummary, Period } from '@/lib/api'
import { formatCount, formatDay, enumerateDays, parseDay, toIsoDay } from '@/lib/format'

const PERIODS: { value: Period; label: string; hint: string }[] = [
  { value: 'daily', label: 'Daily', hint: 'one digest per day' },
  { value: 'weekly', label: 'Weekly', hint: 'rolling 7-day windows' },
  { value: 'monthly', label: 'Monthly', hint: 'one digest per month' },
]

export interface RangeSelection {
  from: string | null
  to: string | null
}

interface RunPanelProps {
  channel: ChannelSummary | null
  activity: ChannelActivity | null
  range: RangeSelection
  onRangeChange: (range: RangeSelection) => void
  period: Period
  onPeriodChange: (period: Period) => void
  force: boolean
  onForceChange: (force: boolean) => void
  maxMessages: number
  onMaxMessagesChange: (max: number) => void
  busy: 'capture' | 'digest' | null
  onCapture: () => void
  onGenerate: () => void
}

/** Date-range controls plus the Capture / Generate digest actions. */
export function RunPanel({
  channel,
  activity,
  range,
  onRangeChange,
  period,
  onPeriodChange,
  force,
  onForceChange,
  maxMessages,
  onMaxMessagesChange,
  busy,
  onCapture,
  onGenerate,
}: RunPanelProps) {
  const activeDays = (activity?.days ?? []).filter((day) => day.messages > 0)
  const disabled = channel === null
  const captureBusy = busy === 'capture'
  const digestBusy = busy === 'digest'
  const anyBusy = busy !== null

  const summary = (() => {
    if (!range.from && !range.to) {
      return { label: 'Whole captured history', messages: null as number | null, days: null }
    }
    const from = range.from ?? range.to
    const to = range.to ?? range.from
    if (!from || !to) return { label: 'Incomplete range', messages: null, days: null }
    const fromDate = parseDay(from)
    const toDate = parseDay(to)
    if (!fromDate || !toDate) return { label: 'Incomplete range', messages: null, days: null }
    const [start, end] = fromDate <= toDate ? [from, to] : [to, from]
    const days = enumerateDays(parseDay(start)!, parseDay(end)!).length
    const messages = activeDays
      .filter((day) => day.date >= start && day.date <= end)
      .reduce((total, day) => total + day.messages, 0)
    return { label: `${formatDay(start)} → ${formatDay(end)}`, messages, days }
  })()

  const applyLatestActiveDays = (count: number) => {
    if (activeDays.length === 0) return
    const slice = activeDays.slice(-count)
    onRangeChange({ from: slice[0].date, to: slice[slice.length - 1].date })
  }

  const applyFullSpan = () => {
    const first = activity?.first ? toIsoDay(new Date(activity.first)) : null
    const last = activity?.last ? toIsoDay(new Date(activity.last)) : null
    onRangeChange({ from: first, to: last })
  }

  const applyToday = () => {
    const today = toIsoDay(new Date())
    onRangeChange({ from: today, to: today })
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-base">
          <CalendarIcon className="size-4 text-muted-foreground" />
          Date range &amp; run
        </CardTitle>
        <CardDescription>
          Pick days that contain messages, then capture missing history or build digests for that
          window.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-5">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-xs text-muted-foreground">Quick ranges from real activity:</span>
          <Button
            variant="outline"
            size="sm"
            disabled={disabled || activeDays.length === 0}
            onClick={() => applyLatestActiveDays(7)}
          >
            last 7 active days
          </Button>
          <Button
            variant="outline"
            size="sm"
            disabled={disabled || activeDays.length === 0}
            onClick={() => applyLatestActiveDays(30)}
          >
            last 30 active days
          </Button>
          <Button
            variant="outline"
            size="sm"
            disabled={disabled || !activity?.first}
            onClick={applyFullSpan}
          >
            whole captured span
          </Button>
          <Button variant="outline" size="sm" disabled={disabled} onClick={applyToday}>
            today
          </Button>
        </div>

        <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_18rem]">
          <div className="flex flex-col gap-3">
            <div className="flex flex-wrap items-end gap-3">
              <div className="flex flex-col gap-1.5">
                <Label htmlFor="range-from">From</Label>
                <DayPickerField
                  id="range-from"
                  value={range.from}
                  placeholder="Any start"
                  disabled={disabled}
                  align="start"
                  onChange={(day) => onRangeChange({ from: day, to: range.to })}
                />
              </div>
              <div className="flex flex-col gap-1.5">
                <Label htmlFor="range-to">To</Label>
                <DayPickerField
                  id="range-to"
                  value={range.to}
                  placeholder="Any end"
                  disabled={disabled}
                  align="start"
                  onChange={(day) => onRangeChange({ from: range.from, to: day })}
                />
              </div>
              <div className="flex flex-col gap-1.5">
                <Label htmlFor="period">Digest period</Label>
                <Select
                  value={period}
                  onValueChange={(value) => onPeriodChange(value as Period)}
                  disabled={disabled}
                >
                  <SelectTrigger id="period" className="min-w-[9rem]">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {PERIODS.map((option) => (
                      <SelectItem key={option.value} value={option.value}>
                        {option.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
            </div>

            <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
              <Badge variant="outline">{summary.label}</Badge>
              {summary.days !== null && <span>{formatCount(summary.days)} days</span>}
              {summary.messages !== null && (
                <span>· {formatCount(summary.messages)} captured messages in range</span>
              )}
              {range.from && range.to && summary.messages === 0 && (
                <span className="text-amber-500">
                  · no messages stored in this range yet, capture it first
                </span>
              )}
            </div>

            <label className="flex w-fit cursor-pointer items-center gap-2 text-xs">
              <input
                type="checkbox"
                checked={force}
                onChange={(event) => onForceChange(event.target.checked)}
                disabled={disabled}
                className="size-3.5 accent-primary"
              />
              <span className={cn(force && 'text-foreground', !force && 'text-muted-foreground')}>
                Rebuild digests that already exist (force)
              </span>
            </label>
          </div>

          <div className="flex flex-col gap-3 rounded-lg border border-border/70 bg-muted/20 p-3">
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="max-messages">Max messages per capture</Label>
              <Input
                id="max-messages"
                type="number"
                min={1}
                max={100000}
                value={maxMessages}
                disabled={disabled || captureBusy}
                onChange={(event) => {
                  const parsed = Number(event.target.value)
                  onMaxMessagesChange(
                    Number.isFinite(parsed) && parsed > 0 ? Math.floor(parsed) : 0,
                  )
                }}
              />
              <span className="text-[11px] text-muted-foreground">
                Oldest-first backfill stop condition for the selected window.
              </span>
            </div>

            <Button
              onClick={onCapture}
              disabled={disabled || anyBusy || maxMessages <= 0}
              className="w-full"
            >
              {captureBusy ? <Loader2Icon className="animate-spin" /> : <DownloadIcon />}
              {captureBusy ? 'Capturing…' : 'Capture messages'}
            </Button>
            <Button
              variant="secondary"
              onClick={onGenerate}
              disabled={disabled || anyBusy || !range.from}
              className="w-full"
            >
              {digestBusy ? <Loader2Icon className="animate-spin" /> : <SparklesIcon />}
              {digestBusy ? 'Generating…' : 'Generate digest'}
            </Button>
            <p className="flex items-start gap-1.5 text-[11px] leading-tight text-muted-foreground">
              <RefreshCcwIcon className="mt-0.5 size-3 shrink-0" />
              {period === 'daily' ? 'Daily' : period === 'weekly' ? 'Weekly' : 'Monthly'} digests
              {range.to ? ` up to ${formatDay(range.to)}` : ' from the chosen start'}.
            </p>
          </div>
        </div>
      </CardContent>
    </Card>
  )
}

interface DayPickerFieldProps {
  id: string
  value: string | null
  placeholder: string
  disabled: boolean
  align: 'start' | 'center' | 'end'
  onChange: (day: string | null) => void
}

function DayPickerField({
  id,
  value,
  placeholder,
  disabled,
  align,
  onChange,
}: DayPickerFieldProps) {
  const selected = parseDay(value)
  return (
    <Popover>
      <PopoverTrigger
        render={
          <Button
            id={id}
            variant="outline"
            disabled={disabled}
            className="w-[11.5rem] justify-start gap-2 font-normal"
          />
        }
      >
        <CalendarIcon className="size-3.5 text-muted-foreground" />
        <span className={cn('truncate', !selected && 'text-muted-foreground')}>
          {selected ? formatDay(value) : placeholder}
        </span>
      </PopoverTrigger>
      <PopoverContent className="w-auto p-0" align={align} sideOffset={6}>
        <Calendar
          mode="single"
          selected={selected ?? undefined}
          defaultMonth={selected ?? undefined}
          onSelect={(day) => onChange(day ? toIsoDay(day) : null)}
        />
        <div className="flex items-center justify-between border-t border-border px-2 py-1.5">
          <Button variant="ghost" size="sm" onClick={() => onChange(null)}>
            Clear
          </Button>
          <Button variant="ghost" size="sm" onClick={() => onChange(toIsoDay(new Date()))}>
            Today
          </Button>
        </div>
      </PopoverContent>
    </Popover>
  )
}
