import { useBranding } from '@/lib/branding'

/**
 * The business's mark and name, in the sidebar and above the sign-in card. The square is the
 * fallback rather than a placeholder image: it is already the brand colour, so an instance
 * that never uploads a logo still looks deliberate.
 */
export function BrandMark({ className }: { className?: string }) {
  const { data } = useBranding()
  const name = data?.name ?? 'LinSuite'
  return (
    <span className={className}>
      {data?.logo_url ? (
        <img src={data.logo_url} alt="" className="size-6 shrink-0 rounded-md object-contain" />
      ) : (
        <span aria-hidden className="size-6 shrink-0 rounded-md bg-primary" />
      )}
      <span className="truncate">{name}</span>
    </span>
  )
}
