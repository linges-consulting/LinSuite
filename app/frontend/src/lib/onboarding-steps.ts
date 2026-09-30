import type { OnboardingStepKey } from '@/lib/api'

/**
 * The seven checklist steps, in the fixed order both the checklist itself
 * (`components/onboarding-checklist.tsx`) and its focused step pages
 * (`routes/setup-checklist.tsx`, #117) render in. Sourced from the server's own order
 * (`settings/onboarding_routes.py::_steps`) — kept in sync by hand, the same posture
 * `lib/nav.ts`'s `NAV` and `lib/capability-gate.ts`'s `ADMIN_MODE_CAPABILITIES` already take
 * with their own backend-mirrored lists.
 *
 * A plain data module rather than living inside `onboarding-checklist.tsx`: a component file
 * that also exports a constant breaks Fast Refresh for every consumer of that constant.
 */
export const STEP_ORDER: OnboardingStepKey[] = [
  'business',
  'hours',
  'tax',
  'services',
  'staff',
  'email',
  'branding',
]

export const STEP_LABEL: Record<OnboardingStepKey, string> = {
  business: 'Business details',
  hours: 'Opening hours',
  tax: 'Tax',
  services: 'Services',
  staff: 'Staff',
  email: 'Email sending',
  branding: 'Branding',
}
