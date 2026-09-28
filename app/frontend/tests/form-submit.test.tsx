import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { renderApp, stubApi, BUSINESS_NAME } from './harness'

/**
 * Submitting a form (#47). The client side: `/f/#<token>` gains a signature pad (drawn, plus
 * a typed full name; a keyboard alternative signs with the typed name), sends the answers
 * with a submission id minted once and kept for every retry, puts a 422's per-field codes
 * under their fields, keeps everything on a network failure, and ends on a confirmation that
 * shows none of the answers. The staff side: the profile's Forms card lists what was
 * submitted and opens one with its own version's labels.
 */

const PREGNANT = '11111111-1111-4111-8111-111111111111'
const WEEKS = '22222222-2222-4222-8222-222222222222'
const SIGNATURE = '33333333-3333-4333-8333-333333333333'
const TOKEN = 'k'.repeat(43)
const DRAWN = 'data:image/png;base64,DRAWN'

const PUBLIC_FORM = {
  version_id: 'v1',
  template_name: 'Prenatal intake',
  schema: {
    fields: [
      { key: PREGNANT, type: 'yes_no', label: 'Are you pregnant?', required: true },
      {
        key: WEEKS,
        type: 'short_text',
        label: 'How many weeks?',
        required: true,
        show_if: { key: PREGNANT, equals: ['yes'] },
      },
      { key: SIGNATURE, type: 'signature', label: 'Signature', required: true },
    ],
  },
  business: { name: BUSINESS_NAME, logo_url: null },
  client_first_name: 'Priya',
  expires_at: '2026-09-24T12:00:00Z',
}

