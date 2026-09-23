/**
 * Typed client for the Discord Recall JSON API.
 *
 * Every endpoint is served from the same origin as this bundle, so all paths
 * are relative ("/api/..."). Everything that is a Discord id stays a `string`:
 * snowflakes are larger than Number.MAX_SAFE_INTEGER, so they are never
 * converted to JavaScript numbers anywhere in this module.
 */

/** Base URL for the API. Empty means "same origin" (the production case). */
const API_BASE: string = import.meta.env.VITE_API_BASE ?? ''

export type Period = 'daily' | 'weekly' | 'monthly'

export interface ServerSummary {
  id: string
  name: string
  channels: number
  captured: number
  digests: number
}

export interface ChannelSummary {
  id: string
  name: string
  serverId: string
  serverName: string
  messages: number
  lastMessage: string | null
  digests: number
  latestDigest: string | null
}

export interface ActivityDay {
  /** Calendar day in YYYY-MM-DD form. */
  date: string
  messages: number
}

export interface ChannelActivity {
  days: ActivityDay[]
  /** First/oldest captured message timestamp (ISO 8601) or null when empty. */
  first: string | null
  /** Last/newest captured message timestamp (ISO 8601) or null when empty. */
  last: string | null
}

export interface Digest {
  id: number
  period: string
  /** Period start (ISO 8601). */
  start: string
  /** Period end (ISO 8601). Returned by the API but not part of the documented shape. */
  end: string | null
  messages: number
  /** Markdown body. */
  content: string
}

export interface ActionStatus {
  ok: boolean
  status: string
  /** Set when the action was queued as a background job. */
  jobId: number | null
}

export interface ServerListResponse {
  servers: ServerSummary[]
}

export interface ChannelListResponse {
  channels: ChannelSummary[]
}

export interface DigestListResponse {
  digests: Digest[]
}

export interface CaptureRequest {
  channelId: string
  since?: string
  until?: string
  maxMessages: number
}

export interface DigestRequest {
  channelId: string
  /** Sent to the API as the `from` key. */
  from: string
  to?: string
  period: Period
  force?: boolean
}

export interface AskResponse {
  answer: string
}

/** Error carrying the HTTP status and any `detail` the API returned. */
export class ApiError extends Error {
  readonly status: number
  readonly detail: string | null
  readonly path: string

  constructor(message: string, options: { status: number; detail?: string | null; path: string }) {
    super(message)
    this.name = 'ApiError'
    this.status = options.status
    this.detail = options.detail ?? null
    this.path = options.path
  }
}

/* ------------------------------------------------------------------ */
/* Defensive parsing                                                   */
/* ------------------------------------------------------------------ */

function asRecord(value: unknown): Record<string, unknown> {
  return typeof value === 'object' && value !== null ? (value as Record<string, unknown>) : {}
}

/** Ids arrive as strings; keep them strings even if a proxy sends a number. */
function asId(value: unknown): string {
  if (typeof value === 'string') return value
  if (typeof value === 'number' && Number.isFinite(value))
    return BigInt(Math.trunc(value)).toString()
  if (value === null || value === undefined) return ''
  return String(value)
}

function asNumber(value: unknown, fallback = 0): number {
  if (typeof value === 'number' && Number.isFinite(value)) return value
  if (typeof value === 'string' && value.trim() !== '') {
    const parsed = Number(value)
    if (Number.isFinite(parsed)) return parsed
  }
  return fallback
}

function asString(value: unknown, fallback = ''): string {
  return typeof value === 'string'
    ? value
    : value === null || value === undefined
      ? fallback
      : String(value)
}

function asNullableString(value: unknown): string | null {
  if (typeof value === 'string' && value !== '') return value
  return null
}

function asArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : []
}

/** Accepts either a bare array or an object wrapping it under `key`. */
function asList(value: unknown, key: string): unknown[] {
  if (Array.isArray(value)) return value
  return asArray(asRecord(value)[key])
}

