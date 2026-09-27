import { useRef, useState } from 'react'
import { MapPin, Trash2 } from 'lucide-react'
import { Field, FormError } from '@/components/form'
import { ChoiceSelect } from '@/components/choice-select'
import { EmptyState } from '@/components/empty-state'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { type DiagramId, type NoteAnnotation } from '@/lib/api'

import { ANNOTATION_COLOURS, DIAGRAM_LABELS } from '@/lib/note-diagrams'

/** Stable named diagrams. Normalized coordinates refer to the entire 400 × 600 canvas.
 * Changing these outlines would require a new diagram id so old records keep their meaning. */
function Outline({ diagram }: { diagram: DiagramId }) {
  if (diagram === 'layout')
    return (
      <g fill="none" stroke="currentColor" strokeWidth="3">
        <rect x="25" y="40" width="350" height="520" rx="2" />
        <path d="M25 280H160m55 0h160M200 40v195m0 90v235M25 425h100m60 0h190" />
        <path
          d="M160 280v-55a55 55 0 0 1 55 55M125 425v-60a60 60 0 0 1 60 60"
          strokeDasharray="5 4"
        />
        <text x="50" y="75" stroke="none" fill="currentColor" fontSize="16">
          Layout
        </text>
      </g>
    )
  return (
    <g
      fill="none"
      stroke="currentColor"
      strokeWidth="2.5"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <ellipse cx="200" cy="62" rx="34" ry="43" />
      <path d="M177 99v21l-52 20-40 132-17 62 17 7 28-65 30-85-1 122 21 210 27 0 10-189 10 189h27l21-210-1-122 30 85 28 65 17-7-17-62-40-132-52-20V99M163 523l-13 35 38 0 2-35m20 0 2 35h38l-13-35" />
      {diagram === 'body_front' ? (
        <>
          <path d="M167 180q33 18 66 0M200 202v96M171 315q29 16 58 0" />
          <circle cx="188" cy="56" r="2" />
          <circle cx="212" cy="56" r="2" />
          <path d="M189 79q11 6 22 0" />
        </>
      ) : (
        <>
          <path
            d="M200 136v179M163 164l23 41m51-41-23 41M164 326q36-25 72 0"
            strokeDasharray="5 5"
          />
          <path d="M173 50q27-12 54 0" />
        </>
      )}
    </g>
  )
}

