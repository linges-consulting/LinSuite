import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { TriangleAlert, Upload } from 'lucide-react'
import { useEffect, useMemo, useRef, useState, type CSSProperties, type DragEvent } from 'react'
import { toast } from 'sonner'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Skeleton } from '@/components/ui/skeleton'
import {
  fetchBranding,
  previewBranding,
  removeBrandingAsset,
  updateBrandColours,
  uploadBrandingAsset,
  type Branding,
  type BrandingAssetKind,
} from '@/lib/api'
import { useBranding } from '@/lib/branding'
import { BRANDING, BRANDING_DOCUMENT } from '@/lib/query-keys'

const HEX = /^#[0-9a-fA-F]{6}$/
/** WCAG AA for body text. Below it the screen warns — and saves anyway. */
const AA = 4.5
/** Long enough to swallow a drag across the colour picker, short enough to feel live. */
const PREVIEW_DEBOUNCE_MS = 200

/** The value once it has stopped changing. One consumer, so it lives here. */
function useDebounced<T>(value: T, ms: number): T {
  const [settled, setSettled] = useState(value)
  useEffect(() => {
    const timer = setTimeout(() => setSettled(value), ms)
    return () => clearTimeout(timer)
  }, [value, ms])
  return settled
}

/**
 * The white-label panel: two images and two colours.
 *
 * The dark-theme variant of each colour and the contrast of text on it are computed by the
 * server, and the preview asks for them rather than re-deriving them here. Lightening a
 * colour perceptually is real arithmetic; two implementations of it would be two answers to
 * "what does this look like on dark", and the one on screen would be the one that is wrong.
 */
export function BrandingPanel() {
  const queryClient = useQueryClient()
  const stored = useQuery({ queryKey: BRANDING, queryFn: fetchBranding })
  const [draft, setDraft] = useState<{ brand_primary: string; brand_secondary: string } | null>(
    null,
  )

  const colours = draft ?? {
    brand_primary: stored.data?.brand_primary ?? '#1d4ed8',
    brand_secondary: stored.data?.brand_secondary ?? '#0f766e',
  }
  const valid = HEX.test(colours.brand_primary) && HEX.test(colours.brand_secondary)

  // Only asked for while somebody is still choosing, and only once they stop: a colour input
  // dragged across its picker fires continuously, and without this every intermediate shade
  // is a round trip *and* a cache entry that lives for the rest of the session.
  const asked = useDebounced(
    useMemo(
      () => ({
        brand_primary: colours.brand_primary,
        brand_secondary: colours.brand_secondary,
      }),
      [colours.brand_primary, colours.brand_secondary],
    ),
    PREVIEW_DEBOUNCE_MS,
  )
  // `asked === colours` is what "they have stopped" means. Without it the first keystroke
  // fires a request for the *previous* value, which is the one already on screen.
  const settled =
    asked.brand_primary === colours.brand_primary &&
    asked.brand_secondary === colours.brand_secondary
  const preview = useQuery({
    queryKey: [...BRANDING, 'preview', asked.brand_primary, asked.brand_secondary],
    queryFn: () => previewBranding(asked),
    enabled: settled && valid && draft !== null,
    staleTime: Infinity,
  })
  // Falling back to the stored palette keeps the two panels on screen through the pause
  // rather than blanking them on every keystroke.
  const shown: Branding | undefined =
    draft === null ? stored.data : (preview.data ?? stored.data)

  const save = useMutation({
    mutationFn: updateBrandColours,
    onSuccess: (saved) => {
      queryClient.setQueryData(BRANDING, saved)
      queryClient.invalidateQueries({ queryKey: BRANDING_DOCUMENT })
      setDraft(null)
      toast.success('Brand colours saved')
    },
    onError: (error) => toast.error(error.message),
  })

  if (stored.isPending) return <Skeleton className="h-96 w-full" />
  if (stored.isError) {
    return (
      <p role="alert" className="text-sm text-destructive">
        {stored.error.message}
      </p>
    )
  }

  return (
    <div className="flex max-w-3xl flex-col gap-10">
      <section className="flex flex-col gap-4">
        <SectionTitle
          title="Logo and favicon"
          description="PNG, JPEG or WebP for the logo (up to 1 MB); PNG or ICO for the favicon. Both are resized and re-saved as PNG."
        />
        <div className="grid gap-4 sm:grid-cols-2">
          <AssetDropzone kind="logo" label="Logo" accept="image/png,image/jpeg,image/webp" />
          <AssetDropzone kind="favicon" label="Favicon" accept="image/png,image/x-icon" />
        </div>
      </section>

      <section className="flex flex-col gap-4 border-t pt-8">
        <SectionTitle
          title="Brand colours"
          description="The primary colour is buttons, links, active states and the focus ring. The secondary is an accent — calendar highlights and chart series, never a button."
        />
        <div className="grid gap-6 sm:grid-cols-2">
          <ColourField
            id="brand_primary"
            label="Primary"
            value={colours.brand_primary}
            ratio={shown?.contrast.primary}
            darkRatio={shown?.contrast.primary_dark}
            onChange={(brand_primary) => setDraft({ ...colours, brand_primary })}
          />
          <ColourField
            id="brand_secondary"
            label="Secondary"
            value={colours.brand_secondary}
            ratio={shown?.contrast.secondary}
            darkRatio={shown?.contrast.secondary_dark}
            onChange={(brand_secondary) => setDraft({ ...colours, brand_secondary })}
          />
        </div>

        {shown && (
          <div className="grid gap-4 sm:grid-cols-2">
            <ThemePreview theme="light" colors={shown.colors} />
            <ThemePreview theme="dark" colors={shown.colors} />
          </div>
        )}

        <div className="flex gap-2">
          <Button
            disabled={!valid || draft === null || save.isPending}
            onClick={() => save.mutate(colours)}
          >
            {save.isPending ? 'Saving…' : 'Save colours'}
          </Button>
          {draft !== null && (
            <Button variant="ghost" onClick={() => setDraft(null)}>
              Discard
            </Button>
          )}
        </div>
      </section>
    </div>
  )
}

