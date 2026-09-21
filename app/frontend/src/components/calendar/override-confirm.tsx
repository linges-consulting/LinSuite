import { useState } from 'react'
import { Field } from '@/components/form'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'

/**
 * "This runs 30 minutes past Maria's shift end — continue?" (tech-stack §22).
 *
 * The server has said a start breaks one or more *advisory* rules — the shift, time off, a
 * closure, the booking horizon — and could be booked past them by a human, on the record.
 * This dialog puts each rule into words (`lib/calendar/overrides.ts`), takes an optional
 * reason, and either confirms or explains who could. The physical rules never reach here: a
 * busy room is `not_offered`, and there is no dialog for it.
 *
 * Who may confirm is decided by the server on the resubmit; the same rule is applied here
 * only to leave out a Confirm button that would be refused, and to say who could press it.
 */
export function OverrideConfirm(props: {
  sentences: string[]
  /** Set when this session may not confirm: shown instead of the Confirm button. */
  forbidden: string | null
  pending: boolean
  onConfirm: (reason: string | null) => void
  onCancel: () => void
}) {
  const [reason, setReason] = useState('')
  return (
    <Dialog open onOpenChange={(open) => !open && props.onCancel()}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Book outside availability?</DialogTitle>
          <DialogDescription>
            This time is outside the schedule. Rooms and equipment are never overridden; only the
            rules below would be set aside, and the override is recorded.
          </DialogDescription>
        </DialogHeader>
        <ul className="list-disc space-y-1 pl-5 text-sm" aria-label="Rules this breaks">
          {props.sentences.map((sentence) => (
            <li key={sentence}>{sentence}</li>
          ))}
        </ul>
        {props.forbidden ? (
          <p role="alert" className="text-sm text-muted-foreground">
            {props.forbidden}
          </p>
        ) : (
          <Field label="Reason (optional)" htmlFor="override-reason">
            <Input
              id="override-reason"
              maxLength={500}
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              placeholder="Client asked; happy to stay"
            />
          </Field>
        )}
        <DialogFooter>
          <Button type="button" variant="outline" onClick={props.onCancel}>
            Cancel
          </Button>
          {!props.forbidden && (
            <Button
              type="button"
              disabled={props.pending}
              onClick={() => props.onConfirm(reason.trim() || null)}
            >
              Confirm
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