function parseServer(value: unknown): ServerSummary {
  const row = asRecord(value)
  return {
    id: asId(row.id),
    name: asString(row.name, 'unknown server'),
    channels: asNumber(row.channels),
    captured: asNumber(row.captured),
    digests: asNumber(row.digests),
  }
}

function parseChannel(value: unknown): ChannelSummary {
  const row = asRecord(value)
  return {
    id: asId(row.id),
    name: asString(row.name, 'unknown-channel'),
    serverId: asId(row.serverId),
    serverName: asString(row.serverName, 'unknown server'),
    messages: asNumber(row.messages),
    lastMessage: asNullableString(row.lastMessage),
    digests: asNumber(row.digests),
    latestDigest: asNullableString(row.latestDigest),
  }
}

function parseActivityDay(value: unknown): ActivityDay {
  const row = asRecord(value)
  return { date: asString(row.date).slice(0, 10), messages: asNumber(row.messages) }
}

function parseDigest(value: unknown): Digest {
  const row = asRecord(value)
  return {
    id: asNumber(row.id),
    period: asString(row.period, 'daily'),
    start: asString(row.start),
    end: asNullableString(row.end),
    messages: asNumber(row.messages),
    content: asString(row.content),
  }
}

function parseStatus(value: unknown, ok = true): ActionStatus {
  const row = asRecord(value)
  return {
    ok: typeof row.ok === 'boolean' ? row.ok : ok,
    status: asString(row.status, 'done'),
    jobId: typeof row.jobId === 'number' ? row.jobId : null,
  }
}

/* ------------------------------------------------------------------ */
/* Transport                                                           */
/* ------------------------------------------------------------------ */

const DEFAULT_TIMEOUT_MS = 30_000
/** Capture/digest/discover run CLI subprocesses and legitimately take minutes. */
const LONG_TIMEOUT_MS = 20 * 60_000

interface RequestOptions {
  /** Hard cap per request so a stalled fetch surfaces as an error, not a spinner. */
  timeoutMs?: number
  method?: 'GET' | 'POST'
  query?: Record<string, string | number | undefined>
  body?: unknown
  signal?: AbortSignal
}

function buildUrl(path: string, query?: RequestOptions['query']): string {
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(query ?? {})) {
    if (value === undefined || value === '') continue
    search.set(key, String(value))
  }
  const qs = search.toString()
  return `${API_BASE}${path}${qs ? `?${qs}` : ''}`
}

async function readDetail(response: Response): Promise<string | null> {
  try {
    const text = await response.text()
    if (!text) return null
    try {
      const parsed = JSON.parse(text) as unknown
      const detail = asRecord(parsed).detail
      if (typeof detail === 'string') return detail
      if (Array.isArray(detail)) return JSON.stringify(detail)
      return text.slice(0, 400)
    } catch {
      return text.slice(0, 400)
    }
  } catch {
    return null
  }
}

async function requestRaw(path: string, options: RequestOptions = {}): Promise<unknown> {
  const url = buildUrl(path, options.query)
  const timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS
  const controller = new AbortController()
  const timer = setTimeout(
    () => controller.abort(new DOMException(`timed out after ${timeoutMs / 1000}s`, 'TimeoutError')),
    timeoutMs,
  )
  if (options.signal) {
    if (options.signal.aborted) controller.abort(options.signal.reason)
    else options.signal.addEventListener('abort', () => controller.abort(), { once: true })
  }

  let response: Response
  try {
    response = await fetch(url, {
      method: options.method ?? 'GET',
      headers: options.body === undefined ? undefined : { 'Content-Type': 'application/json' },
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
      signal: controller.signal,
    })
  } catch (error) {
    if (error instanceof DOMException && error.name === 'TimeoutError') {
      throw new ApiError(`${path} did not answer within ${timeoutMs / 1000}s`, {
        status: 0,
        detail: 'the server is busy (a digest run holds the store) or unreachable',
        path,
      })
    }
    if (error instanceof DOMException && error.name === 'AbortError') throw error
    throw new ApiError(`Cannot reach the Discord Recall API at ${url}`, {
      status: 0,
      detail: error instanceof Error ? error.message : String(error),
      path,
    })
  } finally {
    clearTimeout(timer)
  }

  if (!response.ok) {
    const detail = await readDetail(response)
    throw new ApiError(detail ?? `Request failed with HTTP ${response.status}`, {
      status: response.status,
      detail,
      path,
    })
  }

  if (response.status === 204) return null
  const text = await response.text()
  if (!text) return null
  try {
    return JSON.parse(text) as unknown
  } catch {
    throw new ApiError(`Expected JSON from ${path} but received a non-JSON body`, {
      status: response.status,
      detail: text.slice(0, 200),
      path,
    })
  }
}

