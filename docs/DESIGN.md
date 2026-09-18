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

**2. Semantic** — `background`, `foreground`, `card`, `popover`, `primary`, `secondary` (neutral, *not* brand),
`muted`, `accent`, `border`, `input`, `ring`, `sidebar-*`, and status: `destructive`, `success`, `warning`, `info`.
Neutrals are Tailwind **slate** (cool grey pairs with blue). Light: page `slate-50`, cards white, text `slate-900`,
muted text `slate-600` (7:1). Dark: page `slate-950`, cards `slate-900`, text `slate-100`, muted `slate-400`.

**3. Utilities** — `bg-primary`, `text-muted-foreground`, `border-input`, `bg-success/10 text-success`, etc.

Rules: never a raw colour in a component. Status is never colour-only — pair with a label or icon.
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
Installed: button, input, label, card, dialog, dropdown-menu, table, sonner (toasts), tabs, badge, skeleton.
Add more with `npx shadcn@latest add <name>` from `app/frontend`.

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
