import { QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router'
import { toast } from 'sonner'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { Toaster } from '@/components/ui/sonner'
import type { FormField } from '@/lib/forms'
import { createQueryClient } from '@/lib/query-client'
import { ThemeProvider } from '@/lib/theme'
import { SettingsPage } from '@/routes/settings'

/**
 * Settings → Forms: the template list and the builder.
 *
 * Admin Mode cannot be entered from a test browser session, so this is where the builder's
 * behaviour is proved: keys minted once and kept, the "show only if" picker limited to
 * earlier yes/no and choice fields, the publish dialog's re-signature flag, and the live
 * preview hiding a conditional field until its condition holds. The fake is a small stateful
 * server, like `services.test.tsx`'s.
 */

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/

type Row = Record<string, any>

function template(fields: FormField[], overrides: Row = {}): Row {
  return {
    id: 't1',
    name: 'Prenatal intake',
    kind: 'intake',
    retired_at: null,
    updated_at: '2026-09-22T12:00:00Z',
    latest_version: null,
    has_unpublished_changes: fields.length > 0,
    draft: { schema: { fields }, is_health_form: true, is_mandatory: false },
    ...overrides,
  }
}

function fakeServer(seed: Row) {
  const templates: Row[] = [seed]
  const versions: Row[] = []
  const calls: { url: string; method: string; body?: any }[] = []

  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: RequestInit) => {
      const method = init?.method ?? 'GET'
      const body = init?.body ? JSON.parse(init.body as string) : undefined
      calls.push({ url, method, body })
      const row = templates.find((t) => t.id === url.split('/')[4])

      if (url === '/api/admin/forms' && method === 'GET') return Response.json({ templates })
      if (url.endsWith('/draft') && method === 'PUT') {
        const { schema, is_health_form, is_mandatory, ...rest } = body
        Object.assign(row!, rest, {
          draft: { schema, is_health_form, is_mandatory },
          has_unpublished_changes: true,
        })
        return Response.json(row)
      }
      if (url.endsWith('/publish')) {
        const number = versions.length + 1
        const version = {
          number,
          published_at: '2026-09-22T12:00:00Z',
          requires_resignature: body.requires_resignature,
          is_health_form: row!.draft.is_health_form,
          is_mandatory: row!.draft.is_mandatory,
        }
        versions.unshift(version)
        Object.assign(row!, { latest_version: number, has_unpublished_changes: false })
        return Response.json(version, { status: 201 })
      }
      if (url === '/api/admin/forms' && method === 'POST') {
        const created = template([], { ...body, id: 't2' })
        delete created.is_health_form
        delete created.is_mandatory
        created.draft.is_health_form = body.is_health_form
        created.draft.is_mandatory = body.is_mandatory
        templates.push(created)
        return Response.json(created, { status: 201 })
      }
      if (url.endsWith('/versions')) return Response.json({ versions })
      return Response.json({}, { status: 404 })
    }),
  )
  return { calls, templates }
}

function renderSettings() {
  return render(
    <ThemeProvider>
      <QueryClientProvider client={createQueryClient()}>
        <MemoryRouter>
          <SettingsPage />
        </MemoryRouter>
        <Toaster position="bottom-right" />
      </QueryClientProvider>
    </ThemeProvider>,
  )
}

async function openBuilder(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole('tab', { name: 'Forms' }))
  await user.click(await screen.findByRole('button', { name: 'Edit Prenatal intake' }))
  await screen.findByRole('button', { name: 'Save draft' })
}

const drafts = (calls: { method: string; url: string; body?: any }[]) =>
  calls.filter((c) => c.method === 'PUT' && c.url.endsWith('/draft'))

beforeEach(() => {
  vi.unstubAllGlobals()
  toast.dismiss()
})

