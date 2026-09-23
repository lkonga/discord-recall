import { useMemo } from 'react'
import {
  CalendarIcon,
  DownloadIcon,
  Loader2Icon,
  RefreshCcwIcon,
  SparklesIcon,
} from 'lucide-react'

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
import type { ChannelActivity, ChannelSummary, JobStatus, Period } from '@/lib/api'
import {
  formatCompact,
  formatCount,
  formatDay,
  formatDayShort,
  parseDay,
  toIsoDay,
} from '@/lib/format'
import {
  digestScopeSentence,
  scopeRangeLabel,
  scopeVerdict,
  summariseScope,
  windowDaysForScope,
} from '@/lib/job-scope'
import type { Scope } from '@/lib/job-scope'
import {
  JOB_KIND_LABEL,
  JOB_STATE_LABEL,
  digestAggregateLabel,
  outcomeRows,
  parseDigestSummary,
} from '@/lib/job-text'
import type { OutcomeRow } from '@/lib/job-text'

const PERIODS: { value: Period; label: string; hint: string }[] = [
  { value: 'daily', label: 'Daily', hint: 'one digest per day' },
  { value: 'weekly', label: 'Weekly', hint: 'rolling 7-day windows' },
  { value: 'monthly', label: 'Monthly', hint: 'one digest per month' },
]

/** Rows listed after a run; longer scopes are summarised with a "+N more" line. */
const MAX_OUTCOME_ROWS = 60

export interface RangeSelection {
  from: string | null
  to: string | null
}

export interface LastRun {
  job: JobStatus
  /** Scope captured when the run was enqueued, so the rows match the request. */
  scope: Scope
  /** Channel the run belonged to; the block is skipped for other channels. */
  channelId: string
  period: Period
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
  /** Size of the loaded activity window; counts cannot see days before it. */
  windowDays: number
  onWidenWindow: (days: number) => void
  /** The enqueue POST is in flight (sub-second): the only button spinner. */
  enqueuing: 'capture' | 'digest' | null
  /** A queued/running capture for THIS channel: disable, keep browsing. */
  captureRunning: boolean
  digestRunning: boolean
  /** Last finished run for this channel, with the scope it was queued for. */
  lastRun: LastRun | null
  onDismissLastRun: () => void
  /** Inline refusal from the API, e.g. a digest range with no messages. */
  notice: string | null
  onCapture: () => void
  onGenerate: () => void
}