// jsdom has no canvas: a 2D context that records nothing, and a fixed PNG for what was drawn.
const drawn = vi.fn(() => DRAWN)
beforeEach(() => {
  const context = new Proxy({}, { get: () => () => {}, set: () => true })
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(
    context as unknown as CanvasRenderingContext2D,
  )
  vi.spyOn(HTMLCanvasElement.prototype, 'toDataURL').mockImplementation(drawn)
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

type Submit = { status: number; body: unknown } | 'network'

/** The public page, with the submit POST answered from `answers` in turn. */
function openForm(...answers: Submit[]) {
  const sent: Record<string, unknown>[] = []
  stubApi({
    signedIn: false,
    respond: (url: string) => (url === '/api/public/forms/lookup' ? Response.json(PUBLIC_FORM) : undefined),
  })
  const fetchStub = vi.mocked(fetch)
  const passthrough = fetchStub.getMockImplementation()!
  fetchStub.mockImplementation(async (url, init) => {
    if (url === '/api/public/forms/submit') {
      sent.push(JSON.parse(String(init!.body)))
      const answer = answers.shift() ?? { status: 200, body: { status: 'received' } }
      if (answer === 'network') throw new TypeError('Failed to fetch')
      return Response.json(answer.body, { status: answer.status })
    }
    return passthrough(url, init)
  })
  window.history.replaceState(null, '', `/f/#${TOKEN}`)
  renderApp(`/f/#${TOKEN}`)
  return sent
}

async function draw() {
  const pad = screen.getByRole('img', { name: /signature pad/i })
  fireEvent.pointerDown(pad, { clientX: 10, clientY: 10, pointerId: 1 })
  fireEvent.pointerMove(pad, { clientX: 60, clientY: 40, pointerId: 1 })
  fireEvent.pointerUp(pad, { clientX: 60, clientY: 40, pointerId: 1 })
}

async function fillIn(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole('radio', { name: 'Yes' }))
  await user.type(screen.getByLabelText(/How many weeks/), 'twelve weeks')
  await user.type(screen.getByRole('textbox', { name: 'Full name' }), 'Priya Nair')
  await draw()
}

test('the signature pad marks the form incomplete until drawn and named', async () => {
  const sent = openForm()
  const user = userEvent.setup()
  await user.click(await screen.findByRole('radio', { name: 'No' }))

  await user.click(screen.getByRole('button', { name: 'Submit' }))
  const name = screen.getByRole('textbox', { name: 'Full name' })
  expect(name).toHaveAccessibleDescription('Draw your signature and type your full name.')
  expect(sent).toHaveLength(0)

  await user.type(name, 'Priya Nair')
  await user.click(screen.getByRole('button', { name: 'Submit' }))
  expect(name).toHaveAccessibleDescription('Draw your signature and type your full name.')
  expect(sent).toHaveLength(0)

  await draw()
  await user.click(screen.getByRole('button', { name: 'Submit' }))
  await waitFor(() => expect(sent).toHaveLength(1))
  expect(sent[0]).toMatchObject({
    token: TOKEN,
    version_id: 'v1',
    answers: { [PREGNANT]: 'no', [SIGNATURE]: { name: 'Priya Nair', image: DRAWN } },
  })
  expect(sent[0].submission_id).toMatch(/^[0-9a-f-]{36}$/)
})

test('without a pointer, the typed name can be adopted as the signature, and Clear undoes it', async () => {
  const sent = openForm()
  const user = userEvent.setup()
  await user.click(await screen.findByRole('radio', { name: 'No' }))
  await user.type(screen.getByRole('textbox', { name: 'Full name' }), 'Priya Nair')

  await user.click(screen.getByRole('button', { name: 'Sign with my typed name' }))
  await user.click(screen.getByRole('button', { name: 'Clear signature' }))
  await user.click(screen.getByRole('button', { name: 'Submit' }))
  expect(sent).toHaveLength(0)

  await user.click(screen.getByRole('button', { name: 'Sign with my typed name' }))
  await user.click(screen.getByRole('button', { name: 'Submit' }))
  await waitFor(() => expect(sent).toHaveLength(1))
  expect(sent[0].answers).toMatchObject({ [SIGNATURE]: { name: 'Priya Nair', image: DRAWN } })
})

test('a 422 puts each code under its own field, mapping a signature the server refused to the specific hint', async () => {
  openForm({
    status: 422,
    body: {
      detail: 'Some answers need attention.',
      code: 'invalid_answers',
      errors: { [WEEKS]: 'required', [SIGNATURE]: 'invalid' },
    },
  })
  const user = userEvent.setup()
  await fillIn(user)

  await user.click(screen.getByRole('button', { name: 'Submit' }))

  await waitFor(() =>
    expect(screen.getByLabelText(/How many weeks/)).toHaveAccessibleDescription('This is required.'),
  )
  // The pad was drawn and named (`fillIn`), so an `invalid` from the server can only be its
  // own stricter ink check — the same hint the client-side one gives for the same reason.
  expect(screen.getByRole('textbox', { name: 'Full name' })).toHaveAccessibleDescription(
    'Your signature is too small — please sign again.',
  )
  expect(screen.getByRole('radio', { name: 'Yes' })).not.toHaveAccessibleDescription()
})

test('an incomplete signature refused as invalid still gets the general prompt, not the ink hint', async () => {
  // A server that (implausibly) marks a half-filled signature `invalid` rather than
  // `required`: `message()` only shows the specific hint when both fields are actually
  // filled in, so this edge case reads as "finish it", not "it was too small".
  openForm({
    status: 422,
    body: {
      detail: 'Some answers need attention.',
      code: 'invalid_answers',
      errors: { [SIGNATURE]: 'invalid' },
    },
  })
  const user = userEvent.setup()
  await user.click(await screen.findByRole('radio', { name: 'No' }))
  await user.type(screen.getByRole('textbox', { name: 'Full name' }), 'Priya Nair')

  await user.click(screen.getByRole('button', { name: 'Submit' }))

  await waitFor(() =>
    expect(screen.getByRole('textbox', { name: 'Full name' })).toHaveAccessibleDescription(
      'Draw your signature and type your full name.',
    ),
  )
})

test('a network failure keeps the answers, and the retry sends the same submission id', async () => {
  const sent = openForm('network', { status: 200, body: { status: 'already_received' } })
  const user = userEvent.setup()
  await fillIn(user)

  await user.click(screen.getByRole('button', { name: 'Submit' }))
  expect(await screen.findByRole('alert')).toHaveTextContent(/could not be sent/i)
  expect(screen.getByLabelText(/How many weeks/)).toHaveValue('twelve weeks')
  expect(screen.getByRole('textbox', { name: 'Full name' })).toHaveValue('Priya Nair')

  await user.click(screen.getByRole('button', { name: 'Submit' }))
  expect(await screen.findByRole('heading', { name: /thank you/i })).toBeInTheDocument()
  expect(sent).toHaveLength(2)
  expect(sent[1].submission_id).toBe(sent[0].submission_id)
  expect(sent[1].answers).toEqual(sent[0].answers)
})

test('the confirmation names the business and shows none of the answers', async () => {
  openForm()
  const user = userEvent.setup()
  await fillIn(user)

  await user.click(screen.getByRole('button', { name: 'Submit' }))

  expect(await screen.findByRole('heading', { name: /thank you/i })).toBeInTheDocument()
  expect(screen.getByText(`${BUSINESS_NAME} has received your form.`)).toBeInTheDocument()
  expect(document.body).not.toHaveTextContent('twelve weeks')
  expect(document.body).not.toHaveTextContent('Priya Nair')
  expect(screen.queryByRole('img', { name: /signature/i })).not.toBeInTheDocument()
  expect(screen.queryByRole('textbox')).not.toBeInTheDocument()
})

test('a link that died meanwhile says so', async () => {
  openForm({ status: 404, body: { detail: 'This link is no longer valid.', code: 'link_invalid' } })
  const user = userEvent.setup()
  await fillIn(user)

  await user.click(screen.getByRole('button', { name: 'Submit' }))

  expect(await screen.findByRole('heading', { name: 'This link is no longer valid' })).toBeInTheDocument()
})

// --- staff: the profile's Forms card ----------------------------------------------------------

const CUSTOMER = {
  id: 'c1',
  first_name: 'Priya',
  last_name: 'Nair',
  email: null,
  phone: '4165550199',
  created_at: '2026-01-05T15:00:00Z',
  classification: 'new',
  date_of_birth: null,
  emergency_contact_name: null,
  emergency_contact_phone: null,
  emergency_contact_relationship: null,
  secondary_contact_name: null,
  secondary_contact_phone: null,
  secondary_contact_email: null,
  notes: null,
  updated_at: '2026-01-05T15:00:00Z',
  retention: { status: 'not_held', expires_on: null },
  suppressed: false,
  erasure: null,
}

function reader(capabilities: string[], ready: () => boolean = () => true) {
  return stubApi({
    signedIn: true,
    dualRole: false,
    respond: (url: string) => {
      const path = new URL(url, 'http://test').pathname
      if (url === '/api/auth/me') {
        return Response.json({
          id: 'u2',
          email: 'desk@cedar.example',
          role: 'Staff',
          capabilities,
          mode: 'staff',
          can_switch_modes: false,
          admin_grant_expires_at: null,
          admin_hard_limit_at: null,
          must_change_password: false,
          mfa: {
            enrolled: false,
            method: null,
            pending: false,
            enrolment_required: false,
            verified_at: null,
            email_otp_allowed: false,
          },
        })
      }
      if (path === '/api/customers/c1') {
        return Response.json({
          customer: CUSTOMER,
          timezone: 'America/Toronto',
          appointments: [],
          notification_failures: [],
        })
      }
      if (path === '/api/customers/c1/form-links') return Response.json({ links: [] })
      if (path === '/api/customers/c1/forms') {
        return Response.json({
          submissions: [
            {
              id: 's1',
              template_id: 't1',
              template_name: 'Prenatal intake',
              version: 1,
              method: 'link',
              submitted_at: '2026-09-22T14:00:00Z',
              pdf_ready: ready(),
            },
          ],
        })
      }
      if (path === '/api/customers/c1/forms/s1') {
        return Response.json({
          id: 's1',
          template_id: 't1',
          template_name: 'Prenatal intake',
          version: 1,
          method: 'link',
          submitted_at: '2026-09-22T14:00:00Z',
          fields: [
            { key: PREGNANT, type: 'yes_no', label: 'Are you pregnant?' },
            { key: WEEKS, type: 'short_text', label: 'How many weeks?' },
            { key: SIGNATURE, type: 'signature', label: 'Signature' },
          ],
          answers: { [PREGNANT]: 'yes', [WEEKS]: 'twelve', [SIGNATURE]: { name: 'Priya Nair', image: DRAWN } },
        })
      }
      return undefined
    },
  })
}

test('the Forms card lists submissions, and opening one shows its own version’s labels', async () => {
  const { calls } = reader(['customers.view', 'forms.view'])
  const user = userEvent.setup()
  renderApp('/clients/c1')

  const card = (await screen.findByRole('heading', { name: 'Forms' })).closest('[data-slot="card"]') as HTMLElement
  const table = await within(card).findByRole('table', { name: 'Completed forms' })
  expect(within(table).getByText('Prenatal intake')).toBeInTheDocument()
  // No answers are fetched until one is opened: the list is metadata.
  expect(calls.some((c) => c.url.endsWith('/forms/s1'))).toBe(false)
  // `forms.view` alone: nothing to send with.
  expect(within(card).queryByRole('button', { name: 'Send form' })).not.toBeInTheDocument()

  await user.click(within(table).getByRole('button', { name: 'Open Prenatal intake' }))
  const dialog = await screen.findByRole('dialog', { name: /Prenatal intake/ })
  expect(await within(dialog).findByText('Are you pregnant?')).toBeInTheDocument()
  expect(within(dialog).getByText('Yes')).toBeInTheDocument()
  expect(within(dialog).getByText('twelve')).toBeInTheDocument()
  expect(within(dialog).getByRole('img', { name: 'Signature of Priya Nair' })).toHaveAttribute('src', DRAWN)
})

test('without forms.view the card shows no completed forms', async () => {
  const { calls } = reader(['customers.view', 'forms.issue'])
  renderApp('/clients/c1')

  const card = (await screen.findByRole('heading', { name: 'Forms' })).closest('[data-slot="card"]') as HTMLElement
  expect(await within(card).findByRole('button', { name: 'Send form' })).toBeInTheDocument()
  expect(within(card).queryByRole('table', { name: 'Completed forms' })).not.toBeInTheDocument()
  expect(calls.some((c) => c.url.endsWith('/c1/forms'))).toBe(false)
})

test('an archived PDF is unavailable while rendering and opens an authenticated blob once ready', async () => {
  let ready = false
  reader(['customers.view', 'forms.view'], () => ready)
  const preview = { opener: {}, location: { replace: vi.fn() }, close: vi.fn() }
  const opened = vi.spyOn(window, 'open').mockReturnValue(preview as unknown as Window)
  const create = vi.fn(() => 'blob:archived-form')
  vi.stubGlobal('URL', Object.assign(URL, { createObjectURL: create, revokeObjectURL: vi.fn() }))
  const passthrough = vi.mocked(fetch).getMockImplementation()!
  vi.mocked(fetch).mockImplementation(async (url, init) => {
    if (!String(url).endsWith('/s1/pdf')) return passthrough(url, init)
    expect(opened).toHaveBeenCalledWith('', '_blank')
    expect(preview.opener).toBeNull()
    return new Response(new Blob(['archive'], { type: 'application/pdf' }), { headers: { 'Content-Type': 'application/pdf' } })
  })
  const user = userEvent.setup()
  renderApp('/clients/c1')
  expect(await screen.findByRole('button', { name: /Rendering/ })).toBeDisabled()
  ready = true
  const view = await screen.findByRole('button', { name: 'View PDF for Prenatal intake' }, { timeout: 5500 })
  await user.click(view)
  await waitFor(() => expect(preview.location.replace).toHaveBeenCalledWith('blob:archived-form'))
})

// --- the pad repaints what it holds ---------------------------------------------------------

test('a remounted pad redraws the signature it still holds, and a new one stays blank', async () => {
  const { render } = await import('@testing-library/react')
  const { SignaturePad } = await import('@/components/signature-pad')
  const painted: string[] = []
  const context = new Proxy(
    {},
    {
      get: (_, key) =>
        key === 'drawImage' ? (image: HTMLImageElement) => painted.push(image.src) : () => {},
      set: () => true,
    },
  )
  vi.mocked(HTMLCanvasElement.prototype.getContext).mockReturnValue(
    context as unknown as CanvasRenderingContext2D,
  )
  // jsdom never loads images: this one "loads" as soon as it has a source.
  vi.stubGlobal(
    'Image',
    class {
      onload: (() => void) | null = null
      private source = ''
      get src() {
        return this.source
      }
      set src(value: string) {
        this.source = value
        queueMicrotask(() => this.onload?.())
      }
    },
  )

  render(<SignaturePad id="sig" value={{ name: 'Priya Nair', image: DRAWN }} onChange={() => {}} />)
  await waitFor(() => expect(painted).toEqual([DRAWN]))

  painted.length = 0
  render(<SignaturePad id="blank" value={undefined} onChange={() => {}} />)
  await Promise.resolve()
  expect(painted).toEqual([])
})
