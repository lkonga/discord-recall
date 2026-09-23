import { Fragment, useMemo } from 'react'
import { EyeIcon, ListChecksIcon, Loader2Icon, RefreshCwIcon } from 'lucide-react'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { cn } from 'cn'
import type { JobStatus } from '@/lib/api'
import { relativeFromNow } from '@/lib/format'
import { JOB_KIND_LABEL, JOB_STATE_LABEL, jobCounterLabel } from '@/lib/job-text'

interface JobsPanelProps {
  jobs: JobStatus[]
  loading: boolean
  /** True while the panel is refreshing itself every 5s. */
  polling: boolean
  error: Error | null
  /** Display-ready channel name without the leading "#"; null for whole-DB jobs. */
  channelLabel: (channelId: string | null) => string
  onReload: () => void
  onWatch: (jobId: number) => void
  watchingJobId: number | null
}

function statusVariant(status: JobStatus['status']) {
  if (status === 'error') return 'destructive' as const
  if (status === 'done') return 'default' as const
  return 'secondary' as const
}

/**
 * Queue backlog: the last few jobs with their phase and message counter. Polls
 * itself while something is active and works on demand otherwise, so a run
 * started in another tab is still visible here.
 */
export function JobsPanel({
  jobs,
  loading,
  polling,
  error,
  channelLabel,
  onReload,
  onWatch,
  watchingJobId,
}: JobsPanelProps) {
  const activeCount = useMemo(
    () => jobs.filter((job) => job.status === 'queued' || job.status === 'running').length,
    [jobs],
  )

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-base">
          <ListChecksIcon className="size-4 text-muted-foreground" />
          Recent jobs
          {activeCount > 0 && (
            <Badge variant="secondary" className="gap-1 tabular-nums">
              <Loader2Icon className="animate-spin" />
              {activeCount} active
            </Badge>
          )}
        </CardTitle>
        <CardDescription className="flex flex-wrap items-center gap-2">
          <span>
            {polling
              ? 'refreshing every 5s while a job runs'
              : 'idle — refresh to check for jobs started elsewhere'}
          </span>
          <Button
            variant="ghost"
            size="xs"
            onClick={onReload}
            disabled={loading}
            aria-label="Reload recent jobs"
            title="Reload recent jobs"
          >
            <RefreshCwIcon className={cn(loading && 'animate-spin')} />
            refresh
          </Button>
        </CardDescription>
      </CardHeader>
      <CardContent>
        {error && (
          <p className="mb-2 text-xs text-destructive">
            could not read the job list: {error.message}
          </p>
        )}

        {jobs.length === 0 ? (
          <p className="text-xs text-muted-foreground">
            {loading
              ? 'loading recent jobs…'
              : 'No jobs yet. Capture, digest and discover runs all show up here.'}
          </p>
        ) : (
          <div className="overflow-x-auto">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Kind</TableHead>
                  <TableHead>Channel</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead>Phase</TableHead>
                  <TableHead className="text-right">Progress</TableHead>
                  <TableHead className="text-right">Updated</TableHead>
                  <TableHead className="w-10" />
                </TableRow>
              </TableHeader>
              <TableBody>
                {jobs.map((job) => {
                  const active = job.status === 'queued' || job.status === 'running'
                  const counter = jobCounterLabel(job)
                  return (
                    <Fragment key={job.id}>
                      <TableRow>
                        <TableCell className="font-medium">
                          {JOB_KIND_LABEL[job.kind]} #{job.id}
                        </TableCell>
                        <TableCell className="max-w-[12rem] truncate text-muted-foreground">
                          {job.channelId ? `#${channelLabel(job.channelId)}` : '—'}
                        </TableCell>
                        <TableCell>
                          <Badge variant={statusVariant(job.status)} className="gap-1">
                            {active && <Loader2Icon className="animate-spin" />}
                            {JOB_STATE_LABEL[job.status]}
                          </Badge>
                        </TableCell>
                        <TableCell className="max-w-[14rem] truncate text-muted-foreground">
                          {job.phase || '—'}
                        </TableCell>
                        <TableCell className="text-right tabular-nums">
                          {counter ?? '—'}
                        </TableCell>
                        <TableCell className="text-right text-muted-foreground">
                          {relativeFromNow(job.updatedAt)}
                        </TableCell>
                        <TableCell>
                          <Button
                            variant="ghost"
                            size="icon-xs"
                            onClick={() => onWatch(job.id)}
                            aria-label={`Follow job ${job.id}`}
                            title={
                              watchingJobId === job.id
                                ? 'Already following this job'
                                : 'Follow this job'
                            }
                            disabled={watchingJobId === job.id}
                          >
                            <EyeIcon />
                          </Button>
                        </TableCell>
                      </TableRow>
                      {job.status === 'error' && (
                        <TableRow className="border-0">
                          <TableCell
                            colSpan={7}
                            className="pt-0 text-xs whitespace-normal text-destructive"
                          >
                            {job.message || 'the worker reported no reason'}
                          </TableCell>
                        </TableRow>
                      )}
                    </Fragment>
                  )
                })}
              </TableBody>
            </Table>
          </div>
        )}
      </CardContent>
    </Card>
  )
}
