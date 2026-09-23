/**
 * Range maths for the "capture, then digest" flow.
 *
 * The panel has to state exactly what a run will cover, because "Generate
 * digest" alone never told anyone which days it used. The period alignment here
 * mirrors the API's own rules (digest/range.py): a daily digest covers one day,
 * a weekly digest starts on Monday, a monthly digest starts on the 1st.
 */

import type { ChannelActivity, Period } from '@/lib/api'
import { addDays, enumerateDays, formatCount, formatDay, parseDay, toIsoDay } from '@/lib/format'

/** Ranges longer than this roll up per period instead of listing a chip per day. */
export const MAX_SCOPE_CHIPS = 62

/** Selectable activity windows, in days. Shared by the chart and the run panel. */
export const ACTIVITY_WINDOWS: { value: number; label: string }[] = [
  { value: 30, label: 'Last 30 days' },
  { value: 90, label: 'Last 90 days' },
  { value: 180, label: 'Last 180 days' },
  { value: 365, label: 'Last 365 days' },
  { value: 1825, label: 'Last 5 years' },
]

/** Smallest window preset that covers `days`, for widening the window on demand. */
export function windowPresetFor(days: number): number {
  return (
    ACTIVITY_WINDOWS.find((option) => option.value >= days)?.value ??
    ACTIVITY_WINDOWS[ACTIVITY_WINDOWS.length - 1].value
  )
}

/** Safety cap so a nonsense range cannot allocate an unbounded day list. */
const MAX_SCOPE_DAYS = 4000

export interface ScopeDay {
  /** ISO day, YYYY-MM-DD. */
  date: string
  /** Captured messages on that day (0 when nothing is stored). */
  messages: number
}

export interface ScopePeriod {
  key: string
  /** First day the digest period covers. */
  start: string
  /** Last day the digest period covers (it may pass the end of the selection). */
  end: string
  label: string
  /** Days of this period that fall inside the selected range. */
  days: ScopeDay[]
  messages: number
  activeDays: number
}

export interface Scope {
  from: string | null
  to: string | null
  /** Ordered endpoints: null until at least one end of the range is chosen. */
  start: string | null
  end: string | null
  /** True only when both ends are set. */
  bothEnds: boolean
  /** Day chips, capped at MAX_SCOPE_CHIPS (the most recent days of the range). */
  days: ScopeDay[]
  dayCount: number
  truncated: boolean
  messages: number
  activeDays: number
  emptyDays: number
  periods: ScopePeriod[]
  /** Periods that hold at least one message: the only ones a digest run builds. */
  activePeriods: number
  /** The range starts before the loaded activity window, so counts cannot see it. */
  outsideWindow: boolean
  windowStart: string
}

/** Accepts "YYYY-MM-DD" or an ISO timestamp and returns a plain ISO day. */
export function normaliseDay(value: string | null | undefined): string | null {
  const date = parseDay(value)
  return date ? toIsoDay(date) : null
}

function startOfDay(date: Date): Date {
  return new Date(date.getFullYear(), date.getMonth(), date.getDate())
}

/** First day of the digest period that contains `date` (mirrors range.align). */
export function periodStartOf(date: Date, period: Period): Date {
  if (period === 'weekly') {
    const day = startOfDay(date)
    // getDay(): 0 = Sunday. Monday is the API's week start.
    return addDays(day, -((day.getDay() + 6) % 7))
  }
  if (period === 'monthly') return new Date(date.getFullYear(), date.getMonth(), 1)
  return startOfDay(date)
}

function addPeriod(date: Date, period: Period): Date {
  if (period === 'weekly') return addDays(date, 7)
  if (period === 'monthly') return new Date(date.getFullYear(), date.getMonth() + 1, 1)
  return addDays(date, 1)
}

function periodLabel(start: Date, end: Date, period: Period): string {
  if (period === 'monthly') {
    return start.toLocaleDateString(undefined, { month: 'long', year: 'numeric' })
  }
  if (period === 'weekly') {
    return `${formatDay(toIsoDay(start))} → ${formatDay(toIsoDay(end))}`
  }
  return formatDay(toIsoDay(start))
}

/** Period starts covering `start`..`end` inclusive, each with its in-range days. */
export function buildPeriods(
  start: string,
  end: string,
  period: Period,
  counts: Map<string, number>,
): ScopePeriod[] {
  const periods: ScopePeriod[] = []
  const lastDay = parseDay(end)
  if (!lastDay) return periods
  let cursor = periodStartOf(parseDay(start) ?? lastDay, period)
  let guard = 0
  while (cursor <= lastDay && guard < MAX_SCOPE_DAYS) {
    guard += 1
    const nextStart = addPeriod(cursor, period)
    const periodEnd = addDays(nextStart, -1)
    const days: ScopeDay[] = []
    let messages = 0
    let activeDays = 0
    // Only the part of the period inside the selection contributes to the count.
    const walk = enumerateDays(cursor, periodEnd, MAX_SCOPE_DAYS)
    for (const day of walk) {
      if (day < start || day > end) continue
      const count = counts.get(day) ?? 0
      days.push({ date: day, messages: count })
      messages += count
      if (count > 0) activeDays += 1
    }
    periods.push({
      key: `${period}:${toIsoDay(cursor)}`,
      start: toIsoDay(cursor),
      end: toIsoDay(periodEnd),
      label: periodLabel(cursor, periodEnd, period),
      days,
      messages,
      activeDays,
    })
    cursor = nextStart
  }
  return periods
}