function SectionTitle(props: { title: string; description: string }) {
  return (
    <div className="flex flex-col gap-1">
      <h2 className="text-base font-medium">{props.title}</h2>
      <p className="text-xs text-muted-foreground">{props.description}</p>
    </div>
  )
}

function ColourField(props: {
  id: string
  label: string
  value: string
  ratio?: number
  darkRatio?: number
  onChange: (value: string) => void
}) {
  const malformed = !HEX.test(props.value)
  return (
    <div className="flex flex-col gap-2">
      <Label htmlFor={props.id}>{props.label}</Label>
      <div className="flex items-center gap-2">
        <input
          type="color"
          aria-label={`${props.label} colour picker`}
          className="size-9 shrink-0 cursor-pointer rounded-lg border border-input bg-transparent"
          value={HEX.test(props.value) ? props.value : '#000000'}
          onChange={(e) => props.onChange(e.target.value)}
        />
        <Input
          id={props.id}
          value={props.value}
          spellCheck={false}
          maxLength={7}
          className="font-mono"
          onChange={(e) => props.onChange(e.target.value)}
          aria-invalid={malformed ? true : undefined}
        />
      </div>
      {malformed ? (
        <p className="text-xs text-destructive">Use a six-digit hex colour, like #1d4ed8.</p>
      ) : (
        <ContrastNote ratio={props.ratio} darkRatio={props.darkRatio} />
      )}
    </div>
  )
}

/**
 * The warning, not a refusal. It is the business's own brand, and an application that
 * declined a logo colour for failing an accessibility ratio would simply be wrong about
 * whose decision that is. Saying so is the useful half.
 */
function ContrastNote(props: { ratio?: number; darkRatio?: number }) {
  if (props.ratio === undefined || props.darkRatio === undefined) return null
  const worst = Math.min(props.ratio, props.darkRatio)
  if (worst >= AA) {
    return (
      <p className="text-xs text-muted-foreground" data-numeric>
        Text on this colour: {props.ratio}:1 light, {props.darkRatio}:1 dark.
      </p>
    )
  }
  return (
    <p role="alert" className="flex items-start gap-1.5 text-xs text-warning">
      <TriangleAlert aria-hidden className="mt-px size-3.5 shrink-0" />
      <span>
        Text on this colour is {worst}:1, below the 4.5:1 readability guideline. You can still
        use it.
      </span>
    </p>
  )
}

