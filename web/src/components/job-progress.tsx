import {
  DownloadIcon,
  Loader2Icon,
  RefreshCwIcon,
  SparklesIcon,
  XIcon,
} from 'lucide-react'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Progress } from '@/components/ui/progress'
import { cn } from 'cn'
import type { JobStatus } from '@/lib/api'
import { relativeFromNow } from '@/lib/format'
import { JOB_KIND_LABEL, JOB_STATE_LABEL, jobCounterLabel } from '@/lib/job-text'

interface JobProgressCardProps {
  job: JobStatus
  /** Display-ready channel name without the leading "#". */
  channelName: string | null
  /** What the run covers, e.g. "Sep 17 → Sep 24 · daily". */
  scopeNote: string | null
  /** True while the 1.5s poll loop is running. */
  polling: boolean
  /** Set when polling this job failed; polling has already stopped. */
  error: Error | null
  onDismiss: () => void
}

const KIND_ICON = {
  capture: DownloadIcon,
  digest: SparklesIcon,
  discover: RefreshCwIcon,
  other: RefreshCwIcon,
} as const

/**
 * Live progress for the job this tab is following: phase, messages fetched and
 * a bar that sweeps while the API reports no percentage.
 */
export function JobProgressCard({
  job,
  channelName,
  scopeNote,
  polling,
  error,
  onDismiss,
}: JobProgressCardProps) {
  const Icon = KIND_ICON[job.kind]
  const active = job.status === 'queued' || job.status === 'running'
  const counter = jobCounterLabel(job)
  const percentText = job.percent === null ? null : `${Math.round(job.percent)}%`

  return (
    <Card className={cn(job.status === 'error' && 'border-destructive/50')}>
      <CardHeader>
        <CardTitle className="flex flex-wrap items-center gap-2 text-sm">
          <Icon className={cn('size-4 text-muted-foreground', active && 'animate-pulse')} />
          <span>{JOB_KIND_LABEL[job.kind]} job {job.id}</span>
          {channelName && (
            <Badge variant="outline" className="max-w-[16rem] truncate">
              #{channelName}
            </Badge>
          )}
          <Badge
            variant={
              job.status === 'error' ? 'destructive' : job.status === 'done' ? 'default' : 'secondary'
            }
            className="gap-1"
          >
            {active && <Loader2Icon className="animate-spin" />}
            {JOB_STATE_LABEL[job.status]}
          </Badge>
          {percentText && <span className="text-xs text-muted-foreground">{percentText}</span>}
          <Button
            variant="ghost"
            size="icon-xs"
            className="ml-auto"
            onClick={onDismiss}
            aria-label="Stop following this job"
            title="Stop following this job"
          >
            <XIcon />
          </Button>
        </CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-2">
        <Progress
          value={active ? job.percent : job.status === 'error' ? 0 : 100}
          aria-label={`${JOB_KIND_LABEL[job.kind]} progress`}
          aria-valuetext={percentText ?? (active ? 'in progress' : JOB_STATE_LABEL[job.status])}
        />
        <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
          {job.phase && (
            <Badge variant="secondary" className="font-normal">
              {job.phase}
            </Badge>
          )}
          {counter && <span>{counter} so far</span>}
          {!active && job.finishedAt && <span>· finished {relativeFromNow(job.finishedAt)}</span>}
          {active && polling && <span>· updating every 1.5s</span>}
          {active && !polling && <span>· polling paused</span>}
        </div>
        {scopeNote && <p className="text-[11px] text-muted-foreground">{scopeNote}</p>}
        {job.message && job.status !== 'error' && (
          <p className="truncate text-[11px] text-muted-foreground" title={job.message}>
            {job.message}
          </p>
        )}
        {job.status === 'error' && (
          <p className="text-xs text-destructive">
            {job.message || 'the worker reported no reason'}
          </p>
        )}
        {error && (
          <p className="text-xs text-destructive">
            progress polling stopped: {error.message}
          </p>
        )}
      </CardContent>
    </Card>
  )
}
