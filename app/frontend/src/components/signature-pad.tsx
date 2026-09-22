import { useEffect, useRef } from 'react'
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
 */
export function SignaturePad(props: {
  id: string
  value: unknown
  onChange: (value: Signature | undefined) => void
  describedBy?: string
}) {
  const canvas = useRef<HTMLCanvasElement>(null)
  const drawing = useRef(false)
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
  useEffect(paper, [])

  const emit = (nextName: string, nextImage: string) =>
    props.onChange(nextName || nextImage ? { name: nextName, image: nextImage } : undefined)
  const snapshot = () => canvas.current?.toDataURL('image/png') ?? ''

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
    emit(name, snapshot())
  }
  const clear = () => {
    paper()
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
