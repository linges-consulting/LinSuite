import { QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { afterEach, expect, test, vi } from 'vitest'
import { Toaster } from '@/components/ui/sonner'
import type { Customer } from '@/lib/api'
import { createQueryClient } from '@/lib/query-client'
import { ThemeProvider } from '@/lib/theme'
import { SchedulePage } from '@/routes/schedule'
import { fakeServer } from './schedule-lifecycle-harness'

/**
 * The quick-book shortcut (Phase 14, #16 decision #2): a customer handed to `/schedule`
 * through router state — exactly what the phone-lookup page's and the screen-pop panel's
 * "Book appointment" button send (`components/phone-lookup-result.tsx`) — opens the "New
 * appointment" dialog with that customer already selected, no second search.
 */

afterEach(() => vi.unstubAllGlobals())

const QUICK_BOOK_CUSTOMER: Customer = {
  id: 'c9',
  first_name: 'Priya',
  last_name: 'Nair',
  email: null,
  phone: '4165550199',
  classification: 'repeat',
}

function renderScheduleWithQuickBookState(customer: Customer | null) {
  return render(
    <ThemeProvider>
      <QueryClientProvider client={createQueryClient()}>
        <MemoryRouter
          initialEntries={[
            { pathname: '/schedule', state: customer ? { quickBookCustomer: customer } : null },
          ]}
        >
          <SchedulePage />
        </MemoryRouter>
        <Toaster position="bottom-right" />
      </QueryClientProvider>
    </ThemeProvider>,
  )
}

test('a quick-book customer opens the New appointment dialog already selected', async () => {
  fakeServer([])

  renderScheduleWithQuickBookState(QUICK_BOOK_CUSTOMER)

  const dialog = await screen.findByRole('dialog', { name: 'New appointment' })
  expect(dialog).toHaveTextContent('Priya Nair')
  // Preselected, not the search box — "Change" is the chip's own way out, and only appears
  // once a customer is already picked.
  expect(screen.getByRole('button', { name: 'Change' })).toBeInTheDocument()
  expect(screen.queryByLabelText('Find a client')).not.toBeInTheDocument()
})

test('with no quick-book state, the schedule screen opens with no dialog at all', async () => {
  fakeServer([])

  renderScheduleWithQuickBookState(null)

  await screen.findByRole('button', { name: 'New appointment' })
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
})
