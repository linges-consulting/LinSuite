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
 * "Cancel this appointment?" with an optional reason (Task 18) — the same dialog for one
 * appointment and for a whole booking group, told apart by `title`/`description` only. The
 * reason is free text, stored on the row(s) and shown on the cancelled card's tooltip.
 */
export function CancelConfirm(props: {
  title: string
  description: string
  pending: boolean
  onConfirm: (reason: string | null) => void
  onCancel: () => void
}) {
  const [reason, setReason] = useState('')
  return (
    <Dialog open onOpenChange={(open) => !open && props.onCancel()}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>{props.title}</DialogTitle>
          <DialogDescription>{props.description}</DialogDescription>
        </DialogHeader>
        <Field label="Reason (optional)" htmlFor="cancel-reason">
          <Input
            id="cancel-reason"
            maxLength={500}
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            placeholder="Client called to cancel"
          />
        </Field>
        <DialogFooter>
          <Button type="button" variant="outline" onClick={props.onCancel}>
            Never mind
          </Button>
          <Button
            type="button"
            variant="destructive"
            disabled={props.pending}
            onClick={() => props.onConfirm(reason.trim() || null)}
          >
            Cancel it
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
