import qrcode from 'qrcode-generator'

/**
 * The enrolment QR code, drawn in the browser from the `otpauth://` URI.
 *
 * Client-side rather than an image endpoint on the API: an endpoint that rendered this would
 * be a second place the TOTP secret is served from, and one that a proxy, a browser cache or
 * a screenshot tool could hold onto after the enrolment screen is gone.
 *
 * `qrcode-generator` is the encoder and nothing else — no DOM, no canvas, ~20KB — so the
 * markup is ours. One `<path>` of module squares rather than a few hundred `<rect>`s, and
 * `currentColor` so it inverts with the theme instead of vanishing on a dark surface.
 */
export function QrCode({ value, label, className = 'size-44' }: { value: string; label: string; className?: string }) {
  // Type 0 = smallest version that fits; M is the error correction every authenticator reads.
  const qr = qrcode(0, 'M')
  qr.addData(value)
  qr.make()

  const count = qr.getModuleCount()
  const quiet = 2 // the margin the spec asks for, so a scanner can find the edges
  const size = count + quiet * 2

  let path = ''
  for (let row = 0; row < count; row++) {
    for (let col = 0; col < count; col++) {
      if (qr.isDark(row, col)) path += `M${col + quiet} ${row + quiet}h1v1h-1z`
    }
  }

  return (
    <svg
      role="img"
      aria-label={label}
      viewBox={`0 0 ${size} ${size}`}
      className={`${className} rounded-lg bg-background p-1 ring-1 ring-foreground/10`}
      shapeRendering="crispEdges"
    >
      <path d={path} fill="currentColor" />
    </svg>
  )
}
