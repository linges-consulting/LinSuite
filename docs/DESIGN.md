# LinSuite design system

A dense operational tool — calendars, tables, forms — used all day by staff. It should feel
crisp and quiet, not like a marketing site. Source of truth for tokens: `app/frontend/src/index.css`.

## Tokens

Three layers. Components only ever use layer 2 via Tailwind utilities (layer 3).

**1. Brand (white-label)** — plain hex, the only values a business changes. Set at runtime on
`<html style="--brand-primary: #...">`; everything else follows.

| Token | Default | Used for |
|---|---|---|
| `--brand-primary` / `-foreground` | `#1d4ed8` / `#fff` | primary buttons, active states, focus ring, links |
| `--brand-primary-dark` / `-foreground` | `#60a5fa` / `#0f172a` | the same roles on dark surfaces (a dark brand colour is unreadable on slate-950) |
| `--brand-secondary` / `-foreground` | `#0f766e` / `#fff` | accents: calendar highlights, secondary chart series. Never buttons. |
| `--brand-secondary-dark` / `-foreground` | `#68b5ac` / `#0f172a` | the same, on dark surfaces |

A business sets only `--brand-primary` and `--brand-secondary`. The other four are **derived**,
server-side, in `app/backend/src/settings/branding.py` — one implementation, which the Branding
screen's live preview asks for rather than re-deriving in TypeScript.

- **The dark variant** raises the colour's Oklab lightness to `L = 0.72` and keeps 90% of its
  chroma. Oklab because its lightness axis is perceptually uniform: scaling `#rrggbb` toward
  `#ffffff` in sRGB shifts hue (blues go violet, reds go pink), and the result is a different
  colour rather than a lighter one. Chroma is pulled back slightly because full saturation at
  that lightness reads as neon. `#1d4ed8` derives to `#659dff`, against the `#60a5fa` that was
  chosen by hand for the default — close enough that the rule and the taste agree.
- **The foreground** is whichever of `#ffffff` and `#0f172a` has the higher WCAG contrast on
  the colour.
- **Contrast is reported, never enforced.** The Branding screen shows the ratio of text on each
  brand surface in both themes and warns below 4.5:1 — and saves anyway. It is the business's
  own brand; an application that refused a logo colour would be wrong about whose decision that is.

Two indirections make the white-label swap free: `--primary` resolves to `--brand-primary` on
`:root`/`.light` and to `--brand-primary-dark` under `.dark`, and `--accent-brand` does the same
for the secondary. Components use `bg-primary` / `bg-brand-secondary` and never know which theme
they are in. `.light` exists so a nested element — the Branding preview panel — can carry the
light palette inside a dark page, exactly as `.dark` already did the reverse.

**2. Semantic** — `background`, `foreground`, `card`, `popover`, `primary`, `secondary` (neutral, *not* brand),
`muted`, `accent`, `border`, `input`, `ring`, `sidebar-*`, and status: `destructive`, `success`, `warning`, `info`.
Neutrals are Tailwind **slate** (cool grey pairs with blue). Light: page `slate-50`, cards white, text `slate-900`,
muted text `slate-600` (7:1). Dark: page `slate-950`, cards `slate-900`, text `slate-100`, muted `slate-400`.

**3. Utilities** — `bg-primary`, `text-muted-foreground`, `border-input`, `bg-success/10 text-success`, etc.

Rules: never a raw colour in a component. Status is never colour-only — pair with a label or icon.
Persisted session-note annotation colours are record data, independent of theme and branding. Their named palette lives in `src/lib/note-diagrams.ts`; components render each recorded colour alongside annotation text and type.
Dark mode is designed with light, not derived from it; check contrast in both.

## Typography

**One family, deliberately.** Inter Variable, self-hosted (`@fontsource-variable/inter`; on-prem installs
cannot depend on Google Fonts), for headings, body and data alike. A display/body pairing earns its keep on
marketing pages; in a tool where the largest text is an 18px page title and most of the screen is a 14px
table, a second face adds a download and a seam without adding hierarchy — weight and size do that work.
Inter's `cv11`/`ss01` alternates and tabular figures are enabled globally.