/* ------------------------------------------------------------------ */
/* Endpoints                                                           */
/* ------------------------------------------------------------------ */

/** GET /api/servers */
export async function listServers(signal?: AbortSignal): Promise<ServerSummary[]> {
  const payload = await requestRaw('/api/servers', { signal })
  return asList(payload, 'servers')
    .map(parseServer)
    .filter((server) => server.id !== '')
}

/**
 * GET /api/channels?server=<id>&q=<text>
 *
 * `limit` is not part of the documented contract but the endpoint defaults to
 * 300 rows (max 2000, ordered by message count), which would silently hide the
 * low-traffic channels of a very large server. 2000 is the endpoint maximum and
 * well above any real guild's channel count, so ask for all of it.
 */
export async function listChannels(
  params: { server?: string; q?: string; limit?: number } = {},
  signal?: AbortSignal,
): Promise<ChannelSummary[]> {
  const payload = await requestRaw('/api/channels', {
    query: { server: params.server, q: params.q, limit: params.limit ?? 2000 },
    signal,
  })
  return asList(payload, 'channels')
    .map(parseChannel)
    .filter((channel) => channel.id !== '')
}

/** GET /api/channel/<id>/activity?days=<n> */
export async function fetchChannelActivity(
  channelId: string,
  days = 90,
  signal?: AbortSignal,
): Promise<ChannelActivity> {
  const payload = asRecord(
    await requestRaw(`/api/channel/${encodeURIComponent(channelId)}/activity`, {
      query: { days },
      signal,
    }),
  )
  return {
    days: asList(payload.days, 'days').map(parseActivityDay),
    first: asNullableString(payload.first),
    last: asNullableString(payload.last),
  }
}

/** GET /api/channel/<id>/digests (already newest-first from the API). */
export async function fetchChannelDigests(
  channelId: string,
  signal?: AbortSignal,
): Promise<Digest[]> {
  const payload = await requestRaw(`/api/channel/${encodeURIComponent(channelId)}/digests`, {
    signal,
  })
  return asList(payload, 'digests').map(parseDigest)
}

/*
 * Legacy blocking calls. The UI no longer uses them: the API now enqueues and
 * these endpoints are compat shims that answer straight away. Everything the
 * app runs goes through createJob/queueCapture/queueDigest/queueDiscover below.
 */

/** @deprecated Use queueCapture: it does not tie the UI to one HTTP request. */
export async function captureMessages(
  body: CaptureRequest,
  signal?: AbortSignal,
): Promise<ActionStatus> {
  return parseStatus(await requestRaw('/api/capture', { timeoutMs: LONG_TIMEOUT_MS, method: 'POST', body, signal }))
}

/** @deprecated Use queueDigest: it does not tie the UI to one HTTP request. */
export async function generateDigest(
  body: DigestRequest,
  signal?: AbortSignal,
): Promise<ActionStatus> {
  return parseStatus(await requestRaw('/api/digest', { timeoutMs: LONG_TIMEOUT_MS, method: 'POST', body, signal }))
}

