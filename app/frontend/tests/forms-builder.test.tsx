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
    applies_to_all: false,
    valid_for_months: null,
    service_ids: [],
    ...overrides,
  }
}

function fakeServer(seed: Row, history: Row[] = [], services: Row[] = []) {
  const templates: Row[] = [seed]
  const versions: Row[] = [...history]
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
      if (url.endsWith('/settings') && method === 'PUT') {
        Object.assign(row!, body)
        return Response.json(row)
      }
      if (url.endsWith('/retire') && method === 'POST') {
        Object.assign(row!, { retired_at: '2026-09-22T13:00:00Z' })
        return Response.json(row)
      }
      if (url.endsWith('/publish')) {
        const number = versions.length + 1
        const version = {
          number,
          name: row!.name,
          kind: row!.kind,
          schema: row!.draft.schema,
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
      if (/\/versions\/\d+$/.test(url)) {
        const number = Number(url.split('/').pop())
        const version = versions.find((v) => v.number === number)
        return version ? Response.json(version) : Response.json({}, { status: 404 })
      }
      if (url.endsWith('/versions')) return Response.json({ versions })
      if (url === '/api/admin/services') return Response.json({ services })
      return Response.json({}, { status: 404 })
    }),
  )
  return { calls, templates, versions }
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

describe('the builder, continued', () => {
  it('never offers "show only if" on a signature block', async () => {
    fakeServer(
      template([
        { key: crypto.randomUUID(), type: 'yes_no', label: 'Agree?', required: true },
        { key: crypto.randomUUID(), type: 'signature', label: 'Signature', required: true },
      ]),
    )
    const user = userEvent.setup()
    renderSettings()
    await openBuilder(user)

    expect(screen.queryByRole('combobox', { name: 'Show field 2 only if' })).not.toBeInTheDocument()
  })

  it('shows each version under the title it was published with', async () => {
    fakeServer(
      template([{ key: crypto.randomUUID(), type: 'yes_no', label: 'Any?', required: true }], {
        latest_version: 1,
      }),
      [
        {
          number: 1,
          name: 'Old prenatal intake',
          kind: 'intake',
          published_at: '2026-09-21T12:00:00Z',
          requires_resignature: false,
          is_health_form: true,
          is_mandatory: false,
        },
      ],
    )
    const user = userEvent.setup()
    renderSettings()
    await openBuilder(user)

    const history = screen.getByRole('region', { name: 'Versions' })
    expect(await within(history).findByText('Old prenatal intake')).toBeInTheDocument()
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

// --- fix: Publish offers nothing to publish when the draft matches what is already out -------

describe('the publish gate', () => {
  it('disables Publish with a hint while the draft matches the latest version, and re-enables it on a real change', async () => {
    const key = crypto.randomUUID()
    const fields = [{ key, type: 'yes_no', label: 'Any allergies?', required: true }]
    fakeServer(template(fields, { latest_version: 1, has_unpublished_changes: false }), [
      {
        number: 1,
        name: 'Prenatal intake',
        kind: 'intake',
        schema: { fields },
        published_at: '2026-09-21T12:00:00Z',
        requires_resignature: false,
        is_health_form: true,
        is_mandatory: false,
      },
    ])
    const user = userEvent.setup()
    renderSettings()
    await openBuilder(user)

    const publishButton = await screen.findByRole('button', { name: 'Publish…' })
    await waitFor(() => expect(publishButton).toBeDisabled())
    expect(publishButton).toHaveAttribute('title', expect.stringContaining('Nothing has changed'))
    expect(screen.getByText('Nothing to publish')).toBeInTheDocument()

    await user.clear(screen.getByLabelText('Question'))
    await user.type(screen.getByLabelText('Question'), 'Any allergies at all?')

    expect(publishButton).toBeEnabled()
    expect(screen.queryByText('Nothing to publish')).not.toBeInTheDocument()
  })

  it('still shows the server’s message if a 409 draft_unchanged reaches it anyway (a race, not a crash)', async () => {
    fakeServer(template([{ key: crypto.randomUUID(), type: 'yes_no', label: 'Any?', required: true }]))
    // Simulate the server refusing even though nothing locally marked Publish as offering
    // nothing — the case the gate cannot see coming, such as another tab publishing first.
    const fetchStub = vi.mocked(fetch)
    const passthrough = fetchStub.getMockImplementation()!
    fetchStub.mockImplementation(async (url, init) => {
      if (String(url).endsWith('/publish')) {
        return Response.json(
          { detail: 'Nothing has changed since version 1.', code: 'draft_unchanged' },
          { status: 409 },
        )
      }
      return passthrough(url, init)
    })
    const user = userEvent.setup()
    renderSettings()
    await openBuilder(user)

    await user.click(screen.getByRole('button', { name: 'Publish…' }))
    const dialog = await screen.findByRole('dialog')
    await user.click(within(dialog).getByRole('button', { name: 'Publish' }))

    expect(await screen.findByText('Nothing has changed since version 1.')).toBeInTheDocument()
  })
})

// --- fix: retiring asks in the app's own dialog, not window.confirm ---------------------------

describe('retiring', () => {
  it('asks in a dialog stating the consequence, never window.confirm', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm')
    const server = fakeServer(template([], { latest_version: 1 }))
    const user = userEvent.setup()
    renderSettings()
    await user.click(await screen.findByRole('tab', { name: 'Forms' }))

    await user.click(await screen.findByRole('button', { name: 'Actions for Prenatal intake' }))
    await user.click(await screen.findByRole('menuitem', { name: 'Retire' }))

    const dialog = await screen.findByRole('dialog', { name: /Retire Prenatal intake/ })
    expect(within(dialog).getByText(/no new links can be sent/i)).toBeInTheDocument()
    expect(within(dialog).getByText(/revoked/i)).toBeInTheDocument()
    expect(confirmSpy).not.toHaveBeenCalled()

    await user.click(within(dialog).getByRole('button', { name: 'Retire' }))

    await waitFor(() => {
      expect(server.calls.some((c) => c.url.endsWith('/retire') && c.method === 'POST')).toBe(true)
    })
    expect(await screen.findByText('Retired Prenatal intake')).toBeInTheDocument()
  })

  it('cancelling the dialog retires nothing', async () => {
    const server = fakeServer(template([], { latest_version: 1 }))
    const user = userEvent.setup()
    renderSettings()
    await user.click(await screen.findByRole('tab', { name: 'Forms' }))
    await user.click(await screen.findByRole('button', { name: 'Actions for Prenatal intake' }))
    await user.click(await screen.findByRole('menuitem', { name: 'Retire' }))

    const dialog = await screen.findByRole('dialog', { name: /Retire Prenatal intake/ })
    await user.click(within(dialog).getByRole('button', { name: 'Cancel' }))

    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(server.calls.some((c) => c.url.endsWith('/retire'))).toBe(false)
  })
})

// --- Task 8: the essential-forms checklist settings panel --------------------------------------

describe('the essential-forms checklist settings', () => {
  it('saves who it applies to and how long a submission stays valid', async () => {
    const server = fakeServer(template([]), [], [
      { id: 'svc1', name: 'Massage', active: true },
      { id: 'svc2', name: 'Facial', active: true },
    ])
    const user = userEvent.setup()
    renderSettings()
    await openBuilder(user)

    await user.click(await screen.findByRole('radio', { name: 'Clients booked for these services' }))
    await user.click(await screen.findByRole('checkbox', { name: 'Massage' }))
    await user.type(screen.getByLabelText('Valid for (months)'), '12')
    await user.click(screen.getByRole('button', { name: 'Save settings' }))

    await waitFor(() => {
      const saved = server.calls.find((c) => c.url.endsWith('/settings'))
      expect(saved?.body).toEqual({
        applies_to_all: false,
        valid_for_months: 12,
        service_ids: ['svc1'],
      })
    })
  })

  it('rejects a period outside 1–120 months before it is ever sent', async () => {
    fakeServer(template([]))
    const user = userEvent.setup()
    renderSettings()
    await openBuilder(user)

    await user.type(screen.getByLabelText('Valid for (months)'), '121')

    expect(screen.getByRole('button', { name: 'Save settings' })).toBeDisabled()
    expect(screen.getByText('Between 1 and 120 months.')).toBeInTheDocument()
  })
})
