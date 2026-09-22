import { render, screen } from '@testing-library/react'
import { expect, test } from 'vitest'
import { ClassificationBadge } from '@/components/classification-badge'
import type { Classification } from '@/lib/api'

/** The one place `new` / `repeat` / `vip` becomes a word on screen — pinned for all three,
 *  since the list, the profile and the booking dialog's search all lean on this rendering
 *  the right word rather than only a colour. */
test.each([
  ['new', 'New'],
  ['repeat', 'Repeat'],
  ['vip', 'VIP'],
] satisfies [Classification, string][])('%s renders as %s', (classification, word) => {
  render(<ClassificationBadge classification={classification} />)

  expect(screen.getByText(word)).toBeInTheDocument()
})