describe('the builder', () => {
  it('mints a UUID for a new field and keeps it when the label is edited', async () => {
    const server = fakeServer(template([]))
    const user = userEvent.setup()
    renderSettings()
    await openBuilder(user)

    await user.click(screen.getByRole('button', { name: 'Add field' }))
    await user.click(await screen.findByRole('menuitem', { name: 'Short answer' }))
    await user.type(screen.getByLabelText('Question'), 'Full name')
    await user.click(screen.getByRole('button', { name: 'Save draft' }))

    await waitFor(() => expect(drafts(server.calls)).toHaveLength(1))
    const [first] = drafts(server.calls)[0].body.schema.fields
    expect(first.key).toMatch(UUID)
    expect(first.label).toBe('Full name')

    await user.clear(screen.getByLabelText('Question'))
    await user.type(screen.getByLabelText('Question'), 'Legal name')
    await user.click(screen.getByRole('button', { name: 'Save draft' }))

    await waitFor(() => expect(drafts(server.calls)).toHaveLength(2))
    const [second] = drafts(server.calls)[1].body.schema.fields
    expect(second).toMatchObject({ key: first.key, label: 'Legal name' })
  })

  it('offers only earlier yes/no and choice fields to "show only if"', async () => {
    fakeServer(
      template([
        { key: crypto.randomUUID(), type: 'short_text', label: 'Name', required: false },
        { key: crypto.randomUUID(), type: 'yes_no', label: 'Pregnant?', required: true },
        {
          key: crypto.randomUUID(),
          type: 'single_choice',
          label: 'Contact by',
          required: false,
          options: ['Email', 'Phone'],
        },
        { key: crypto.randomUUID(), type: 'short_text', label: 'Weeks', required: false },
        { key: crypto.randomUUID(), type: 'multi_choice', label: 'Areas', required: false, options: ['Back'] },
      ]),
    )
    const user = userEvent.setup()
    renderSettings()
    await openBuilder(user)

    await user.click(screen.getByRole('combobox', { name: 'Show field 4 only if' }))
    const offered = (await screen.findAllByRole('option')).map((o) => o.textContent)
    expect(offered).toEqual(['Always show', 'Pregnant?', 'Contact by'])
  })

  it('sends requires_resignature when publishing', async () => {
    const server = fakeServer(
      // Version 1 exists: re-signing only means something from version 2 on.
      template(
        [{ key: crypto.randomUUID(), type: 'yes_no', label: 'Any allergies?', required: true }],
        { latest_version: 1 },
      ),
    )
    const user = userEvent.setup()
    renderSettings()
    await openBuilder(user)

    await user.click(screen.getByRole('button', { name: 'Publish…' }))
    const dialog = await screen.findByRole('dialog')
    await user.click(within(dialog).getByRole('checkbox', { name: /sign this version again/i }))
    await user.click(within(dialog).getByRole('button', { name: 'Publish' }))

    await waitFor(() => {
      const publish = server.calls.find((c) => c.url.endsWith('/publish'))
      expect(publish?.body).toEqual({ requires_resignature: true })
    })
    // The draft on screen is what gets published: it is saved first.
    const order = server.calls.filter((c) => c.method !== 'GET').map((c) => c.url.split('/').pop())
    expect(order).toEqual(['draft', 'publish'])
  })

  it('hides a conditional field in the preview until its condition holds', async () => {
    const pregnant = crypto.randomUUID()
    fakeServer(
      template([
        { key: pregnant, type: 'yes_no', label: 'Are you pregnant?', required: true },
        {
          key: crypto.randomUUID(),
          type: 'short_text',
          label: 'How many weeks?',
          required: true,
          show_if: { key: pregnant, equals: ['yes'] },
        },
      ]),
    )
    const user = userEvent.setup()
    renderSettings()
    await openBuilder(user)

    const preview = screen.getByRole('region', { name: 'Preview' })
    expect(within(preview).getByText('Are you pregnant?')).toBeInTheDocument()
    expect(within(preview).queryByText('How many weeks?')).not.toBeInTheDocument()

    await user.click(within(preview).getByRole('radio', { name: 'Yes' }))
    expect(within(preview).getByText('How many weeks?')).toBeInTheDocument()

    await user.click(within(preview).getByRole('radio', { name: 'No' }))
    expect(within(preview).queryByText('How many weeks?')).not.toBeInTheDocument()
  })
})

describe('the list', () => {
  it('creates a form with the health and mandatory flags and opens it', async () => {
    const server = fakeServer(template([]))
    const user = userEvent.setup()
    renderSettings()
    await user.click(await screen.findByRole('tab', { name: 'Forms' }))

    await user.click(await screen.findByRole('button', { name: 'New form' }))
    const dialog = await screen.findByRole('dialog')
    await user.type(within(dialog).getByLabelText('Name'), 'Massage waiver')
    await user.click(within(dialog).getByRole('checkbox', { name: 'Health form' }))
    await user.click(within(dialog).getByRole('checkbox', { name: 'Mandatory (essential form)' }))
    await user.click(within(dialog).getByRole('button', { name: 'Create form' }))

    await waitFor(() => {
      const post = server.calls.find((c) => c.method === 'POST')
      expect(post?.body).toEqual({
        name: 'Massage waiver',
        kind: 'intake',
        is_health_form: true,
        is_mandatory: true,
      })
    })
    expect(await screen.findByRole('heading', { name: 'Massage waiver' })).toBeInTheDocument()
  })

  it('says what state each form is in', async () => {
    fakeServer(template([], { latest_version: 2, retired_at: '2026-09-22T12:00:00Z' }))
    const user = userEvent.setup()
    renderSettings()
    await user.click(await screen.findByRole('tab', { name: 'Forms' }))

    const row = (await screen.findByText('Prenatal intake')).closest('tr')!
    expect(within(row).getByText('Intake')).toBeInTheDocument()
    expect(within(row).getByText('v2')).toBeInTheDocument()
    expect(within(row).getByText('Retired')).toBeInTheDocument()
  })
})
