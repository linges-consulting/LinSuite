import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, expect, test, vi } from 'vitest'
import { SessionNotesCard } from '@/components/session-notes'
import { choose } from './choose'

afterEach(() => vi.unstubAllGlobals())
const template = {
  id: 't1',
  name: 'SOAP',
  active: true,
  fields: [{ key: 'subjective', label: 'Subjective', required: true }],
  diagram_ids: ['body_front', 'layout'],
}

function api() {
  let note: Record<string, unknown> | null = null
  const fetcher = vi.fn(async (url: string, init?: RequestInit) => {
    const body = init?.body ? JSON.parse(String(init.body)) : null
    if (url === '/api/note-templates') return Response.json([template])
    if (url.endsWith('/session-note-appointments'))
      return Response.json([
        {
          id: 'a1',
          service_name: 'Massage',
          starts_at: '2026-09-27T14:00:00Z',
        },
      ])
    if (init?.method === 'POST' && url.endsWith('/session-notes')) {
      note = {
        id: 'n1',
        template_id: 't1',
        appointment_id: 'a1',
        author_staff_id: 's1',
        author_name: 'Rae',
        template,
        revision: 1,
        created_at: '2026-09-27T15:00:00Z',
        updated_at: '2026-09-27T15:00:00Z',
        locked_at: null,
        can_edit: true,
        ...body,
      }
      return Response.json(note, { status: 201 })
    }
    if (url.endsWith('/lock')) {
      note = {
        ...note,
        locked_at: '2026-09-27T16:00:00Z',
        revision: 2,
        can_edit: false,
      }
      return Response.json(note)
    }
    if (url.endsWith('/n1')) return Response.json(note)
    if (url.endsWith('/session-notes')) return Response.json(note ? [note] : [])
    throw new Error(`Unexpected request ${url}`)
  })
  vi.stubGlobal('fetch', fetcher)
  return fetcher
}

function card(canWrite = true) {
  return render(
    <QueryClientProvider
      client={
        new QueryClient({ defaultOptions: { queries: { retry: false } } })
      }
    >
      <SessionNotesCard
        customerId="c1"
        timezone="America/Toronto"
        suppressed={false}
        canWrite={canWrite}
      />
    </QueryClientProvider>,
  )
}

test('practitioner saves an appointment note and reopens it to lock it', async () => {
  const calls = api()
  const user = userEvent.setup()
  card()
  await user.click(
    await screen.findByRole('button', { name: 'New session note' }),
  )
  await choose(user, 'Appointment', /Massage$/)
  await choose(user, 'Note template', 'SOAP')
  await user.type(screen.getByLabelText('Subjective *'), 'Shoulder feels stiff')
  await user.click(screen.getByRole('button', { name: 'Save draft' }))
  await user.click(await screen.findByRole('button', { name: 'Open SOAP' }))
  expect(await screen.findByLabelText('Subjective *')).toHaveValue(
    'Shoulder feels stiff',
  )
  await user.click(screen.getByRole('button', { name: 'Lock note' }))
  await user.click(screen.getByRole('button', { name: 'Confirm lock' }))
  await waitFor(() =>
    expect(screen.getByLabelText('Subjective *')).toBeDisabled(),
  )
  expect(calls.mock.calls.filter(([url]) => url.endsWith('/n1'))).toHaveLength(
    1,
  )
  await user.click(screen.getAllByRole('button', { name: /^Close$/ })[0])
  await user.click(await screen.findByRole('button', { name: 'Open SOAP' }))
  expect(await screen.findByLabelText('Subjective *')).toBeDisabled()
  expect(calls.mock.calls.filter(([url]) => url.endsWith('/n1'))).toHaveLength(
    2,
  )
})

test('diagram markup is entered with a keyboard and survives saving and reopening', async () => {
  const calls = api()
  const user = userEvent.setup()
  card()
  await user.click(
    await screen.findByRole('button', { name: 'New session note' }),
  )
  await choose(user, 'Appointment', /Massage$/)
  await choose(user, 'Note template', 'SOAP')
  await choose(user, 'Diagram', 'Floor / project layout')
  await choose(user, 'Annotation tool', 'Shaded zone')
  await user.type(screen.getByLabelText('Annotation text'), 'Repair area')
  await user.click(screen.getByRole('button', { name: 'Add annotation' }))
  expect(screen.getByText('1. Repair area')).toBeVisible()
  await user.click(screen.getByRole('button', { name: 'Save draft' }))
  await user.click(await screen.findByRole('button', { name: 'Open SOAP' }))
  expect(await screen.findByText('1. Repair area')).toBeVisible()
  const post = calls.mock.calls.find(
    ([url, init]) => url.endsWith('/session-notes') && init?.method === 'POST',
  )!
  const body = JSON.parse(String(post[1]!.body))
  expect(body.annotations[0]).toMatchObject({
    kind: 'zone',
    diagram_id: 'layout',
    x: 0.5,
    y: 0.5,
    width: 0.2,
    height: 0.15,
    colour: '#DC2626',
    text: 'Repair area',
  })
  expect(Number.isFinite(Date.parse(body.annotations[0].timestamp))).toBe(true)
})
