import '@testing-library/jest-dom/vitest'

// jsdom has no matchMedia; the theme provider needs it.
window.matchMedia ??= (query: string) =>
  ({
    matches: false,
    media: query,
    addEventListener() {},
    removeEventListener() {},
  }) as unknown as MediaQueryList
