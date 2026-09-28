import { Link2Off } from 'lucide-react'

/**
 * A link that no longer works — a wrong/expired form link (`routes/public-form.tsx`), or a
 * booking-management link past its appointment's start, already cancelled, or simply unknown
 * (`routes/booking-manage.tsx`). Extracted from `public-form.tsx` (Phase 6 Task 6, #10) so
 * both public dead-link screens read the same way rather than drifting apart in wording.
 */
export function DeadEnd(props: { title: string; text: string }) {
  return (
    <div className="flex flex-col items-center gap-3 rounded-xl border border-dashed px-6 py-12 text-center">
      <Link2Off className="size-6 text-muted-foreground" aria-hidden />
      <h1 className="text-lg font-semibold">{props.title}</h1>
      <p className="max-w-sm text-muted-foreground">{props.text}</p>
    </div>
  )
}
