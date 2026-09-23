import { Progress as ProgressPrimitive } from '@base-ui/react/progress'
import { cn } from 'cn'

/**
 * Determinate/indeterminate progress bar.
 *
 * `value` of null renders the indeterminate sweep (the job API reports
 * `percent: null` for work with no known total); the base-ui indicator is only
 * used when a percentage exists, because it writes an inline width that would
 * otherwise override the sweep animation.
 */
function Progress({
  className,
  value,
  ...props
}: ProgressPrimitive.Root.Props) {
  return (
    <ProgressPrimitive.Root
      data-slot="progress"
      value={value}
      className={cn('w-full', className)}
      {...props}
    >
      <ProgressPrimitive.Track
        data-slot="progress-track"
        className="relative h-1.5 w-full overflow-hidden rounded-full bg-muted"
      >
        {value === null ? (
          <div
            data-slot="progress-indicator"
            data-indeterminate
            className="h-full w-1/4 rounded-full bg-primary animate-[progress-sweep_1.4s_ease-in-out_infinite]"
          />
        ) : (
          <ProgressPrimitive.Indicator
            data-slot="progress-indicator"
            className="h-full rounded-full bg-primary transition-[width] duration-500 ease-out"
          />
        )}
      </ProgressPrimitive.Track>
    </ProgressPrimitive.Root>
  )
}

export { Progress }
