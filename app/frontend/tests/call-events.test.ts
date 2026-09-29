import { afterEach, expect, test, vi } from 'vitest'
import { DemoCallEventSource } from '@/lib/call-events'

/**
 * S6: `DemoCallEventSource` away from the DOM (Phase 14, #16 decision #1) — the one seam a
 * real telephony transport would replace. `trigger()` asks `POST /api/cti/simulate-call` and
 * hands the answer to every current subscriber; `subscribe` returns its own unsubscribe.
 */

afterEach(() => vi.unstubAllGlobals())

const CALL = { call_id: 'call-1', phone: '4165550199', received_at: '2026-09-29T12:00:00Z' }

function stubSimulateCall() {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => Response.json(CALL)),
  )
}

test('trigger asks the server and hands the event to every subscriber', async () => {
  stubSimulateCall()
  const source = new DemoCallEventSource()
  const received: unknown[] = []
  source.subscribe((event) => received.push(event))

  const returned = await source.trigger()

  expect(returned).toEqual(CALL)
  expect(received).toEqual([CALL])
})

test('several subscribers all hear the same call', async () => {
  stubSimulateCall()
  const source = new DemoCallEventSource()
  const a: unknown[] = []
  const b: unknown[] = []
  source.subscribe((event) => a.push(event))
  source.subscribe((event) => b.push(event))

  await source.trigger()

  expect(a).toEqual([CALL])
  expect(b).toEqual([CALL])
})

test('unsubscribing stops that listener from hearing later calls', async () => {
  stubSimulateCall()
  const source = new DemoCallEventSource()
  const received: unknown[] = []
  const unsubscribe = source.subscribe((event) => received.push(event))

  unsubscribe()
  await source.trigger()

  expect(received).toEqual([])
})

test('a failed simulate call rejects rather than notifying subscribers with nothing', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => Response.json({ detail: 'Not found.' }, { status: 404 })),
  )
  const source = new DemoCallEventSource()
  const received: unknown[] = []
  source.subscribe((event) => received.push(event))

  await expect(source.trigger()).rejects.toThrow()
  expect(received).toEqual([])
})
