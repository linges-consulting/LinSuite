/**
 * Dollars on the screen, integer cents on the wire.
 *
 * **Money is integer cents everywhere** (CLAUDE.md, tech-stack §21), and this is the one
 * place the two representations meet. It converts from the *string* a person typed rather
 * than from a `number`, because `Math.round(12.005 * 100)` is 1200: the binary double
 * nearest 12.005 is a hair below it, so the half-up rule the tax rounding depends on would
 * quietly round the wrong way on exactly the values it exists for.
 */

/** Cents as a plain decimal string — no symbol, so a caller can put the currency where the
 *  business's locale wants it. Always two decimal places. */
export function centsToDollars(cents: number): string {
  return (cents / 100).toFixed(2)
}

/**
 * The cents a typed amount means, or `null` when it is not an amount at all.
 *
 * Null rather than a throw or a zero: an empty box and "12..4" are both "the form cannot be
 * submitted yet", and a zero would silently price a service at nothing. A leading `-` is not
 * accepted — a discount is a line on an invoice, not a service that costs less than nothing —
 * so the refusal happens here rather than as a 422 after a round trip.
 *
 * Rounds half **up** on the third decimal, matching the rule invoices round tax by.
 */
export function dollarsToCents(typed: string): number | null {
  const cleaned = typed.trim().replace(/[$,\s]/g, '')
  const parts = /^(\d*)(?:\.(\d*))?$/.exec(cleaned)
  if (!parts || (!parts[1] && !parts[2])) return null

  const whole = Number(parts[1] || '0')
  // Padded so a bare ".5" and a "12.5" both have a hundredths and a thousandths digit to
  // read, and anything past the third is already below half a cent.
  const fraction = (parts[2] ?? '').padEnd(3, '0')
  const cents = whole * 100 + Number(fraction.slice(0, 2))
  return Number(fraction[2]) >= 5 ? cents + 1 : cents
}