type Point = { x: number; y: number }
const number = (value: string) => (value.trim() ? Number(value) : NaN)
function annotationErrors(
  kind: NoteAnnotation['kind'],
  text: string,
  point: Point,
  zone?: { width: number; height: number },
) {
  const errors: Record<string, string> = {}
  if (kind === 'text' && !text.trim())
    errors.text = 'Enter the annotation text.'
  for (const axis of ['x', 'y'] as const) {
    if (!Number.isFinite(point[axis]) || point[axis] < 0 || point[axis] > 1)
      errors[axis] = 'Enter a number between 0 and 1.'
  }
  if (zone) {
    if (
      !Number.isFinite(zone.width) ||
      zone.width <= 0 ||
      point.x + zone.width > 1
    )
      errors.width = 'Enter a positive width that fits inside the diagram.'
    if (
      !Number.isFinite(zone.height) ||
      zone.height <= 0 ||
      point.y + zone.height > 1
    )
      errors.height = 'Enter a positive height that fits inside the diagram.'
  }
  return errors
}
export function NoteDiagram(props: {
  diagrams: DiagramId[]
  annotations: NoteAnnotation[]
  readOnly: boolean
  timezone: string
  onChange: (value: NoteAnnotation[]) => void
}) {
  const [diagram, setDiagram] = useState<DiagramId>(props.diagrams[0])
  const [kind, setKind] = useState<NoteAnnotation['kind']>('pin')
  const [colour, setColour] = useState(ANNOTATION_COLOURS[0].value)
  const [text, setText] = useState('')
  const [x, setX] = useState('0.5')
  const [y, setY] = useState('0.5')
  const [width, setWidth] = useState('0.2')
  const [height, setHeight] = useState('0.15')
  const [error, setError] = useState('')
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({})
  const blur = (field: string) => {
    const errors = annotationErrors(
      kind,
      text,
      { x: number(x), y: number(y) },
      kind === 'zone'
        ? { width: number(width), height: number(height) }
        : undefined,
    )
    setFieldErrors((current) => ({ ...current, [field]: errors[field] ?? '' }))
  }
  const start = useRef<Point | null>(null)
  const [preview, setPreview] = useState<{
    x: number
    y: number
    width: number
    height: number
  } | null>(null)
  if (!props.diagrams.length) return null
  const add = (point: Point, size?: { width: number; height: number }) => {
    if (props.readOnly) return
    if (props.annotations.length >= 500) {
      setError('A note can contain at most 500 annotations.')
      return
    }
    const zone =
      kind === 'zone'
        ? (size ?? { width: number(width), height: number(height) })
        : undefined
    const errors = annotationErrors(kind, text, point, zone)
    setFieldErrors(errors)
    if (Object.keys(errors).length) return
    props.onChange([
      ...props.annotations,
      {
        id: crypto.randomUUID(),
        kind,
        diagram_id: diagram,
        ...point,
        ...zone,
        colour,
        text,
        timestamp: new Date().toISOString(),
      },
    ])
    setError('')
  }
  const point = (event: React.PointerEvent<SVGSVGElement>): Point => {
    const rect = event.currentTarget.getBoundingClientRect()
    return {
      x: Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width)),
      y: Math.max(0, Math.min(1, (event.clientY - rect.top) / rect.height)),
    }
  }
  const rectangle = (a: Point, b: Point) => ({
    x: Math.min(a.x, b.x),
    y: Math.min(a.y, b.y),
    width: Math.abs(a.x - b.x),
    height: Math.abs(a.y - b.y),
  })
  return (
    <section
      className="space-y-4 rounded-xl border p-4"
      aria-label="Visual markup"
    >
      <div className="flex items-center gap-2 font-medium">
        <MapPin className="size-4" aria-hidden />
        Visual markup
      </div>
      <div className="grid gap-5 md:grid-cols-[minmax(180px,260px)_1fr]">
        <div className="space-y-3">
          <Field label="Diagram" htmlFor="note-diagram">
            <ChoiceSelect
              id="note-diagram"
              value={diagram}
              onValueChange={(value) => setDiagram(value as DiagramId)}
              options={props.diagrams.map((id) => ({
                value: id,
                label: DIAGRAM_LABELS[id],
              }))}
            />
          </Field>
          <svg
            role="img"
            aria-label={`${DIAGRAM_LABELS[diagram]} diagram`}
            viewBox="0 0 400 600"
            className={`aspect-[2/3] w-full rounded-lg border bg-muted/30 text-muted-foreground ${props.readOnly ? '' : 'touch-none cursor-crosshair'}`}
            onPointerDown={(e) => {
              if (props.readOnly || e.button !== 0) return
              start.current = point(e)
              e.currentTarget.setPointerCapture(e.pointerId)
            }}
            onPointerMove={(e) => {
              if (start.current && kind === 'zone')
                setPreview(rectangle(start.current, point(e)))
            }}
            onPointerCancel={() => {
              start.current = null
              setPreview(null)
            }}
            onPointerUp={(e) => {
              if (!start.current || props.readOnly) return
              const end = point(e)
              if (kind === 'zone') {
                const zone = rectangle(start.current, end)
                if (zone.width > 0.003 && zone.height > 0.003) add(zone, zone)
                else
                  setError(
                    'Drag across the diagram to shade a zone, or enter its coordinates.',
                  )
              } else add(end)
              start.current = null
              setPreview(null)
            }}
          >
            <Outline diagram={diagram} />
            {props.annotations
              .filter((a) => a.diagram_id === diagram)
              .map((a) => (
                <g key={a.id}>
                  <title>{`${a.kind}: ${a.text || 'Annotation'} · ${a.timestamp}`}</title>
                  {a.kind === 'zone' ? (
                    <rect
                      x={a.x * 400}
                      y={a.y * 600}
                      width={(a.width ?? 0) * 400}
                      height={(a.height ?? 0) * 600}
                      fill={a.colour}
                      fillOpacity="0.25"
                      stroke={a.colour}
                      strokeWidth="2"
                    />
                  ) : (
                    <circle
                      cx={a.x * 400}
                      cy={a.y * 600}
                      r="8"
                      fill={a.colour}
                    />
                  )}
                  <text
                    x={a.x * 400 + (a.x > 0.5 ? -12 : 12)}
                    textAnchor={a.x > 0.5 ? 'end' : 'start'}
                    y={Math.max(18, a.y * 600 - 12)}
                    fill={a.colour}
                    fontSize="16"
                    fontWeight="600"
                  >
                    {props.annotations.indexOf(a) + 1}
                    {a.kind === 'text'
                      ? ` · ${a.text.slice(0, 18)}${a.text.length > 18 ? '…' : ''}`
                      : ''}
                  </text>
                </g>
              ))}
            {preview && (
              <rect
                x={preview.x * 400}
                y={preview.y * 600}
                width={preview.width * 400}
                height={preview.height * 600}
                fill={colour}
                fillOpacity="0.15"
                stroke={colour}
                strokeDasharray="5 4"
              />
            )}
          </svg>
        </div>
        <div className="space-y-4">
          {!props.readOnly && (
            <>
              <p className="text-sm text-muted-foreground">
                Click to place a pin or text. Drag to shade a zone. You can also
                enter coordinates below; 0 is the top or left edge, 1 is the
                bottom or right edge.
              </p>
              <div className="grid grid-cols-2 gap-3">
                <Field label="Annotation tool" htmlFor="note-tool">
                  <ChoiceSelect
                    id="note-tool"
                    value={kind}
                    onValueChange={(value) =>
                      setKind(value as NoteAnnotation['kind'])
                    }
                    options={[
                      { value: 'pin', label: 'Pin' },
                      { value: 'zone', label: 'Shaded zone' },
                      { value: 'text', label: 'Text' },
                    ]}
                  />
                </Field>
                <Field label="Annotation colour" htmlFor="note-colour">
                  <ChoiceSelect
                    id="note-colour"
                    value={colour}
                    onValueChange={setColour}
                    options={ANNOTATION_COLOURS}
                  />
                </Field>
              </div>
              <Field
                label="Annotation text"
                htmlFor="annotation-text"
                error={fieldErrors.text}
              >
                <Textarea
                  id="annotation-text"
                  rows={2}
                  maxLength={2000}
                  value={text}
                  onBlur={() => blur('text')}
                  onChange={(e) => setText(e.target.value)}
                />
              </Field>
              <div className="grid grid-cols-2 gap-3">
                <Field
                  label="X coordinate"
                  htmlFor="mark-x"
                  error={fieldErrors.x}
                >
                  <Input
                    type="number"
                    id="mark-x"
                    min="0"
                    max="1"
                    step="0.01"
                    value={x}
                    onBlur={() => blur('x')}
                    onChange={(e) => setX(e.target.value)}
                  />
                </Field>
                <Field
                  label="Y coordinate"
                  htmlFor="mark-y"
                  error={fieldErrors.y}
                >
                  <Input
                    type="number"
                    id="mark-y"
                    min="0"
                    max="1"
                    step="0.01"
                    value={y}
                    onBlur={() => blur('y')}
                    onChange={(e) => setY(e.target.value)}
                  />
                </Field>
                {kind === 'zone' && (
                  <>
                    <Field
                      label="Zone width"
                      htmlFor="mark-width"
                      error={fieldErrors.width}
                    >
                      <Input
                        id="mark-width"
                        type="number"
                        min="0.01"
                        max="1"
                        step="0.01"
                        value={width}
                        onBlur={() => blur('width')}
                        onChange={(e) => setWidth(e.target.value)}
                      />
                    </Field>
                    <Field
                      label="Zone height"
                      htmlFor="mark-height"
                      error={fieldErrors.height}
                    >
                      <Input
                        id="mark-height"
                        type="number"
                        min="0.01"
                        max="1"
                        step="0.01"
                        value={height}
                        onBlur={() => blur('height')}
                        onChange={(e) => setHeight(e.target.value)}
                      />
                    </Field>
                  </>
                )}
              </div>
              {error && <FormError>{error}</FormError>}
              <Button
                variant="outline"
                onClick={() => add({ x: number(x), y: number(y) })}
              >
                Add annotation
              </Button>
            </>
          )}
          <div className="space-y-2">
            <h3 className="text-sm font-medium">Recorded annotations</h3>
            {!props.annotations.length && (
              <EmptyState
                icon={MapPin}
                title="No annotations recorded"
                description="Add a pin, shaded zone or text to document a location on the diagram."
              />
            )}
            <ol className="space-y-2">
              {props.annotations.map((a, i) => (
                <li
                  key={a.id}
                  className="flex items-start gap-3 rounded-lg border p-3"
                >
                  <span
                    className="mt-1 size-3 shrink-0 rounded-full"
                    style={{ backgroundColor: a.colour }}
                    aria-hidden
                  />
                  <div className="min-w-0 flex-1">
                    <p className="break-words whitespace-pre-wrap">
                      {i + 1}. {a.text || a.kind}
                    </p>
                    <p className="text-xs text-muted-foreground">
                      {DIAGRAM_LABELS[a.diagram_id]} · {a.kind} · (
                      {a.x.toFixed(3)}, {a.y.toFixed(3)})
                    </p>
                    <time
                      className="text-xs text-muted-foreground"
                      dateTime={a.timestamp}
                    >
                      {new Date(a.timestamp).toLocaleString('en-CA', {
                        timeZone: props.timezone,
                        dateStyle: 'medium',
                        timeStyle: 'short',
                      })}
                    </time>
                  </div>
                  {!props.readOnly && (
                    <Button
                      variant="ghost"
                      size="icon-sm"
                      aria-label={`Remove annotation ${i + 1}`}
                      onClick={() =>
                        props.onChange(
                          props.annotations.filter((mark) => mark.id !== a.id),
                        )
                      }
                    >
                      <Trash2 aria-hidden />
                    </Button>
                  )}
                </li>
              ))}
            </ol>
          </div>
        </div>
      </div>
    </section>
  )
}