`--font-heading` is a real token (shadcn's `Card`, `Dialog` titles use `font-heading`) and resolves to
`--font-sans`. It stays as the single place a pairing would be introduced — e.g. a tenant's brand face —
so that decision never requires touching components. Do not point it at a different family without a
DESIGN.md update.

Body is **14px** (`text-sm`) — this is a desktop tool; 16px is for marketing. Inputs stay ≥16px on mobile widths.

| Role | Class |
|---|---|
| Page title (top bar) | `text-base font-semibold tracking-tight` |
| Section / card title | `text-base font-medium` |
| Body, table cells, nav | `text-sm` (14) |
| Labels, captions, badges | `text-xs font-medium` (12) |
| Numbers in tables, prices, times | tabular by default (`table`, `time`, `[data-numeric]`) |

Weight carries hierarchy: 600 titles, 500 labels/nav, 400 body. No display sizes above 24px anywhere in the app.

## Spacing, radius, elevation

- Tailwind 4px scale. Page gutter `p-4 md:p-6`. Card padding 16. Table row 40px. Nav item 36px. Top bar and sidebar header 56px.
- `--radius: 6px`. Buttons/inputs `rounded-lg` (6), cards `rounded-xl` (~8), badges pill. Nothing rounder.
- Elevation is a `ring-1 ring-foreground/10` or a border, never a drop shadow, except popovers/dialogs (shadcn defaults).
- Reading width for prose/empty states `max-w-3xl`; data views are full width.

## Layout

Sidebar 240px (`w-60`) with brand mark + name, primary nav (icon + label, active = `bg-sidebar-accent`), collapses to an
overlay drawer below `md` with a `bg-black/50` scrim. Top bar: page `h1` left, actions right. Content in `<main>`.
One primary action per screen. Destructive actions sit apart from primary ones and confirm first.

## Components

shadcn/ui (Radix, `radix-nova` preset) in `src/components/ui/` — vendored, edit freely but keep the API.
Installed: button, input, label, card, dialog, dropdown-menu, table, sonner (toasts), tabs, badge, skeleton,
popover, command (+ its input-group/textarea dependencies — the searchable-list half of a combobox).
Add more with `npx shadcn@latest add <name>` from `app/frontend`. The generator writes
`import { cn } from "cn"` — repoint it at `@/lib/utils` and do **not** install the `cn`
package to make the error go away: it is a real package (the CLI's own dependency) that
resolves to something else entirely. `tests/dependencies.test.ts` fails if either half
comes back.

- `EmptyState` (`src/components/empty-state.tsx`) for every empty list/table: icon, title, one sentence, optional action.
- `Badge` variants for status: `success`, `warning`, `info`, `destructive` (tinted), `secondary`/`outline` for neutral tags.
- Icons: lucide only, `size-4` inline, `size-5` in empty states. Icon-only buttons need `aria-label`. No emoji.
- Loading > 300ms: `Skeleton` sized like the content, never a page-blocking spinner. Buttons disable while pending.
- Forms: visible `Label` per field, error text below the field, validate on blur.
- Toasts (`sonner`) for outcomes of actions; never for validation errors.

## Motion

Purpose or nothing. Micro-interactions 150–200ms with the strengthened `ease-out`
(`cubic-bezier(0.23,1,0.32,1)`); popovers scale from their trigger; exits faster than enters.
Never animate keyboard-driven or high-frequency actions (calendar navigation, command palette). Only `transform`/`opacity`.
`prefers-reduced-motion` collapses all durations globally.

## Do / don't

- Do keep tables dense: 40px rows, 13–14px text, tabular numbers, right-aligned money.
- Do use `text-muted-foreground` for secondary text — and only that.
- Don't introduce a colour, radius, shadow or font size outside this file.
- Don't use gradients, glass, or decorative illustration. The brand colour is the only accent.
- Don't build a new primitive when a shadcn one exists; don't fork shadcn components into variants without a second consumer.
