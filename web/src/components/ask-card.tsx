import { useState } from 'react'
import { Loader2Icon, MessageCircleQuestionIcon, SparklesIcon } from 'lucide-react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { toast } from 'sonner'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Textarea } from '@/components/ui/textarea'
import { cn } from 'cn'
import { askQuestion, type ChannelSummary } from '@/lib/api'

interface AskCardProps {
  channel: ChannelSummary | null
}

/** Ad-hoc question against the captured history (POST /api/ask). */
export function AskCard({ channel }: AskCardProps) {
  const [question, setQuestion] = useState('')
  const [scope, setScope] = useState<'channel' | 'everything'>('channel')
  const [answer, setAnswer] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const disabled = busy || question.trim().length === 0 || (scope === 'channel' && channel === null)

  const submit = async () => {
    const trimmed = question.trim()
    if (!trimmed) return
    setBusy(true)
    const toastId = toast.loading('Asking the archive…')
    try {
      const response = await askQuestion({
        question: trimmed,
        channelId: scope === 'channel' && channel ? channel.id : undefined,
      })
      setAnswer(response.answer)
      toast.success('Answer ready', { id: toastId })
    } catch (error) {
      toast.error(error instanceof Error ? error.message : 'Ask failed', { id: toastId })
    } finally {
      setBusy(false)
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-base">
          <MessageCircleQuestionIcon className="size-4 text-muted-foreground" />
          Ask the archive
        </CardTitle>
        <CardDescription>
          Free-form question over the captured messages, answered by the configured LLM.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <Textarea
          value={question}
          onChange={(event) => setQuestion(event.target.value)}
          placeholder="What did people decide about the loading-state bug?"
          rows={2}
          onKeyDown={(event) => {
            if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) {
              event.preventDefault()
              void submit()
            }
          }}
        />
        <div className="flex flex-wrap items-center gap-2">
          <Button
            variant={scope === 'channel' ? 'secondary' : 'ghost'}
            size="sm"
            aria-pressed={scope === 'channel'}
            onClick={() => setScope('channel')}
            disabled={channel === null}
          >
            <span className="tabular-nums">{channel ? `#${channel.name}` : 'no channel'}</span>
          </Button>
          <Button
            variant={scope === 'everything' ? 'secondary' : 'ghost'}
            size="sm"
            aria-pressed={scope === 'everything'}
            onClick={() => setScope('everything')}
          >
            everything captured
          </Button>
          <span className="text-[11px] text-muted-foreground">⌘/Ctrl + Enter</span>
          <Button className="ml-auto" onClick={() => void submit()} disabled={disabled}>
            {busy ? <Loader2Icon className="animate-spin" /> : <SparklesIcon />}
            {busy ? 'Asking…' : 'Ask'}
          </Button>
        </div>
        {answer !== null && (
          <div className="rounded-lg border border-border/70 bg-muted/20 p-3">
            <div className="mb-1 flex items-center gap-2">
              <Badge variant="outline">answer</Badge>
              {channel && scope === 'channel' && <Badge variant="secondary">#{channel.name}</Badge>}
            </div>
            <div
              className={cn(
                'prose prose-sm dark:prose-invert max-w-none text-sm',
                'prose-p:my-2 prose-ul:my-2 prose-headings:mt-3 prose-headings:mb-1',
              )}
            >
              <ReactMarkdown remarkPlugins={[remarkGfm]}>{answer}</ReactMarkdown>
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  )
}
