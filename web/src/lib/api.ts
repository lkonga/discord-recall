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

/** POST /api/capture */
export async function captureMessages(
  body: CaptureRequest,
  signal?: AbortSignal,
): Promise<ActionStatus> {
  return parseStatus(await requestRaw('/api/capture', { timeoutMs: LONG_TIMEOUT_MS, method: 'POST', body, signal }))
}

/** POST /api/digest */
export async function generateDigest(
  body: DigestRequest,
  signal?: AbortSignal,
): Promise<ActionStatus> {
  return parseStatus(await requestRaw('/api/digest', { timeoutMs: LONG_TIMEOUT_MS, method: 'POST', body, signal }))
}

/** POST /api/discover */
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
