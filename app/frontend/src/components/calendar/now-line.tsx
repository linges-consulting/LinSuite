/**
 * The current time in the business's zone, across a column that is today (tech-stack §13).
 * Red is the one colour the spec asks for by name; `destructive` is the token that is red.
 */
export function NowLine({ minutes }: { minutes: number }) {
  return (
    <div
      className="pointer-events-none absolute inset-x-0 z-20"
      style={{ top: minutes }}
      data-testid="now-line"
      aria-hidden
    >
      <div className="absolute -left-1 -top-[3px] size-2 rounded-full bg-destructive" />
      <div className="h-0.5 bg-destructive" />
    </div>
  )
}
