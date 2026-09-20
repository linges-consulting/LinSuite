/**
 * Which of the design system's two foregrounds reads on a colour (tech-stack §13: "contrast
 * is computed, not assumed"). WCAG 2 relative luminance, the same arithmetic
 * `settings/branding.py` uses server-side — here for the event cards, which are painted in
 * whichever of the staff colour's two hexes the theme wants and cannot ask the server per card.
 */

export const WHITE = '#ffffff'
/** slate-900, `--foreground` in the light theme. */
export const NEAR_BLACK = '#0f172a'

function luminance(hex: string): number {
  const channel = (i: number) => {
    const c = parseInt(hex.slice(i, i + 2), 16) / 255
    return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4
  }
  return 0.2126 * channel(1) + 0.7152 * channel(3) + 0.0722 * channel(5)
}

/** WCAG contrast ratio between two `#rrggbb` colours, 1..21. */
export function contrast(a: string, b: string): number {
  const [light, dark] = [luminance(a), luminance(b)].sort((x, y) => y - x)
  return (light + 0.05) / (dark + 0.05)
}

/** The foreground with the higher contrast on `background`. */
export function readableOn(background: string): string {
  return contrast(background, WHITE) >= contrast(background, NEAR_BLACK) ? WHITE : NEAR_BLACK
}