/**
 * Range controls plus the two ordered actions.
 *
 * The panel always states the exact scope of a run: "Generate digest" alone
 * never told anyone which days it would use, so every button is preceded by the
 * range, the number of days and how many of them hold captured messages.
 */
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
  windowDays,
  onWidenWindow,
  enqueuing,
  captureRunning,
  digestRunning,
  lastRun,
  onDismissLastRun,
  notice,
  onCapture,
  onGenerate,
}: RunPanelProps) {
  const disabled = channel === null
  const scope = useMemo(
    () => summariseScope({ activity, from: range.from, to: range.to, period, windowDays }),
    [activity, range.from, range.to, period, windowDays],
  )

  const rangeLabel = scopeRangeLabel(scope)
  const neededWindow = windowDaysForScope(scope)
  const captureScope = scope.start
    ? rangeLabel
    : `no date filter · newest ${formatCount(maxMessages)} messages first`

  const digestBlockedReason = (() => {
    if (disabled) return 'Pick a channel first.'
    if (!range.from) return 'Pick a start day in the chart above — a digest needs a start.'
    if (scope.messages === 0 && !scope.outsideWindow) {
      return `No captured messages in ${rangeLabel} yet — run step 1 first.`
    }
    if (scope.outsideWindow && scope.activeDays === 0) {
      return `The range starts before the loaded ${formatCount(windowDays)}-day window, so nothing is known about it here.`
    }
    return null
  })()

  const lastRunRows = useMemo(() => {
    if (!lastRun || lastRun.job.kind !== 'digest') return []
    return outcomeRows(lastRun.scope, parseDigestSummary(lastRun.job.message))
  }, [lastRun])

  const applyLatestActiveDays = (count: number) => {
    const activeDays = (activity?.days ?? []).filter((day) => day.messages > 0)
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

  const activeDaysPresent = (activity?.days ?? []).some((day) => day.messages > 0)

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-base">
          <CalendarIcon className="size-4 text-muted-foreground" />
          Range, then two steps
        </CardTitle>
        <CardDescription>
          Step 1 captures Discord messages into the store for the selected range; step 2 builds
          digests from exactly the same range. Nothing is captured or built outside it.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-5">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-xs text-muted-foreground">Quick ranges from real activity:</span>
          <Button
            variant="outline"
            size="sm"
            disabled={disabled || !activeDaysPresent}
            onClick={() => applyLatestActiveDays(7)}
          >
            last 7 active days
          </Button>
          <Button
            variant="outline"
            size="sm"
            disabled={disabled || !activeDaysPresent}
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

        <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_19rem]">
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
              <Badge variant="outline">selected range: {rangeLabel}</Badge>
              {scope.dayCount > 0 && (
                <span>
                  {formatCount(scope.dayCount)} day{scope.dayCount === 1 ? '' : 's'}
                </span>
              )}
              {scope.dayCount > 0 && (
                <span>
                  · {formatCount(scope.messages)} captured message
                  {scope.messages === 1 ? '' : 's'} in range
                </span>
              )}
            </div>

            {scope.days.length > 0 && (
              <div className="flex flex-col gap-1.5">
                <span className="text-[11px] text-muted-foreground">
                  Days in range, from captured activity (click a chip to select that day):
                </span>
                <div className="flex flex-wrap gap-1">
                  {scope.days.map((day) => {
                    const selected = range.from === day.date || range.to === day.date
                    return (
                      <button
                        key={day.date}
                        type="button"
                        onClick={() => onRangeChange({ from: day.date, to: day.date })}
                        aria-pressed={selected}
                        title={`${formatDay(day.date)} · ${formatCount(day.messages)} captured message${day.messages === 1 ? '' : 's'}`}
                        className={cn(
                          'flex min-w-[3.4rem] flex-col items-center rounded-md border px-1.5 py-1 text-[11px] leading-tight tabular-nums transition-colors',
                          day.messages > 0
                            ? 'border-primary/40 bg-primary/10 text-foreground hover:bg-primary/20'
                            : 'border-dashed border-border bg-transparent text-muted-foreground hover:bg-muted/50',
                          selected && 'ring-2 ring-ring/40',
                        )}
                      >
                        <span>{formatDayShort(day.date)}</span>
                        <span>{day.messages > 0 ? formatCompact(day.messages) : '0'}</span>
                      </button>
                    )
                  })}
                </div>
                {scope.truncated && (
                  <span className="text-[11px] text-muted-foreground">
                    showing the last {formatCount(scope.days.length)} of{' '}
                    {formatCount(scope.dayCount)} days in this range
                  </span>
                )}
              </div>
            )}

            <p className="text-xs font-medium">{scopeVerdict(scope)}</p>
            <p className="text-[11px] leading-tight text-muted-foreground">
              A digest is only built for a period that contains at least one captured message; days
              that show 0 are skipped and produce nothing. Counts come from the local store, so
              capture a range before digesting it.
            </p>

            {scope.outsideWindow && (
              <div className="flex flex-wrap items-center gap-2 rounded-md border border-amber-500/40 bg-amber-500/10 p-2 text-[11px]">
                <span>
                  Counts only cover the last {formatCount(windowDays)} days of activity data, so
                  days before {formatDay(scope.windowStart)} are not loaded.
                </span>
                {neededWindow !== null && (
                  <Button variant="outline" size="xs" onClick={() => onWidenWindow(neededWindow)}>
                    widen activity window to {formatCount(neededWindow)} days
                  </Button>
                )}
              </div>
            )}

            {!disabled && range.from && scope.messages === 0 && !scope.outsideWindow && (
              <div className="flex flex-wrap items-center gap-2 rounded-md border border-amber-500/40 bg-amber-500/10 p-2 text-[11px]">
                <span>
                  Nothing captured in {rangeLabel} yet, so a digest here would be empty.
                </span>
                <Button
                  variant="outline"
                  size="xs"
                  onClick={onCapture}
                  disabled={captureRunning || maxMessages <= 0}
                >
                  capture this range first
                </Button>
              </div>
            )}
          </div>

          <div className="flex flex-col gap-3">
            <div className="flex flex-col gap-2 rounded-lg border border-border/70 bg-muted/20 p-3">
              <span className="text-xs font-semibold">1. Capture messages (for the range)</span>
              <span className="text-[11px] leading-tight text-muted-foreground">
                {captureScope}
              </span>
              <div className="flex flex-col gap-1.5">
                <Label htmlFor="max-messages">Max messages per capture</Label>
                <Input
                  id="max-messages"
                  type="number"
                  min={1}
                  max={100000}
                  value={maxMessages}
                  disabled={disabled || captureRunning}
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
                disabled={disabled || captureRunning || maxMessages <= 0}
                className="w-full"
              >
                {enqueuing === 'capture' ? <Loader2Icon className="animate-spin" /> : <DownloadIcon />}
                Capture messages
              </Button>
              {captureRunning && (
                <p className="text-[11px] text-muted-foreground">
                  A capture is already queued or running for this channel — progress is shown above.
                </p>
              )}
            </div>

            <div className="flex flex-col gap-2 rounded-lg border border-border/70 bg-muted/20 p-3">
              <span className="text-xs font-semibold">2. Build digests (for the same range)</span>
              <span className="text-[11px] leading-tight text-foreground">
                {digestScopeSentence(scope, period)}
              </span>
              {period !== 'daily' && (
                <span className="text-[11px] leading-tight text-muted-foreground">
                  {period === 'weekly' ? 'Weekly' : 'Monthly'} digests cover their whole{' '}
                  {period === 'weekly' ? 'week (Monday to Sunday)' : 'calendar month'}, including
                  days outside the selected range.
                </span>
              )}
              <Button
                variant="secondary"
                onClick={onGenerate}
                disabled={disabled || digestRunning || !range.from || digestBlockedReason !== null}
                className="w-full"
              >
                {enqueuing === 'digest' ? <Loader2Icon className="animate-spin" /> : <SparklesIcon />}
                Generate digest
              </Button>
              {digestRunning && (
                <p className="text-[11px] text-muted-foreground">
                  A digest run is already queued or running for this channel — progress is shown
                  above.
                </p>
              )}
              {!digestRunning && digestBlockedReason && (
                <p className="text-[11px] text-amber-500">{digestBlockedReason}</p>
              )}
              <label className="flex w-fit cursor-pointer items-center gap-2 text-[11px]">
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
              <p className="flex items-start gap-1.5 text-[11px] leading-tight text-muted-foreground">
                <RefreshCcwIcon className="mt-0.5 size-3 shrink-0" />
                {period === 'daily' ? 'Daily' : period === 'weekly' ? 'Weekly' : 'Monthly'} digests
                {range.to ? ` up to ${formatDay(range.to)}` : ' from the chosen start'}.
              </p>
            </div>
          </div>
        </div>

        {notice && <p className="text-xs text-destructive">{notice}</p>}

        {lastRun && channel && lastRun.channelId === channel.id && (
          <LastRunBlock lastRun={lastRun} rows={lastRunRows} onDismiss={onDismissLastRun} />
        )}
      </CardContent>
    </Card>
  )
}

