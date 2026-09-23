/**
 * Turns the worker's plain-text job rows into something the UI can speak.
 *
 * The API reports progress as free text (`job.phase`, `job.message`) plus a
 * message counter, so everything here is deliberately tolerant: a line that does
 * not match yields `null` and the caller falls back to the raw text instead of
 * showing a broken number.
 */

import type { JobKind, JobState, JobStatus, Period } from '@/lib/api'
import type { Scope } from '@/lib/job-scope'
import { formatCount } from '@/lib/format'

export const JOB_KIND_LABEL: Record<JobKind, string> = {
  capture: 'Capture',
  digest: 'Digest',
  discover: 'Discover',
  other: 'Job',
}

export const JOB_STATE_LABEL: Record<JobState, string> = {
  queued: 'queued',
  running: 'running',
  done: 'done',
  error: 'failed',
}

/** Unit the `messages` counter carries for each job kind. */
export function jobCounterLabel(job: JobStatus): string | null {
  if (job.messages <= 0) return null
  const unit =
    job.kind === 'capture'
      ? 'messages'
      : job.kind === 'digest'
        ? 'digests'
        : job.kind === 'discover'
          ? 'channels'
          : 'items'
  return `${formatCount(job.messages)} ${unit}`
}

/* ------------------------------------------------------------------ */
/* Parsing the worker's summary text                                   */
/* ------------------------------------------------------------------ */

export type DigestEntryOutcome = 'built' | 'reused' | 'empty' | 'failed'

export interface DigestEntry {
  outcome: DigestEntryOutcome
  /** ISO day or period start the entry belongs to, when the label carries one. */
  day: string | null
  /** Raw label from the worker (a day, a week range, or a month name). */
  label: string
  channel: string | null
  messages: number | null
  error: string | null
}

export interface DigestSummary {
  period: Period | null
  start: string | null
  end: string | null
  built: number | null
  reused: number | null
  empty: number | null
  failed: number | null
  entries: DigestEntry[]
  /** The text this was parsed from, kept for the "raw output" line. */
  raw: string
}

const DIGEST_TOTALS_RE = /(\d+)\s+built\s*,\s*(\d+)\s+reused(?:\s*,\s*(\d+)\s+empty)?(?:\s*,\s*(\d+)\s+failed)?/i
const DIGEST_HEADER_RE = /\b(daily|weekly|monthly)\s+digests?\s+(\d{4}-\d{2}-\d{2})\s*\.\.?\s*(\d{4}-\d{2}-\d{2})/i
const DISCOVER_RE = /discovered\s+(\d+)\s+servers?\s*,\s*(\d+)\s+(?:text\s+)?channels?/i
const DAY_RE = /(\d{4}-\d{2}-\d{2})/
const ENTRY_RE = /^\s*(built|reused|empty|failed)\s+#?(\S+)\s+(.+?)\s*(?:\((\d+)\s+msgs?\))?\s*$/i

const MONTHS = [
  'january',
  'february',
  'march',
  'april',
  'may',
  'june',
  'july',
  'august',
  'september',
  'october',
  'november',
  'december',
]

/** "September 2026" -> "2026-09-01", for monthly digest labels. */
function monthLabelToDay(label: string): string | null {
  const match = /^([A-Za-z]+)\s+(\d{4})$/.exec(label.trim())
  if (!match) return null
  const month = MONTHS.indexOf(match[1].toLowerCase())
  if (month < 0) return null
  return `${match[2]}-${String(month + 1).padStart(2, '0')}-01`
}

/** Any ISO day inside a label, so "2026-09-16 to 2026-09-23" maps to the start. */
function dayInLabel(label: string): string | null {
  const match = DAY_RE.exec(label)
  return match ? match[1] : monthLabelToDay(label)
}

/**
 * Parses the CLI summary the worker stores in `job.message`, e.g.
 * "daily digests 2026-09-17..2026-09-24: 2 built, 1 reused, 5 empty, 0 failed".
 * Per-period lines ("built #general 2026-09-17 (42 msgs)") are kept when present;
 * the aggregate is used otherwise.
 */
export function parseDigestSummary(message: string): DigestSummary | null {
  const raw = message.trim()
  if (!raw) return null

  const totals = DIGEST_TOTALS_RE.exec(raw)
  const header = DIGEST_HEADER_RE.exec(raw)

  const entries: DigestEntry[] = []
  for (const line of raw.split(/\r?\n/)) {
    const match = ENTRY_RE.exec(line)
    if (!match) continue
    const outcome = match[1].toLowerCase()
    if (outcome !== 'built' && outcome !== 'reused' && outcome !== 'empty' && outcome !== 'failed') {
      continue
    }
    const label = match[3].replace(/:\s*$/, '').trim()
    const separator = label.indexOf(': ')
    entries.push({
      outcome,
      day: dayInLabel(label),
      label,
      channel: match[2] ?? null,
      messages: match[4] ? Number(match[4]) : null,
      error: outcome === 'failed' && separator >= 0 ? label.slice(separator + 2) : null,
    })
  }

  if (!totals && !header && entries.length === 0) return null

  const periodWord = header?.[1]?.toLowerCase()
  const period: Period | null =
    periodWord === 'daily' || periodWord === 'weekly' || periodWord === 'monthly'
      ? periodWord
      : null

  return {
    period,
    start: header?.[2] ?? null,
    end: header?.[3] ?? null,
    built: totals ? Number(totals[1]) : null,
    reused: totals ? Number(totals[2]) : null,
    empty: totals?.[3] ? Number(totals[3]) : null,
    failed: totals?.[4] ? Number(totals[4]) : null,
    entries,
    raw,
  }
}

