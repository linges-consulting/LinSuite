import { CircleCheck, Ellipsis, Link2, UserX, XCircle } from 'lucide-react'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import type { Appointment } from '@/lib/api'

/**
 * The card's own actions (Task 18): Complete, No-show and Cancel as `a.status` allows — all
 * three terminal, so the menu is nothing once it is one of them — plus "Cancel visit" for a
 * grouped appointment, which acts on the whole booking group rather than this one link.
 *
 * A small trigger in the card's corner, shown on hover or keyboard focus (`group-hover` on
 * `EventCard`'s wrapper); `onPointerDown` stops the card's own drag from picking up when the
 * trigger is what was actually pressed.
 */
export function StatusMenu(props: {
  appointment: Appointment
  onComplete: () => void
  onCancel: () => void
  onNoShow: () => void
  onCancelGroup?: () => void
}) {
  const { appointment: a } = props
  if (a.status !== 'confirmed') return null
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button
          type="button"
          aria-label={`Actions for ${a.customer.first_name} ${a.customer.last_name}`}
          data-testid={`menu-${a.id}`}
          className="absolute top-0.5 left-0.5 z-10 rounded-sm bg-black/10 p-0.5 opacity-0 outline-none focus-visible:opacity-100 focus-visible:ring-2 focus-visible:ring-ring group-hover:opacity-100 group-focus-within:opacity-100 hover:bg-black/20"
          onPointerDown={(e) => e.stopPropagation()}
          onClick={(e) => e.stopPropagation()}
        >
          <Ellipsis className="size-3.5" aria-hidden />
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" onPointerDown={(e) => e.stopPropagation()}>
        <DropdownMenuItem onSelect={props.onComplete}>
          <CircleCheck aria-hidden /> Complete
        </DropdownMenuItem>
        <DropdownMenuItem onSelect={props.onNoShow}>
          <UserX aria-hidden /> No-show
        </DropdownMenuItem>
        <DropdownMenuSeparator />
        <DropdownMenuItem variant="destructive" onSelect={props.onCancel}>
          <XCircle aria-hidden /> Cancel
        </DropdownMenuItem>
        {props.onCancelGroup && (
          <DropdownMenuItem variant="destructive" onSelect={props.onCancelGroup}>
            <Link2 aria-hidden /> Cancel visit
          </DropdownMenuItem>
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
