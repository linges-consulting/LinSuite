import { useEffect, useRef, useState } from 'react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'

/** The pad's own resolution, whatever its size on screen: every signature leaves the device
 *  as at most a 600 x 200 PNG — a few KB, far under the server's 200 KB cap and the edge's
 *  2 MiB body limit, on any phone or tablet. */
const WIDTH = 600
const HEIGHT = 200
// Paper and ink, not theme colours: the image is a record, and the server looks for dark
// pixels on it (a blank or white pad is not a signature). Same in light and dark mode.
const PAPER = '#ffffff'
const INK = '#111827'

// Mirrors `forms/submissions.py`'s `_has_ink` — same bounding-box and ink-ratio thresholds,
// so a signature this pad accepts, the server accepts too. `app/shared/signature-ink-
// constants.json` is the canonical reference for these four numbers and
// `tests/test_signature_ink_constants.py` (backend) fails if this file or the server's drifts
// from it — a runtime shared import was tried and reverted, because the frontend's dev/prod
// containers only bind-mount `app/frontend` (`infra/compose.yaml`), so a cross-directory
// import 404s inside the real container even though it resolves fine on the host.
const MIN_INK_PIXELS = 20
const MIN_INK_WIDTH = 0.05
const MIN_INK_HEIGHT = 0.03
const MAX_INK_RATIO = 0.6

export const SIGNATURE_TOO_SMALL = 'Your signature is too small — please sign again.'

/** Whether the pad's current pixels are a real signature, not a dot or a blotch. Fails open
 *  (assumes yes) when pixel data cannot be read at all — a canvas the browser has tainted,
 *  or a test double with no `getImageData` — so a real environment issue never blocks a
 *  legitimate signature; the server is still the final judge either way. */
function hasEnoughInk(ctx: CanvasRenderingContext2D): boolean {
  let data: Uint8ClampedArray
  try {
    data = ctx.getImageData(0, 0, WIDTH, HEIGHT).data
  } catch {
    return true
  }
  let minX = WIDTH
  let minY = HEIGHT
  let maxX = -1
  let maxY = -1
  let dark = 0
  for (let y = 0; y < HEIGHT; y++) {
    for (let x = 0; x < WIDTH; x++) {
      const i = (y * WIDTH + x) * 4
      // The same luminance formula PIL's `convert('L')` uses.
      const luminance = 0.299 * data[i] + 0.587 * data[i + 1] + 0.114 * data[i + 2]
      if (luminance < 128) {
        dark++
        if (x < minX) minX = x
        if (x > maxX) maxX = x
        if (y < minY) minY = y
        if (y > maxY) maxY = y
      }
    }
  }
  if (dark < MIN_INK_PIXELS || dark > MAX_INK_RATIO * WIDTH * HEIGHT) return false
  return maxX - minX + 1 >= MIN_INK_WIDTH * WIDTH && maxY - minY + 1 >= MIN_INK_HEIGHT * HEIGHT
}

export type Signature = { name: string; image: string }

/**
 * A signature (owner ruling Q3): drawn *and* a typed full name, both required.
 *
 * Native `<canvas>` with pointer events — mouse, pen and finger alike; `touch-none` keeps a
 * finger from scrolling the page instead of drawing. The typed-name field sits beside it and
 * is an ordinary labelled input. **Keyboard alternative:** "Sign with my typed name" writes
 * the typed name onto the pad in a script face, so a client who cannot draw can still sign;
 * "Clear signature" starts over either way.
 *
 * The value is `{name, image}` (a PNG data URL, `''` until something is on the pad), or
 * `undefined` while both are empty — so an untouched block reads as unanswered.
 *
 * **The ink check** (fix: a trivial stroke used to pass here and only fail once it reached
 * the server). A stroke that does not clear the server's thresholds still becomes the pad's
 * answer — this never rewrites `lib/forms.ts`'s shared shape check, which a half-filled pad
 * (a typed name with nothing drawn yet) already fails for an unrelated reason — but it shows
 * its own hint right under the canvas, and reports itself through `onWeakChange` so the page
 * can hold Submit back rather than round-tripping a signature the server would refuse anyway.
 */
