import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, expect, test, vi } from 'vitest'
import { NoteTemplatesPanel } from '@/routes/settings-notes'
import { choose } from './choose'

afterEach(() => vi.unstubAllGlobals())

test('administrator configures a general log template and retires it', async () => {
  let templates: Record<string, unknown>[] = []
  const calls = vi.fn(async (url: string, init?: RequestInit) => {
    if (url === '/api/admin/note-templates') return Response.json(templates)
    const body = JSON.parse(String(init?.body))
    templates = [{ id: 't1', ...body }]
    return Response.json(templates[0], {
      status: init?.method === 'POST' ? 201 : 200,
    })
  })
  vi.stubGlobal('fetch', calls)
  const user = userEvent.setup()
  render(
    <QueryClientProvider
      client={
        new QueryClient({ defaultOptions: { queries: { retry: false } } })
      }
    >
      <NoteTemplatesPanel />
    </QueryClientProvider>,
  )
  await user.click(
    await screen.findByRole('button', { name: 'New note template' }),
  )
  await choose(user, 'Starting point', 'General session log')
  await user.click(screen.getByRole('button', { name: 'Save template' }))
  expect(await screen.findByText('General session log')).toBeVisible()
  await user.click(
    screen.getByRole('button', { name: 'Retire General session log' }),
  )
  expect(
    calls.mock.calls.filter(([, init]) => init?.method === 'PUT'),
  ).toHaveLength(0)
  await user.click(screen.getByRole('button', { name: 'Confirm retirement' }))
  expect(await screen.findByText('Retired')).toBeVisible()
  const saved = JSON.parse(
    String(
      calls.mock.calls.find(([, init]) => init?.method === 'POST')![1]!.body,
    ),
  )
  expect(saved.fields.map((f: { label: string }) => f.label)).toEqual([
    'Session summary',
    'Actions taken',
    'Next steps',
  ])
  expect(saved.diagram_ids).toEqual(['layout'])
})