/** "2 built · 1 reused · 5 empty · 0 failed" */
export function digestAggregateLabel(summary: DigestSummary): string {
  const parts: string[] = []
  if (summary.built !== null) parts.push(`${formatCount(summary.built)} built`)
  if (summary.reused !== null) parts.push(`${formatCount(summary.reused)} reused`)
  if (summary.empty !== null) parts.push(`${formatCount(summary.empty)} empty`)
  if (summary.failed !== null) parts.push(`${formatCount(summary.failed)} failed`)
  return parts.length > 0 ? parts.join(' · ') : 'no summary reported'
}

/* ------------------------------------------------------------------ */
/* Completion headline (toasts) and per-period outcome rows            */
/* ------------------------------------------------------------------ */

export interface JobHeadline {
  tone: 'success' | 'error'
  title: string
  description: string | null
}

/** Toast text for a finished job. `channelLabel` is already display-ready. */
export function jobHeadline(job: JobStatus, channelLabel?: string | null): JobHeadline {
  const where = channelLabel ? `#${channelLabel}` : null
  const detail = job.message.trim()
  const description = [where, detail].filter(Boolean).join(' · ') || null

  if (job.status === 'error') {
    return {
      tone: 'error',
      title: `${JOB_KIND_LABEL[job.kind]} failed`,
      description: description ?? job.phase ?? 'the worker reported no reason',
    }
  }

  if (job.kind === 'capture') {
    return {
      tone: 'success',
      title: `captured ${formatCount(job.messages)} messages`,
      description,
    }
  }

  if (job.kind === 'digest') {
    const summary = parseDigestSummary(job.message)
    if (summary && summary.built !== null && summary.reused !== null) {
      const extras: string[] = []
      if (summary.empty) extras.push(`${formatCount(summary.empty)} empty`)
      if (summary.failed) extras.push(`${formatCount(summary.failed)} failed`)
      const suffix = extras.length > 0 ? `, ${extras.join(', ')}` : ''
      return {
        tone: summary.failed ? 'error' : 'success',
        title: `digest: ${formatCount(summary.built)} built, ${formatCount(summary.reused)} reused${suffix}`,
        description,
      }
    }
    return { tone: 'success', title: 'digest finished', description }
  }

  if (job.kind === 'discover') {
    const match = DISCOVER_RE.exec(job.message)
    return {
      tone: 'success',
      title: match
        ? `discovered ${formatCount(Number(match[1]))} servers, ${formatCount(Number(match[2]))} channels`
        : 'channel list refreshed',
      description,
    }
  }

  return { tone: 'success', title: `job ${job.id} finished`, description }
}

export type OutcomeRowKind = 'built' | 'reused' | 'empty' | 'unknown' | 'failed'

export interface OutcomeRow {
  key: string
  label: string
  kind: OutcomeRowKind
  messages: number | null
  detail: string | null
}

const OUTCOME_KINDS: Record<DigestEntryOutcome, OutcomeRowKind> = {
  built: 'built',
  reused: 'reused',
  empty: 'empty',
  failed: 'failed',
}

/**
 * Per-period result rows for the run that just finished.
 *
 * A period the worker named explicitly keeps its own outcome and message count.
 * A period it did not name is marked `empty` when the activity data shows nothing
 * captured (that is exactly what the API reports as "empty") and `unknown`
 * otherwise, because the summary only carried aggregates.
 */
export function outcomeRows(scope: Scope | null, summary: DigestSummary | null): OutcomeRow[] {
  if (!scope) {
    if (!summary) return []
    return [
      {
        key: 'aggregate',
        label: 'run summary',
        kind: summary.failed ? 'failed' : summary.built !== null ? 'built' : 'unknown',
        messages: null,
        detail: digestAggregateLabel(summary),
      },
    ]
  }

  const byDay = new Map<string, DigestEntry>()
  for (const entry of summary?.entries ?? []) {
    if (entry.day) byDay.set(entry.day, entry)
  }

  return scope.periods.map((period) => {
    const entry = byDay.get(period.start)
    if (entry) {
      return {
        key: period.key,
        label: period.label,
        kind: OUTCOME_KINDS[entry.outcome],
        messages: entry.messages ?? period.messages,
        detail: entry.error,
      }
    }
    if (period.messages === 0) {
      return {
        key: period.key,
        label: period.label,
        kind: 'empty' as OutcomeRowKind,
        messages: 0,
        detail: 'nothing captured',
      }
    }
    return {
      key: period.key,
      label: period.label,
      kind: 'unknown' as OutcomeRowKind,
      messages: period.messages,
      detail: 'reported as an aggregate only',
    }
  })
}
