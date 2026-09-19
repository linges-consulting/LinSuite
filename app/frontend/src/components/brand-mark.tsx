import { useBranding } from '@/lib/branding'

/**
 * The business's mark and name, in the sidebar and above the sign-in card.
 *
 * **The logo is a fixed height, not a fixed box.** Most business logos are wordmarks — wider
 * than they are tall — and a 24x24 square with `object-contain` renders one at about 24x12,
 * which is unreadable. The stored image is up to 512 px on its longest side, so the pixels
 * are there; only the box was wrong. Height is pinned to the 24 px the sidebar row allows and
 * the width follows, up to the 240 px sidebar's `max-w-32` so a very wide mark cannot push
 * the name out.
 *
 * The square is the fallback rather than a placeholder image: it is already the brand colour,
 * so an instance that never uploads a logo still looks deliberate.
 */
export function BrandMark({ className }: { className?: string }) {
  const { data } = useBranding()
  const name = data?.name ?? 'LinSuite'
  return (
    <span className={className}>
      {data?.logo_url ? (
        <img
          src={data.logo_url}
          alt=""
          className="h-6 w-auto max-w-32 shrink-0 rounded-md object-contain"
        />
      ) : (
        <span aria-hidden className="size-6 shrink-0 rounded-md bg-primary" />
      )}
      <span className="truncate">{name}</span>
    </span>
  )
}
