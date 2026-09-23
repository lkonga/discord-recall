import { useMemo, useState } from 'react'
import { FileTextIcon, RefreshCwIcon, ScrollTextIcon, TriangleAlertIcon } from 'lucide-react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader } from '@/components/ui/card'
import { ScrollArea } from '@/components/ui/scroll-area'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { Skeleton } from '@/components/ui/skeleton'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { cn } from 'cn'
import type { ChannelSummary, Digest } from '@/lib/api'
import { formatCount, formatDay, formatDateTime } from '@/lib/format'

const PREVIEW_CHARS = 900

interface DigestBrowserProps {
  channel: ChannelSummary | null
  digests: Digest[]
  loading: boolean
  error: Error | null
  onReload: () => void
}

/** Digest reader: markdown cards (default) plus a compact index table. */
export function DigestBrowser({ channel, digests, loading, error, onReload }: DigestBrowserProps) {
  const [periodFilter, setPeriodFilter] = useState<'all' | 'daily' | 'weekly' | 'monthly'>('all')
  const [expanded, setExpanded] = useState<Record<number, boolean>>({})

  const sorted = useMemo(
    () => [...digests].sort((a, b) => (a.start < b.start ? 1 : a.start > b.start ? -1 : 0)),
    [digests],
  )

  const filtered = useMemo(
    () => (periodFilter === 'all' ? sorted : sorted.filter((d) => d.period === periodFilter)),
    [sorted, periodFilter],
  )

  return (
    <Card className="flex flex-col">
      <CardHeader className="gap-2">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex items-center gap-2">
            <ScrollTextIcon className="size-4 text-muted-foreground" />
            <span className="text-base font-semibold">Digests</span>
            {channel && (
              <Badge variant="secondary" className="tabular-nums">
                {formatCount(channel.digests)} stored
              </Badge>
            )}
            {channel?.latestDigest && (
              <span className="text-xs text-muted-foreground">
                newest runs to {formatDateTime(channel.latestDigest)}
              </span>
            )}
          </div>
          <div className="flex items-center gap-2">
            <Select
              value={periodFilter}
              onValueChange={(value) => setPeriodFilter(value as typeof periodFilter)}
            >
              <SelectTrigger size="sm" aria-label="Filter digests by period" className="w-[8.5rem]">
                <SelectValue />
              </SelectTrigger>
              <SelectContent align="end">
                <SelectItem value="all">All periods</SelectItem>
                <SelectItem value="daily">Daily only</SelectItem>
                <SelectItem value="weekly">Weekly only</SelectItem>
                <SelectItem value="monthly">Monthly only</SelectItem>
              </SelectContent>
            </Select>
            <Button variant="ghost" size="icon-sm" onClick={onReload} aria-label="Reload digests">
              <RefreshCwIcon className={cn(loading && 'animate-spin')} />
            </Button>
          </div>
        </div>
      </CardHeader>
      <CardContent>
        {channel === null ? (
          <p className="py-8 text-center text-sm text-muted-foreground">
            Choose a server and a channel to read its digests.
          </p>
        ) : loading && digests.length === 0 ? (
          <div className="flex flex-col gap-3">
            <Skeleton className="h-32 w-full" />
            <Skeleton className="h-32 w-full" />
          </div>
        ) : error && digests.length === 0 ? (
          <p className="flex items-center gap-2 py-6 text-sm text-destructive">
            <TriangleAlertIcon className="size-4" /> {error.message}
          </p>
        ) : filtered.length === 0 ? (
          <div className="flex flex-col items-center gap-2 py-10 text-center">
            <FileTextIcon className="size-5 text-muted-foreground" />
            <p className="text-sm font-medium">
              {sorted.length === 0
                ? 'No digests for this channel yet'
                : 'No digests match the filter'}
            </p>
            <p className="max-w-md text-xs text-muted-foreground">
              Capture the channel for a date range, then press Generate digest. The result appears
              here newest-first.
            </p>
          </div>
        ) : (
          <Tabs defaultValue="cards" className="gap-4">
            <TabsList>
              <TabsTrigger value="cards">
                Cards
                <Badge variant="outline" className="ml-1 tabular-nums">
                  {filtered.length}
                </Badge>
              </TabsTrigger>
              <TabsTrigger value="table">Index</TabsTrigger>
            </TabsList>

            <TabsContent value="cards" className="flex flex-col gap-4">
              {filtered.map((digest) => (
                <Card key={digest.id} className="gap-0 overflow-hidden py-0">
                  <CardHeader className="gap-1 border-b border-border/70 bg-muted/20 px-4 py-3">
                    <div className="flex flex-wrap items-center gap-2">
                      <Badge variant="default" className="uppercase">
                        {digest.period}
                      </Badge>
                      <span className="text-sm font-medium">
                        {formatDay(digest.start)}
                        {digest.end && digest.end.slice(0, 10) !== digest.start.slice(0, 10)
                          ? ` → ${formatDay(digest.end)}`
                          : ''}
                      </span>
                      <Badge variant="outline" className="tabular-nums">
                        {formatCount(digest.messages)} messages
                      </Badge>
                      <span className="ml-auto text-[11px] text-muted-foreground">
                        #{digest.id}
                      </span>
                    </div>
                  </CardHeader>
                  <CardContent className="px-4 py-3">
                    <DigestMarkdown
                      content={digest.content}
                      expanded={expanded[digest.id] ?? false}
                      onToggle={() =>
                        setExpanded((previous) => ({
                          ...previous,
                          [digest.id]: !(previous[digest.id] ?? false),
                        }))
                      }
                    />
                  </CardContent>
                </Card>
              ))}
            </TabsContent>

            <TabsContent value="table">
              <ScrollArea className="max-h-[28rem]">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Period</TableHead>
                      <TableHead>Start</TableHead>
                      <TableHead>End</TableHead>
                      <TableHead className="text-right">Messages</TableHead>
                      <TableHead>Preview</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {filtered.map((digest) => (
                      <TableRow key={digest.id}>
                        <TableCell className="font-medium capitalize">{digest.period}</TableCell>
                        <TableCell className="whitespace-nowrap">
                          {formatDay(digest.start)}
                        </TableCell>
                        <TableCell className="whitespace-nowrap">
                          {digest.end ? formatDay(digest.end) : '—'}
                        </TableCell>
                        <TableCell className="text-right tabular-nums">
                          {formatCount(digest.messages)}
                        </TableCell>
                        <TableCell className="max-w-[28rem] truncate text-muted-foreground">
                          {firstLine(digest.content)}
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </ScrollArea>
            </TabsContent>
          </Tabs>
        )}
      </CardContent>
    </Card>
  )
}

interface DigestMarkdownProps {
  content: string
  expanded: boolean
  onToggle: () => void
}

function DigestMarkdown({ content, expanded, onToggle }: DigestMarkdownProps) {
  const long = content.length > PREVIEW_CHARS
  const text = !long || expanded ? content : truncateAtParagraph(content, PREVIEW_CHARS)

  return (
    <div className="flex flex-col gap-2">
      <div
        className={cn(
          'prose prose-sm dark:prose-invert max-w-none',
          'prose-headings:mt-4 prose-headings:mb-2 prose-headings:font-semibold',
          'prose-p:my-2 prose-ul:my-2 prose-li:my-0.5 prose-hr:my-3',
          'prose-code:rounded prose-code:bg-muted prose-code:px-1 prose-code:py-0.5',
          'prose-pre:bg-muted/60 prose-table:text-xs prose-strong:text-foreground',
          'text-sm break-words',
        )}
      >
        <ReactMarkdown
          remarkPlugins={[remarkGfm]}
          components={{
            a: ({ children, ...props }) => (
              <a {...props} target="_blank" rel="noreferrer noopener">
                {children}
              </a>
            ),
          }}
        >
          {text}
        </ReactMarkdown>
      </div>
      {long && (
        <Button variant="ghost" size="sm" className="w-fit" onClick={onToggle}>
          {expanded ? 'Show less' : `Show full digest (${formatCount(content.length)} chars)`}
        </Button>
      )}
    </div>
  )
}

/** Cuts markdown at a blank line so the preview stays syntactically sane. */
function truncateAtParagraph(content: string, limit: number): string {
  const slice = content.slice(0, limit)
  const boundary = slice.lastIndexOf('\n\n')
  return boundary > limit * 0.5 ? slice.slice(0, boundary) : slice
}

function firstLine(content: string): string {
  const line = content.split('\n').find((candidate) => candidate.trim() !== '')
  if (!line) return '—'
  return line.replace(/[*#_`>]/g, '').trim()
}