/* ------------------------------------------------------------------ */
/* The job queue: every capture/digest/discover run goes through here. */
/* ------------------------------------------------------------------ */

/** Body of POST /api/jobs. Only the keys relevant to `kind` are sent. */
export interface JobRequest {
  kind: JobKind
  channelId?: string
  since?: string
  until?: string
  maxMessages?: number
  /** Sent under the `from` key: Python keywords make the API use an alias. */
  from?: string
  to?: string
  period?: Period
  force?: boolean
}

/**
 * Result of POST /api/jobs.
 *
 * `jobId` is null when the API accepted the request but refused to queue work
 * (a digest range with no captured messages, for example); `status` then holds
 * the reason, which belongs inline next to the button that failed.
 */
export interface JobQueueResult {
  jobId: number | null
  ok: boolean
  status: string
}

/**
 * POST /api/jobs - queue work and return immediately.
 *
 * The worker reports progress on `GET /api/jobs/<id>`, so this call is never
 * given the long timeout the old blocking endpoints needed.
 */
export async function createJob(body: JobRequest, signal?: AbortSignal): Promise<JobQueueResult> {
  const row = asRecord(await requestRaw('/api/jobs', { method: 'POST', body, signal }))
  const jobId = asJobId(row.jobId ?? row.id)
  return {
    jobId,
    ok: typeof row.ok === 'boolean' ? row.ok : jobId !== null,
    status: asString(row.status, jobId === null ? 'the API queued no job' : 'queued'),
  }
}

/** POST /api/jobs with kind=capture. */
export function queueCapture(
  body: CaptureRequest,
  signal?: AbortSignal,
): Promise<JobQueueResult> {
  return createJob(
    {
      kind: 'capture',
      channelId: body.channelId,
      since: body.since,
      until: body.until,
      maxMessages: body.maxMessages,
    },
    signal,
  )
}

/** POST /api/jobs with kind=digest. */
export function queueDigest(body: DigestRequest, signal?: AbortSignal): Promise<JobQueueResult> {
  return createJob(
    {
      kind: 'digest',
      channelId: body.channelId,
      from: body.from,
      to: body.to,
      period: body.period,
      force: body.force,
    },
    signal,
  )
}

/** POST /api/jobs with kind=discover. */
export function queueDiscover(signal?: AbortSignal): Promise<JobQueueResult> {
  return createJob({ kind: 'discover' }, signal)
}

/** @deprecated Use queueDiscover: it does not tie the UI to one HTTP request. */
export async function discoverChannels(signal?: AbortSignal): Promise<ActionStatus> {
  return parseStatus(await requestRaw('/api/discover', { timeoutMs: LONG_TIMEOUT_MS, method: 'POST', body: {}, signal }))
}

/** POST /api/ask */
export async function askQuestion(
  body: { question: string; channelId?: string },
  signal?: AbortSignal,
): Promise<AskResponse> {
  const payload = asRecord(await requestRaw('/api/ask', { method: 'POST', body, signal }))
  return { answer: asString(payload.answer, 'No answer returned.') }
}

/* ------------------------------------------------------------------ */
/* Background jobs                                                     */
/* ------------------------------------------------------------------ */

export type JobKind = 'capture' | 'digest' | 'discover' | 'other'
export type JobState = 'queued' | 'running' | 'done' | 'error'

export interface JobStatus {
  id: number
  kind: JobKind
  status: JobState
  phase: string
  message: string
  messages: number
  /** Null when the job cannot estimate progress (e.g. discover). */
  percent: number | null
  channelId: string | null
  createdAt: string | null
  updatedAt: string | null
  finishedAt: string | null
}

/** Job ids are small integers, unlike Discord snowflakes. */
function asJobId(value: unknown): number | null {
  if (typeof value === 'number' && Number.isFinite(value)) return Math.trunc(value)
  if (typeof value === 'string' && /^\d+$/.test(value.trim())) return Number(value.trim())
  return null
}