export function SignaturePad(props: {
  id: string
  value: unknown
  onChange: (value: Signature | undefined) => void
  describedBy?: string
  /** Called with `true` right after a stroke or a typed signature that does not clear the
   *  ink thresholds, `false` once it does (or the pad is cleared). Omitted by callers with no
   *  Submit button to hold back — the builder's live preview, say. */
  onWeakChange?: (weak: boolean) => void
}) {
  const canvas = useRef<HTMLCanvasElement>(null)
  const drawing = useRef(false)
  const [weak, setWeak] = useState(false)
  const current = (props.value ?? {}) as Partial<Signature>
  const name = current.name ?? ''
  const image = current.image ?? ''

  const context = () => canvas.current?.getContext('2d') ?? null
  const paper = () => {
    const ctx = context()
    if (!ctx) return
    ctx.fillStyle = PAPER
    ctx.fillRect(0, 0, WIDTH, HEIGHT)
  }
  // On mount: fresh paper, then whatever signature the value still holds — a pad remounted
  // with its answer kept must show it, not a blank that reads as "signed".
  useEffect(() => {
    paper()
    const ctx = context()
    if (!ctx || !image) return
    const held = new Image()
    held.onload = () => ctx.drawImage(held, 0, 0, WIDTH, HEIGHT)
    held.src = image
    // eslint-disable-next-line react-hooks/exhaustive-deps -- once, on mount
  }, [])

  const emit = (nextName: string, nextImage: string) =>
    props.onChange(nextName || nextImage ? { name: nextName, image: nextImage } : undefined)
  const snapshot = () => canvas.current?.toDataURL('image/png') ?? ''
  const setWeakFlag = (value: boolean) => {
    setWeak(value)
    props.onWeakChange?.(value)
  }

  const at = (event: React.PointerEvent<HTMLCanvasElement>) => {
    const box = event.currentTarget.getBoundingClientRect()
    return [
      ((event.clientX - box.left) * WIDTH) / (box.width || WIDTH),
      ((event.clientY - box.top) * HEIGHT) / (box.height || HEIGHT),
    ] as const
  }

  const start = (event: React.PointerEvent<HTMLCanvasElement>) => {
    const ctx = context()
    if (!ctx) return
    event.currentTarget.setPointerCapture(event.pointerId)
    drawing.current = true
    ctx.strokeStyle = INK
    ctx.lineWidth = 3
    ctx.lineCap = 'round'
    ctx.lineJoin = 'round'
    ctx.beginPath()
    ctx.moveTo(...at(event))
  }
  const move = (event: React.PointerEvent<HTMLCanvasElement>) => {
    const ctx = context()
    if (!drawing.current || !ctx) return
    ctx.lineTo(...at(event))
    ctx.stroke()
  }
  const end = () => {
    if (!drawing.current) return
    drawing.current = false
    const ctx = context()
    setWeakFlag(!(!ctx || hasEnoughInk(ctx)))
    emit(name, snapshot())
  }

  const signWithName = () => {
    const ctx = context()
    if (!ctx || !name.trim()) return
    paper()
    ctx.fillStyle = INK
    ctx.font = 'italic 56px "Segoe Script", "Brush Script MT", "Snell Roundhand", cursive'
    ctx.textBaseline = 'middle'
    ctx.fillText(name.trim(), 24, HEIGHT / 2, WIDTH - 48)
    setWeakFlag(!hasEnoughInk(ctx))
    emit(name, snapshot())
  }
  const clear = () => {
    paper()
    setWeakFlag(false)
    emit(name, '')
  }

  return (
    <div className="flex flex-col gap-2">
      <canvas
        ref={canvas}
        width={WIDTH}
        height={HEIGHT}
        role="img"
        aria-label={image ? 'Signature pad — signed' : 'Signature pad — draw your signature here'}
        className="block aspect-[3/1] w-full cursor-crosshair touch-none rounded-lg border bg-white"
        onPointerDown={start}
        onPointerMove={move}
        onPointerUp={end}
        onPointerCancel={end}
      />
      {weak && <p role="alert" className="text-sm text-destructive">{SIGNATURE_TOO_SMALL}</p>}
      <div className="flex flex-wrap gap-2">
        <Button type="button" variant="outline" size="sm" onClick={clear}>
          Clear signature
        </Button>
        <Button type="button" variant="outline" size="sm" onClick={signWithName} disabled={!name.trim()}>
          Sign with my typed name
        </Button>
      </div>
      <Label htmlFor={props.id} className="font-normal">
        Full name
      </Label>
      <Input
        id={props.id}
        autoComplete="name"
        maxLength={200}
        value={name}
        aria-describedby={props.describedBy}
        aria-invalid={props.describedBy ? true : undefined}
        onChange={(e) => emit(e.target.value, image)}
      />
    </div>
  )
}