/** Both themes side by side, because dark mode is designed alongside light, not derived. */
function ThemePreview(props: { theme: 'light' | 'dark'; colors: Record<string, string> }) {
  const style: CSSProperties = {}
  for (const [name, value] of Object.entries(props.colors)) {
    // `--brand-*` is layer 1 of DESIGN.md's tokens; `.light` and `.dark` resolve the rest.
    ;(style as Record<string, string>)[`--brand-${name.replaceAll('_', '-')}`] = value
  }
  style.colorScheme = props.theme

  return (
    <div
      className={`${props.theme} flex flex-col gap-3 rounded-xl border bg-background p-4 text-foreground`}
      style={style}
      data-testid={`preview-${props.theme}`}
    >
      <p className="text-xs font-medium text-muted-foreground capitalize">{props.theme}</p>
      <div className="flex flex-wrap items-center gap-2">
        <Button size="sm" type="button">
          Book
        </Button>
        <Badge className="bg-brand-secondary text-brand-secondary-foreground">Confirmed</Badge>
        <a href="#preview" className="text-primary text-sm underline underline-offset-2">
          A link
        </a>
      </div>
    </div>
  )
}

function AssetDropzone(props: { kind: BrandingAssetKind; label: string; accept: string }) {
  const queryClient = useQueryClient()
  const branding = useBranding()
  const input = useRef<HTMLInputElement>(null)
  const [over, setOver] = useState(false)
  const url = props.kind === 'logo' ? branding.data?.logo_url : branding.data?.favicon_url

  const refresh = () => queryClient.invalidateQueries({ queryKey: BRANDING_DOCUMENT })
  const upload = useMutation({
    mutationFn: (file: File) => uploadBrandingAsset(props.kind, file),
    onSuccess: () => {
      refresh()
      toast.success(`${props.label} updated`)
    },
    onError: (error) => toast.error(error.message),
  })
  const remove = useMutation({
    mutationFn: () => removeBrandingAsset(props.kind),
    onSuccess: () => {
      refresh()
      toast.success(`${props.label} removed`)
    },
    onError: (error) => toast.error(error.message),
  })

  const drop = (event: DragEvent) => {
    event.preventDefault()
    setOver(false)
    const file = event.dataTransfer.files[0]
    if (file) upload.mutate(file)
  }

  return (
    <div className="flex flex-col gap-2">
      <Label htmlFor={`${props.kind}-file`}>{props.label}</Label>
      <div
        onDragOver={(e) => {
          e.preventDefault()
          setOver(true)
        }}
        onDragLeave={() => setOver(false)}
        onDrop={drop}
        className={`flex flex-col items-center gap-3 rounded-xl border border-dashed p-4 transition-colors duration-150 ${
          over ? 'border-primary bg-accent' : 'border-input'
        }`}
      >
        {url ? (
          <img src={url} alt={`${props.label} preview`} className="h-16 object-contain" />
        ) : (
          <span className="flex h-16 items-center text-xs text-muted-foreground">
            No {props.label.toLowerCase()} yet
          </span>
        )}
        <input
          ref={input}
          id={`${props.kind}-file`}
          type="file"
          accept={props.accept}
          className="sr-only"
          onChange={(e) => {
            const file = e.target.files?.[0]
            if (file) upload.mutate(file)
            e.target.value = ''
          }}
        />
        <div className="flex gap-2">
          <Button
            type="button"
            size="sm"
            variant="outline"
            disabled={upload.isPending}
            onClick={() => input.current?.click()}
          >
            <Upload aria-hidden />
            {upload.isPending ? 'Uploading…' : url ? 'Replace' : 'Upload'}
          </Button>
          {url && (
            <Button
              type="button"
              size="sm"
              variant="ghost"
              disabled={remove.isPending}
              onClick={() => remove.mutate()}
            >
              Remove
            </Button>
          )}
        </div>
      </div>
    </div>
  )
}