export interface SummariseScopeOptions {
  activity: ChannelActivity | null
  from: string | null
  to: string | null
  period: Period
  /** Days the activity endpoint was asked for: counts beyond it are unknown. */
  windowDays: number
  today?: Date
}

/** Everything the run panel needs to describe one selection. */
export function summariseScope(options: SummariseScopeOptions): Scope {
  const today = options.today ?? new Date()
  const fromDay = normaliseDay(options.from)
  const toDay = normaliseDay(options.to)

  let start: string | null = null
  let end: string | null = null
  let bothEnds = false
  if (fromDay && toDay) {
    bothEnds = true
    start = fromDay <= toDay ? fromDay : toDay
    end = fromDay <= toDay ? toDay : fromDay
  } else if (fromDay) {
    start = fromDay
    end = fromDay
  } else if (toDay) {
    start = toDay
    end = toDay
  }

  const counts = new Map<string, number>()
  for (const day of options.activity?.days ?? []) counts.set(day.date, day.messages)

  const allDays: ScopeDay[] = []
  if (start && end) {
    const startDate = parseDay(start)
    const endDate = parseDay(end)
    if (startDate && endDate) {
      for (const date of enumerateDays(startDate, endDate, MAX_SCOPE_DAYS)) {
        allDays.push({ date, messages: counts.get(date) ?? 0 })
      }
    }
  }

  const messages = allDays.reduce((total, day) => total + day.messages, 0)
  const activeDays = allDays.filter((day) => day.messages > 0).length
  const periods = start && end ? buildPeriods(start, end, options.period, counts) : []
  const windowStart = toIsoDay(addDays(startOfDay(today), -(Math.max(1, options.windowDays) - 1)))

  return {
    from: fromDay,
    to: toDay,
    start,
    end,
    bothEnds,
    // For a very long range the newest days are the ones worth showing, so the
    // chips keep the tail of the range and the panel says how many were hidden.
    days: allDays.slice(-MAX_SCOPE_CHIPS),
    dayCount: allDays.length,
    truncated: allDays.length > MAX_SCOPE_CHIPS,
    messages,
    activeDays,
    emptyDays: allDays.length - activeDays,
    periods,
    activePeriods: periods.filter((period) => period.messages > 0).length,
    outsideWindow: start !== null && start < windowStart,
    windowStart,
  }
}

/** "Sep 17 → Sep 24", or a note when the range is open. */
export function scopeRangeLabel(scope: Scope): string {
  if (!scope.start || !scope.end) return 'no date filter'
  if (scope.start === scope.end) return formatDay(scope.start)
  return `${formatDay(scope.start)} → ${formatDay(scope.end)}`
}

function plural(count: number, singular: string, pluralWord = `${singular}s`): string {
  return count === 1 ? singular : pluralWord
}

/**
 * The sentence shown next to the digest button: which period, over which days,
 * and how many of them actually hold messages.
 */
export function digestScopeSentence(scope: Scope, period: Period): string {
  const range = scopeRangeLabel(scope)
  if (!scope.start || !scope.end) {
    return `Pick a start day: ${period} digests are built for the range you select.`
  }
  const days = `${formatCount(scope.dayCount)} ${plural(scope.dayCount, 'day')}`
  if (period === 'daily') {
    return `Build daily digests for ${range} · ${days} · ${formatCount(scope.activeDays)} contain messages`
  }
  const windows = scope.periods.length
  const noun = period === 'weekly' ? 'week' : 'month'
  return (
    `Build ${period} digests for ${range} · ${formatCount(windows)} ${plural(windows, noun)} overlap` +
    ` · ${formatCount(scope.activePeriods)} contain messages`
  )
}

/** One line stating how much of the selected range is worth digesting. */
export function scopeVerdict(scope: Scope): string {
  if (scope.dayCount === 0) return 'Pick a start day to see what this range covers.'
  if (scope.activeDays === 0) {
    return `None of the ${formatCount(scope.dayCount)} ${plural(scope.dayCount, 'day')} in this range has captured messages yet.`
  }
  if (scope.emptyDays === 0) {
    return `All ${formatCount(scope.dayCount)} ${plural(scope.dayCount, 'day')} in this range have captured messages.`
  }
  return (
    `${formatCount(scope.activeDays)} of ${formatCount(scope.dayCount)} days have messages; ` +
    `the other ${formatCount(scope.emptyDays)} will be skipped (nothing captured).`
  )
}

/** Window size that would cover the range's start, or null when it already does. */
export function windowDaysForScope(scope: Scope, today = new Date()): number | null {
  if (!scope.start || !scope.outsideWindow) return null
  const startDate = parseDay(scope.start)
  if (!startDate) return null
  const days = Math.round((startOfDay(today).getTime() - startDate.getTime()) / 86_400_000) + 1
  return Math.max(1, days)
}