const OUTCOME_STYLE: Record<OutcomeRow['kind'], { dot: string; label: string }> = {
  built: { dot: 'bg-emerald-500', label: 'built' },
  reused: { dot: 'bg-sky-500', label: 'reused (already existed)' },
  empty: { dot: 'bg-muted-foreground/40', label: 'nothing to build' },
  unknown: { dot: 'bg-amber-500', label: 'built or reused' },
  failed: { dot: 'bg-destructive', label: 'failed' },
}

interface LastRunBlockProps {
  lastRun: LastRun
  rows: OutcomeRow[]
  onDismiss: () => void
}

/** What the last finished run did, per period/day instead of one lumped toast. */
function LastRunBlock({ lastRun, rows, onDismiss }: LastRunBlockProps) {
  const { job, scope } = lastRun
  const summary = job.kind === 'digest' ? parseDigestSummary(job.message) : null
  const visible = rows.slice(0, MAX_OUTCOME_ROWS)

  return (
    <div className="flex flex-col gap-2 rounded-lg border border-border/70 bg-muted/20 p-3">
      <div className="flex flex-wrap items-center gap-2">
        <Badge variant="secondary">
          {JOB_KIND_LABEL[job.kind]} #{job.id}
        </Badge>
        <Badge variant={job.status === 'error' ? 'destructive' : 'outline'}>
          {JOB_STATE_LABEL[job.status]}
        </Badge>
        <span className="text-xs text-muted-foreground">
          {scopeRangeLabel(scope)} · {lastRun.period} · last run
        </span>
        <Button variant="ghost" size="xs" className="ml-auto" onClick={onDismiss}>
          dismiss
        </Button>
      </div>

      {job.status === 'error' ? (
        <p className="text-xs text-destructive">
          {job.message || 'the worker reported no reason'}
        </p>
      ) : job.kind === 'capture' ? (
        <p className="text-xs">
          captured {formatCount(job.messages)} message
          {job.messages === 1 ? '' : 's'} for {scopeRangeLabel(scope)}
        </p>
      ) : (
        <>
          {summary && summary.built !== null && (
            <p className="text-xs font-medium">{digestAggregateLabel(summary)}</p>
          )}
          {visible.length > 0 && (
            <ul className="flex flex-col gap-1">
              {visible.map((row) => (
                <li key={row.key} className="flex flex-wrap items-center gap-2 text-[11px]">
                  <span
                    className={cn('size-2 shrink-0 rounded-full', OUTCOME_STYLE[row.kind].dot)}
                    aria-hidden
                  />
                  <span className="min-w-[7rem]">{row.label}</span>
                  <span className="text-muted-foreground">{OUTCOME_STYLE[row.kind].label}</span>
                  {row.messages !== null && (
                    <span className="tabular-nums text-muted-foreground">
                      {formatCount(row.messages)} msg{row.messages === 1 ? '' : 's'}
                    </span>
                  )}
                  {row.detail && <span className="text-muted-foreground">{row.detail}</span>}
                </li>
              ))}
            </ul>
          )}
          {rows.length > visible.length && (
            <p className="text-[11px] text-muted-foreground">
              +{formatCount(rows.length - visible.length)} more periods in scope
            </p>
          )}
          {job.message && (
            <p className="text-[11px] text-muted-foreground" title={job.message}>
              worker summary: {job.message}
            </p>
          )}
        </>
      )}
    </div>
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
