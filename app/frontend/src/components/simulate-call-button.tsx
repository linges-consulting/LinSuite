import { useMutation } from '@tanstack/react-query'
import { PhoneIncoming } from 'lucide-react'
import { toast } from 'sonner'
import { Button } from '@/components/ui/button'
import { useDemoCallTrigger } from '@/lib/call-events'

/**
 * The demo trigger (Phase 14, #16 decision #1). Its only caller decides whether to render it
 * at all (`routes/phone-lookup.tsx`, gated on `GET /api/cti/demo-mode`) — this component
 * itself does not re-check, since the server-side 404 while demo mode is off is the actual
 * enforcement (`scheduling/cti.py::simulate_call`); a stale render here would just show a
 * toast, never a working control nobody was supposed to see.
 */
export function SimulateCallButton() {
  const source = useDemoCallTrigger()
  const trigger = useMutation({
    mutationFn: () => source.trigger(),
    onError: () => toast.error('Could not simulate a call'),
  })

  return (
    <Button
      type="button"
      variant="outline"
      onClick={() => trigger.mutate()}
      disabled={trigger.isPending}
    >
      <PhoneIncoming aria-hidden />
      {trigger.isPending ? 'Ringing…' : 'Simulate incoming call'}
    </Button>
  )
}
