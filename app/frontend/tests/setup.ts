import '@testing-library/jest-dom/vitest'
import { configure } from '@testing-library/dom'
import { afterEach } from 'vitest'

// Testing Library's default `findBy*` budget is 1000 ms, which is a wall clock, not a
// measure of work. Under parallel jsdom workers a test paying a cold app import (and, in
// `mfa.test.tsx`, QR generation) can cross it without anything being wrong — the flake the
// M1 ledger recorded. Five seconds is still short enough that a genuinely stuck assertion
// fails the run rather than hanging it.
configure({ asyncUtilTimeout: 5000 })

// jsdom has neither; cmdk observes its list, the theme provider reads the colour scheme.
globalThis.ResizeObserver ??= class {
  observe() {}
  unobserve() {}
  disconnect() {}
}
Element.prototype.scrollIntoView ??= () => {}

// jsdom implements no Pointer Events API at all, and Radix's menus/popovers open on
// `pointerdown` and capture the pointer while open. Without these they simply never open.
Element.prototype.hasPointerCapture ??= () => false
Element.prototype.setPointerCapture ??= () => {}
Element.prototype.releasePointerCapture ??= () => {}


window.matchMedia ??= (query: string) =>
  ({
    matches: false,
    media: query,
    addEventListener() {},
    removeEventListener() {},
  }) as unknown as MediaQueryList

// jsdom does not clear `document.activeElement` when the node holding focus is unmounted —
// it keeps pointing at the now-detached element. Found writing Phase 6 Task 6's booking
// tests (#10): a Radix `Select` rendered outside a `Dialog` (this repo's existing Select
// tests all happen to be inside one) leaves the trigger button focused; the *next* test's
// fresh render, if its own trigger shares that element's id (as two renders of the same
// component naturally do), then fails to open at all — Radix's own focus bookkeeping gets
// confused by a stale `activeElement` from a different, disconnected tree. Blurring
// whatever is still "focused" after every test is the general fix, not a booking-specific
// one: any test suite exercising a page-level (non-dialog) `Select`/`Popover` more than
// once per file would hit the same thing.
afterEach(() => {
  if (document.activeElement instanceof HTMLElement) document.activeElement.blur()
})
