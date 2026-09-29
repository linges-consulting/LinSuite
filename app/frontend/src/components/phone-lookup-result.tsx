import { useNavigate } from 'react-router'
import { ClassificationBadge } from '@/components/classification-badge'
import { Button } from '@/components/ui/button'
import type { PhoneLookupResult } from '@/lib/api'
import { formatPhone } from '@/lib/phone'

/**
 * The three shapes `GET /api/cti/lookup` can answer, rendered the same way whether it was
 * read for a screen-pop or typed by hand (Phase 14, #16 — `routes/phone-lookup.tsx` and
 * `components/screen-pop-panel.tsx` both use this, so there is one place "what a match looks
 * like" is drawn).
 *
 * "Book appointment" is the quick-book shortcut: it hands the matched `CustomerRecord`
 * straight to the schedule screen's own "New appointment" dialog via router state
 * (`routes/schedule.tsx`), the same shape picking a customer from that dialog's own search
 * already produces — no second customer fetch, no second lookup.
 */
export function PhoneLookupResultView({ result }: { result: PhoneLookupResult }) {
  const navigate = useNavigate()

  if (result.status === 'no_match') {
    return <p className="text-sm text-muted-foreground">No client matches that number.</p>
  }

  if (result.status === 'candidates') {
    return (
      <ul className="flex flex-col gap-2">
        {result.candidates.map((c) => (
          <li
            key={c.id}
            className="flex items-center justify-between gap-2 rounded-lg border px-3 py-2 text-sm"
          >
            <span className="flex flex-wrap items-center gap-x-2">
              <span className="font-medium">
                {c.first_name} {c.last_name}
              </span>
              {c.phone && <span className="tabular-nums text-muted-foreground">{formatPhone(c.phone)}</span>}
            </span>
            <ClassificationBadge classification={c.classification} />
          </li>
        ))}
      </ul>
    )
  }

  const { customer, previous_providers } = result.match!
  return (
    <div className="flex flex-col gap-3 rounded-lg border p-3">
      <div className="flex items-center justify-between gap-2">
        <span className="flex flex-wrap items-center gap-x-2">
          <span className="font-medium">
            {customer.first_name} {customer.last_name}
          </span>
          {customer.phone && (
            <span className="tabular-nums text-muted-foreground">{formatPhone(customer.phone)}</span>
          )}
        </span>
        <ClassificationBadge classification={customer.classification} />
      </div>
      {previous_providers.length > 0 && (
        <p className="text-sm text-muted-foreground">
          Seen before by {previous_providers.map((p) => p.display_name).join(', ')}
        </p>
      )}
      <Button
        type="button"
        size="sm"
        className="self-start"
        onClick={() => navigate('/schedule', { state: { quickBookCustomer: customer } })}
      >
        Book appointment
      </Button>
    </div>
  )
}
