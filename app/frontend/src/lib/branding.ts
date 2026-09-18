import { useQuery } from '@tanstack/react-query'
import { useEffect } from 'react'
import { fetchBrandingDocument, type BrandingDocument } from '@/lib/api'
import { BRANDING_DOCUMENT } from '@/lib/query-keys'

/**
 * The white-label seam, and it is three lines of DOM.
 *
 * `docs/DESIGN.md` layer 1 is a handful of plain-hex CSS variables that everything else
 * resolves from, so becoming this business is setting those variables on `<html>` — no
 * per-screen work, no theme object, nothing to keep in sync. The defaults in `index.css`
 * are what renders until this answers, which is why an instance with no branding set still
 * looks finished.
 *
 * The last answer is kept in `localStorage` and applied before React mounts, so a return
 * visit paints the business's own colours rather than the defaults and then a flash. It is a
 * cache of a public document and nothing more; the request still goes out and wins.
 */

const CACHE = 'branding'

export function applyBranding(document_: BrandingDocument): void {
  const root = document.documentElement
  for (const [name, value] of Object.entries(document_.colors ?? {})) {
    root.style.setProperty(`--brand-${name.replaceAll('_', '-')}`, value)
  }
  if (document_.name) document.title = document_.name
  if (document_.favicon_url) {
    // Created rather than assumed: the tab icon has to be swappable wherever the document
    // came from, and a missing `<link>` would silently leave the stock mark in place.
    let icon = document.querySelector<HTMLLinkElement>('link[rel="icon"]')
    if (!icon) {
      icon = document.createElement('link')
      icon.rel = 'icon'
      document.head.append(icon)
    }
    icon.type = 'image/png'
    icon.href = document_.favicon_url
  }
}

/** Called from `main.tsx` before the first render. Never throws: a broken cache is no cache. */
export function applyCachedBranding(): void {
  try {
    const cached = localStorage.getItem(CACHE)
    if (cached) applyBranding(JSON.parse(cached))
  } catch {
    /* private mode, cleared storage, or a cache this version cannot read */
  }
}

export function useBranding() {
  return useQuery({
    queryKey: BRANDING_DOCUMENT,
    queryFn: fetchBrandingDocument,
    staleTime: Infinity,
    retry: false,
  })
}

/** Called once, at the top of the app. Renders nothing; it only dresses the document. */
export function useApplyBranding(): void {
  const { data } = useBranding()
  useEffect(() => {
    if (!data) return
    applyBranding(data)
    try {
      localStorage.setItem(CACHE, JSON.stringify(data))
    } catch {
      /* the branding still applied; only the head start on the next load is lost */
    }
  }, [data])
}