function asJobKind(value: unknown): JobKind {
  const raw = asString(value).trim().toLowerCase()
  return raw === 'capture' || raw === 'digest' || raw === 'discover' ? raw : 'other'
}

/**
 * Unknown statuses are resolved through `finishedAt`: with one, the job is over
 * (stop polling), without one it is still running (keep polling; the watcher
 * gives up after an hour either way). A spelling change must never wedge a
 * spinner or hide a finished run.
 */
function asJobState(value: unknown, finishedAt: string | null): JobState {
  switch (asString(value).trim().toLowerCase()) {
    case 'queued':
    case 'pending':
    case 'waiting':
      return 'queued'
    case 'running':
    case 'active':
    case 'working':
    case 'starting':
      return 'running'
    case 'done':
    case 'ok':
    case 'success':
    case 'complete':
    case 'completed':
    case 'finished':
      return 'done'
    case 'error':
    case 'failed':
    case 'failure':
      return 'error'
    default:
      return finishedAt ? 'done' : 'running'
  }
}

/** Accepts 0-100 or a 0-1 fraction and clamps to a sane percentage. */
function asPercent(value: unknown): number | null {
  if (value === null || value === undefined || value === '') return null
  const raw = asNumber(value, Number.NaN)
  if (!Number.isFinite(raw)) return null
  const scaled = raw > 0 && raw <= 1 && !Number.isInteger(raw) ? raw * 100 : raw
  return Math.min(100, Math.max(0, Math.round(scaled * 10) / 10))
}

/**
 * Job timestamps are UTC instants serialized without a timezone suffix (the
 * store drops tzinfo), e.g. "2026-09-23T19:57:12.345". Read as-is that is 5.5
 * hours off for this user, so a bare instant is marked as UTC. Values that
 * already carry `Z` or an offset are left untouched.
 */
function asUtcInstant(value: unknown): string | null {
  const raw = asNullableString(value)
  if (!raw) return null
  if (/[zZ]$|[+-]\d{2}:?\d{2}$/.test(raw)) return raw
  if (/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?$/.test(raw)) return `${raw}Z`
  return raw
}

function parseJob(value: unknown): JobStatus {
  const row = asRecord(value)
  const finishedAt = asUtcInstant(row.finishedAt ?? row.finished_at)
  return {
    id: asJobId(row.id ?? row.jobId) ?? 0,
    kind: asJobKind(row.kind),
    status: asJobState(row.status ?? row.state, finishedAt),
    phase: asString(row.phase),
    message: asString(row.message),
    messages: asNumber(row.messages),
    percent: asPercent(row.percent ?? row.progress),
    channelId: asNullableString(row.channelId ?? row.channel_id),
    createdAt: asUtcInstant(row.createdAt ?? row.created_at),
    updatedAt: asUtcInstant(row.updatedAt ?? row.updated_at),
    finishedAt,
  }
}

function parseJobList(value: unknown): JobStatus[] {
  return asList(value, 'jobs')
    .map(parseJob)
    .filter((job) => job.id > 0)
}

/** A job still worth polling. */
export function isJobActive(job: JobStatus): boolean {
  return job.status === 'queued' || job.status === 'running'
}

/** GET /api/jobs/<id> */
export async function getJob(id: number, signal?: AbortSignal): Promise<JobStatus> {
  const path = `/api/jobs/${id}`
  const job = parseJob(await requestRaw(path, { signal }))
  if (job.id <= 0) {
    throw new ApiError(`${path} answered without a job id`, {
      status: 0,
      detail: 'the response shape changed, so progress cannot be tracked',
      path,
    })
  }
  return job
}

/** GET /api/jobs?limit= */
export async function listJobs(
  options: { limit?: number } = {},
  signal?: AbortSignal,
): Promise<JobStatus[]> {
  const payload = await requestRaw('/api/jobs', {
    query: { limit: options.limit ?? 10 },
    signal,
  })
  return parseJobList(payload)
}
