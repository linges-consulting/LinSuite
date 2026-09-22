import { fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { SignaturePad } from '@/components/signature-pad'

/**
 * Fix: the client-side ink check used to accept any non-blank stroke, so a trivial mark
 * passed here and only failed once it reached the server's stricter one — a round trip for a
 * generic error. `signature-pad.tsx` now mirrors the server's bounding-box and ink-ratio
 * thresholds (`forms/submissions.py`'s `_has_ink`): a stroke that fails them still becomes
 * the pad's answer (the shared shape check in `lib/forms.ts` is untouched), but the pad shows
 * its own hint underneath and reports itself weak through `onWeakChange`, which the fill page
 * uses to hold Submit back rather than round-tripping a signature the server would refuse.
 *
 * jsdom has no real canvas, so `getImageData` is mocked to return a fixed pixel buffer
 * standing in for "what was drawn" — a few dark pixels for a trivial mark, most of the pad
 * dark for a real signature — independent of the no-op draw calls jsdom would otherwise
 * silently drop.
 */

const WIDTH = 600
const HEIGHT = 200
const TOO_SMALL = 'Your signature is too small — please sign again.'

function imageData(darkPixels: Array<[number, number]>): ImageData {
  const data = new Uint8ClampedArray(WIDTH * HEIGHT * 4).fill(255) // white, opaque
  for (const [x, y] of darkPixels) {
    const i = (y * WIDTH + x) * 4
    data[i] = data[i + 1] = data[i + 2] = 0
    data[i + 3] = 255
  }
  return { data, width: WIDTH, height: HEIGHT, colorSpace: 'srgb' } as ImageData
}

function stubCanvas(getImageDataReturn: ImageData) {
  const context = new Proxy(
    {},
    {
      get: (_, key) => (key === 'getImageData' ? () => getImageDataReturn : () => {}),
      set: () => true,
    },
  )
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(
    context as unknown as CanvasRenderingContext2D,
  )
  vi.spyOn(HTMLCanvasElement.prototype, 'toDataURL').mockReturnValue('data:image/png;base64,REAL')
}

beforeEach(() => {
  vi.restoreAllMocks()
})

afterEach(() => {
  vi.restoreAllMocks()
})

async function draw() {
  const pad = screen.getByRole('img', { name: /signature pad/i })
  fireEvent.pointerDown(pad, { clientX: 10, clientY: 10, pointerId: 1 })
  fireEvent.pointerMove(pad, { clientX: 60, clientY: 40, pointerId: 1 })
  fireEvent.pointerUp(pad, { clientX: 60, clientY: 40, pointerId: 1 })
}

test('a trivial stroke — a handful of dark pixels in a tiny corner — is flagged weak, with the specific hint', async () => {
  // Six pixels, all within a few pixels of one another: under `MIN_INK_PIXELS` (20) and far
  // under the minimum bounding-box span either way.
  stubCanvas(imageData([[0, 0], [1, 0], [2, 0], [0, 1], [1, 1], [2, 1]]))
  const onWeakChange = vi.fn()
  render(<SignaturePad id="sig" value={undefined} onChange={() => {}} onWeakChange={onWeakChange} />)

  await draw()

  expect(onWeakChange).toHaveBeenLastCalledWith(true)
  expect(await screen.findByRole('alert')).toHaveTextContent(TOO_SMALL)
})

test('a stroke spanning most of the pad is accepted, with no hint and no weak report', async () => {
  const dark: Array<[number, number]> = []
  for (let x = 0; x < WIDTH; x += 4) {
    // Vary both axes: a single-row line has zero height and would fail the height span.
    dark.push([x, 40 + (x % 120)])
  }
  stubCanvas(imageData(dark))
  const onChange = vi.fn()
  const onWeakChange = vi.fn()
  render(
    <SignaturePad id="sig" value={undefined} onChange={onChange} onWeakChange={onWeakChange} />,
  )

  await draw()

  expect(onChange).toHaveBeenLastCalledWith({ name: '', image: 'data:image/png;base64,REAL' })
  expect(onWeakChange).toHaveBeenLastCalledWith(false)
  expect(screen.queryByRole('alert')).not.toBeInTheDocument()
})

test('a stroke that fills nearly the whole pad (an accidental smear) is flagged weak too', async () => {
  const dark: Array<[number, number]> = []
  for (let y = 0; y < HEIGHT; y++) {
    for (let x = 0; x < WIDTH; x++) dark.push([x, y])
  }
  stubCanvas(imageData(dark))
  const onWeakChange = vi.fn()
  render(<SignaturePad id="sig" value={undefined} onChange={() => {}} onWeakChange={onWeakChange} />)

  await draw()

  expect(onWeakChange).toHaveBeenLastCalledWith(true)
})

test('when pixel data cannot be read at all, the pad fails open rather than flagging a real signature', async () => {
  // No `getImageData` at all — the no-op proxy every other signature test in this suite uses.
  const context = new Proxy({}, { get: () => () => {}, set: () => true })
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(
    context as unknown as CanvasRenderingContext2D,
  )
  vi.spyOn(HTMLCanvasElement.prototype, 'toDataURL').mockReturnValue('data:image/png;base64,REAL')
  const onWeakChange = vi.fn()
  render(<SignaturePad id="sig" value={undefined} onChange={() => {}} onWeakChange={onWeakChange} />)

  await draw()

  expect(onWeakChange).not.toHaveBeenCalledWith(true)
  expect(screen.queryByRole('alert')).not.toBeInTheDocument()
})

test('clearing the pad drops the weak hint', async () => {
  stubCanvas(imageData([[0, 0], [1, 0], [2, 0]]))
  render(<SignaturePad id="sig" value={undefined} onChange={() => {}} />)

  await draw()
  expect(await screen.findByRole('alert')).toHaveTextContent(TOO_SMALL)

  fireEvent.click(screen.getByRole('button', { name: 'Clear signature' }))
  expect(screen.queryByRole('alert')).not.toBeInTheDocument()
})
