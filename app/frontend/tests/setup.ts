import '@testing-library/jest-dom/vitest'
import { configure } from '@testing-library/dom'

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
