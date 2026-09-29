import { useState } from 'react'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Label } from '@/components/ui/label'
import type { PackageCredit } from '@/lib/api'
import { centsToDollars } from '@/lib/money'

const CHARGE = 'charge'

/**
 * "Complete — use a package?" (#72). Shown only when the client holds an eligible package for
 * this service; the first package is preselected because that is what a client who bought
 * one expects, and "Charge the regular price" is always one click away. The credit is spent
 * by the server inside the completion transaction, never here.
 */
export function PackagePicker(props: {
  clientName: string
  credits: PackageCredit[]
  pending: boolean
  onConfirm: (packagePurchaseId: string | null) => void
  onCancel: () => void
}) {
  const [choice, setChoice] = useState(props.credits[0]?.package_purchase_id ?? CHARGE)
  const options = [
    ...props.credits.map((c) => ({
      value: c.package_purchase_id,
      label: c.name,
      detail: `${c.credits_remaining} of ${c.credits_total} left · this session $${centsToDollars(c.value_cents)}${
        c.expires_at ? ` · expires ${c.expires_at}` : ''
      }`,
    })),
    { value: CHARGE, label: 'Charge the regular price', detail: 'No package credit is used.' },
  ]
  return (
    <Dialog open onOpenChange={(open) => !open && props.onCancel()}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Complete this appointment</DialogTitle>
          <DialogDescription>
            {props.clientName} has a prepaid package that covers this service.
          </DialogDescription>
        </DialogHeader>
        <fieldset className="flex flex-col gap-3">
          <legend className="sr-only">Payment for this session</legend>
          {options.map((o) => (
            <div key={o.value} className="flex gap-3">
              <input
                type="radio"
                id={`package-${o.value}`}
                name="package-choice"
                className="mt-1 size-4 accent-primary"
                checked={choice === o.value}
                disabled={props.pending}
                onChange={() => setChoice(o.value)}
              />
              <div className="flex flex-col gap-1">
                <Label htmlFor={`package-${o.value}`}>{o.label}</Label>
                <p className="text-xs text-muted-foreground tabular-nums">{o.detail}</p>
              </div>
            </div>
          ))}
        </fieldset>
        <DialogFooter>
          <Button type="button" variant="outline" onClick={props.onCancel}>
            Not yet
          </Button>
          <Button
            type="button"
            disabled={props.pending}
            onClick={() => props.onConfirm(choice === CHARGE ? null : choice)}
          >
            Complete
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
