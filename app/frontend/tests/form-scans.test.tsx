import { QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router'
import { afterEach, expect, test, vi } from 'vitest'
import { ClientFormsCard } from '@/components/client-forms'
import { createQueryClient } from '@/lib/query-client'
import { stubApi } from './harness'
import { choose } from './choose'

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

function card() {
  stubApi({
    signedIn: true,
    respond: (url) => {
      if (url === '/api/forms/templates')
        return Response.json({
          templates: [
            { template_id: 't1', name: 'Intake', kind: 'intake', version: 2 },
          ],
        })
      if (url === '/api/forms/templates/t1/versions')
        return Response.json({
          versions: [
            { number: 2, name: 'Intake' },
            { number: 1, name: 'Old intake' },
          ],
        })
      if (url === '/api/customers/c1/forms')
        return Response.json({ submissions: [] })
      if (url === '/api/customers/c1/form-links')
        return Response.json({ links: [] })
      return undefined
    },
  })
  render(
    <QueryClientProvider client={createQueryClient()}>
      <MemoryRouter>
        <ClientFormsCard
          customerId="c1"
          timezone="America/Toronto"
          canSend
          canView
          suppressed={false}
        />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

test('a phone photo is downsampled before a JSON scan upload and a refusal stays visible', async () => {
  card()
  vi.stubGlobal(
    'URL',
    class extends URL {
      static createObjectURL() {
        return 'blob:photo'
      }
      static revokeObjectURL() {}
    },
  )
  vi.stubGlobal(
    'Image',
    class {
      naturalWidth = 5000
      naturalHeight = 7000
      src = ''
      decode() {
        return Promise.resolve()
      }
    },
  )
  const dimensions: number[][] = []
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({
    drawImage: vi.fn(),
    filter: '',
  } as unknown as CanvasRenderingContext2D)
  vi.spyOn(HTMLCanvasElement.prototype, 'toBlob').mockImplementation(function (
    this: HTMLCanvasElement,
    cb,
  ) {
    dimensions.push([this.width, this.height])
    cb(new Blob(['small-page'], { type: 'image/jpeg' }))
  })
  const bodies: Record<string, unknown>[] = []
  const passthrough = vi.mocked(fetch).getMockImplementation()!
  vi.mocked(fetch).mockImplementation(async (url, init) => {
    if (url === '/api/customers/c1/forms/scans') {
      expect(init?.headers).toEqual({ 'Content-Type': 'application/json' })
      bodies.push(JSON.parse(String(init?.body)))
      return Response.json(
        { detail: 'This client has been erased.' },
        { status: 422 },
      )
    }
    return passthrough(url, init)
  })
  const user = userEvent.setup()
  await user.click(await screen.findByRole('button', { name: 'Upload scan' }))
  await choose(user, 'Form template', 'Intake')
  await user.upload(
    screen.getByLabelText('Scanned pages'),
    new File(['large-photo'], 'phone.jpg', { type: 'image/jpeg' }),
  )
  await user.click(await screen.findByRole('button', { name: 'Save scan' }))
  expect(await screen.findByRole('alert')).toHaveTextContent(
    'This client has been erased.',
  )
  expect(dimensions).toEqual([[1179, 1650]])
  await waitFor(() => expect(bodies).toHaveLength(1))
  expect(bodies[0]).toMatchObject({
    template_id: 't1',
    version_number: 2,
    pages: ['c21hbGwtcGFnZQ=='],
  })
})

test('printing opens a disconnected preview during the button press before fetching HTML', async () => {
  card()
  const preview = { opener: {}, location: { replace: vi.fn() }, close: vi.fn() }
  const opened = vi
    .spyOn(window, 'open')
    .mockReturnValue(preview as unknown as Window)
  vi.stubGlobal(
    'URL',
    class extends URL {
      static createObjectURL() {
        return 'blob:blank-form'
      }
      static revokeObjectURL() {}
    },
  )
  const passthrough = vi.mocked(fetch).getMockImplementation()!
  vi.mocked(fetch).mockImplementation(async (url, init) => {
    if (url === '/api/admin/forms/t1/versions/2/print') {
      expect(opened).toHaveBeenCalledWith('', '_blank')
      expect(preview.opener).toBeNull()
      return new Response('<html><body>Version 2</body></html>')
    }
    return passthrough(url, init)
  })
  const user = userEvent.setup()
  await user.click(
    await screen.findByRole('button', { name: 'Print blank form' }),
  )
  await choose(user, 'Form template', 'Intake')
  await waitFor(() =>
    expect(screen.getByRole('button', { name: /^Print$/ })).toBeEnabled(),
  )
  await user.click(screen.getByRole('button', { name: /^Print$/ }))
  await waitFor(() =>
    expect(preview.location.replace).toHaveBeenCalledWith('blob:blank-form'),
  )
})

test('an unconfirmed scan retry keeps its exact pages and submission id', async () => {
  card()
  vi.stubGlobal(
    'URL',
    class extends URL {
      static createObjectURL() {
        return 'blob:photo'
      }
      static revokeObjectURL() {}
    },
  )
  vi.stubGlobal(
    'Image',
    class {
      naturalWidth = 1000
      naturalHeight = 1500
      src = ''
      decode() {
        return Promise.resolve()
      }
    },
  )
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({
    drawImage: vi.fn(),
    filter: '',
  } as unknown as CanvasRenderingContext2D)
  vi.spyOn(HTMLCanvasElement.prototype, 'toBlob').mockImplementation((cb) =>
    cb(new Blob(['page'], { type: 'image/jpeg' })),
  )
  const bodies: unknown[] = []
  const passthrough = vi.mocked(fetch).getMockImplementation()!
  vi.mocked(fetch).mockImplementation(async (url, init) => {
    if (url === '/api/customers/c1/forms/scans') {
      bodies.push(JSON.parse(String(init?.body)))
      if (bodies.length === 1) throw new TypeError('Response lost')
      return Response.json({ status: 'already_received' })
    }
    return passthrough(url, init)
  })
  const user = userEvent.setup()
  await user.click(await screen.findByRole('button', { name: 'Upload scan' }))
  await choose(user, 'Form template', 'Intake')
  await user.upload(
    screen.getByLabelText('Scanned pages'),
    new File(['photo'], 'phone.jpg', { type: 'image/jpeg' }),
  )
  await user.click(screen.getByRole('button', { name: 'Save scan' }))
  await screen.findByRole('alert')
  expect(screen.getByLabelText('Scanned pages')).toBeDisabled()
  expect(screen.getByLabelText('Form template')).toBeDisabled()
  expect(screen.getByLabelText('Published version')).toBeDisabled()
  await user.click(screen.getByRole('button', { name: 'Save scan' }))
  await waitFor(() => expect(bodies).toHaveLength(2))
  expect(bodies[1]).toEqual(bodies[0])
})
