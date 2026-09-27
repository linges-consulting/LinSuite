import { render, screen } from '@testing-library/react'
import { useRef } from 'react'
import { MemoryRouter } from 'react-router'
import { afterEach, expect, test, vi } from 'vitest'
import { EMBED_RESIZE_MESSAGE, useEmbedMode, useEmbedResize } from '@/lib/embed'

/**
 * `lib/embed.ts` (Phase 6 Task 5, #10): the `?embed=1` reader and the resize-to-parent
 * mechanism `/book` (Task 6) will use. Neither is exercised against a real `/book` page here —
 * it doesn't exist yet — just the mechanism itself, against a probe component.
 */

class FakeResizeObserver {
  static instances: FakeResizeObserver[] = []
  callback: ResizeObserverCallback
  observe = vi.fn()
  unobserve = vi.fn()
  disconnect = vi.fn()
  constructor(callback: ResizeObserverCallback) {
    this.callback = callback
    FakeResizeObserver.instances.push(this)
  }
}

afterEach(() => {
  FakeResizeObserver.instances = []
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

function ResizeProbe({ embed }: { embed: boolean }) {
  const ref = useRef<HTMLDivElement>(null)
  useEmbedResize(ref, embed)
  return <div ref={ref} />
}

function fireResize(height: number) {
  const [observer] = FakeResizeObserver.instances
  observer.callback(
    [{ contentRect: { height } } as ResizeObserverEntry],
    observer as unknown as ResizeObserver,
  )
}

test('in embed mode, a height change posts linsuite:resize to the parent window', () => {
  vi.stubGlobal('ResizeObserver', FakeResizeObserver)
  const post = vi.spyOn(window.parent, 'postMessage')

  render(<ResizeProbe embed />)
  expect(FakeResizeObserver.instances).toHaveLength(1)

  fireResize(420)
  expect(post).toHaveBeenCalledWith({ type: EMBED_RESIZE_MESSAGE, height: 420 }, '*')
})

test('an unchanged height does not post again', () => {
  vi.stubGlobal('ResizeObserver', FakeResizeObserver)
  const post = vi.spyOn(window.parent, 'postMessage')

  render(<ResizeProbe embed />)
  fireResize(420)
  fireResize(420)

  expect(post).toHaveBeenCalledTimes(1)
})

test('outside embed mode, nothing observes and nothing posts', () => {
  vi.stubGlobal('ResizeObserver', FakeResizeObserver)
  const post = vi.spyOn(window.parent, 'postMessage')

  render(<ResizeProbe embed={false} />)

  expect(FakeResizeObserver.instances).toHaveLength(0)
  expect(post).not.toHaveBeenCalled()
})

function ModeProbe() {
  return <div data-testid="mode">{String(useEmbedMode())}</div>
}

test('useEmbedMode is true for ?embed=1', () => {
  render(
    <MemoryRouter initialEntries={['/book?embed=1']}>
      <ModeProbe />
    </MemoryRouter>,
  )
  expect(screen.getByTestId('mode')).toHaveTextContent('true')
})

test('useEmbedMode is false without the param', () => {
  render(
    <MemoryRouter initialEntries={['/book']}>
      <ModeProbe />
    </MemoryRouter>,
  )
  expect(screen.getByTestId('mode')).toHaveTextContent('false')
})
