import { useEffect, useRef } from 'react'
import { useLocation } from 'react-router'

/**
 * `?embed=1` — the convention a page carries when a business has iframed it into their own
 * site (Phase 6 Task 5, #10). `/book` (Task 6) is mounted outside the app shell entirely, the
 * same way `/f/*` already is (`App.tsx`) — there is no nav/header wrapping it to strip, so this
 * flag is not about page chrome in the CSS sense. It only toggles page-owned elements that make
 * sense standalone but not embedded in someone else's page — a "powered by" line, a link back
 * to the marketing site, anything that would otherwise duplicate the parent site's own chrome.
 */
export function useEmbedMode(): boolean {
  const { search } = useLocation()
  return new URLSearchParams(search).get('embed') === '1'
}

/** The `postMessage` a parent site's iframe listens to for auto-sizing. */
export const EMBED_RESIZE_MESSAGE = 'linsuite:resize'

/**
 * While in embed mode, observes `ref`'s content height and posts
 * `{type: 'linsuite:resize', height}` to `window.parent` whenever it changes, so the embedding
 * page can grow or shrink the iframe to fit. A no-op outside embed mode — there is no parent
 * iframe to size. `targetOrigin` is `'*'`: the embedding site's origin is whatever business
 * chose to paste the snippet, never known in advance, the same trade-off Calendly-style
 * booking widgets make.
 */
export function useEmbedResize(ref: React.RefObject<HTMLElement | null>, embed: boolean): void {
  const lastHeight = useRef<number | null>(null)

  useEffect(() => {
    if (!embed) return
    const el = ref.current
    if (!el) return

    const post = (height: number) => {
      if (height === lastHeight.current) return
      lastHeight.current = height
      window.parent.postMessage({ type: EMBED_RESIZE_MESSAGE, height }, '*')
    }

    const observer = new ResizeObserver((entries) => {
      const entry = entries[0]
      if (entry) post(Math.ceil(entry.contentRect.height))
    })
    observer.observe(el)
    return () => observer.disconnect()
  }, [ref, embed])
}
