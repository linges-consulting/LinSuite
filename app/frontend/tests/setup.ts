import '@testing-library/jest-dom/vitest'

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
