import { describe, expect, test } from 'vitest'
import { contrast, readableOn } from '@/lib/calendar/contrast'
import { pack } from '@/lib/calendar/packing'
import {
  DAY_PX,
  addDays,
  instantAt,
  localDate,
  placement,
  wallMinutes,
} from '@/lib/calendar/pixels'
import { snap, snapEnd, snapStart } from '@/lib/calendar/snap'

/**
 * S6: the calendar's arithmetic, away from the DOM. Packing is the spec's zero-overlap
 * rule; pixels is the wall-clock mapping on the two DST days; snap is the grid.
 */

const TORONTO = 'America/Toronto'

describe('packing', () => {
  const at = (id: string, start: number, end: number) => ({ id, start, end })

  test('two simultaneous events are 50 % each, side by side', () => {
    const laid = pack([at('a', 540, 600), at('b', 540, 600)])
    expect(laid.get('a')).toEqual({ column: 0, columns: 2 })
    expect(laid.get('b')).toEqual({ column: 1, columns: 2 })
  })

  test('three are 33 % each', () => {
    const laid = pack([at('a', 540, 600), at('b', 555, 615), at('c', 570, 630)])
    expect([...laid.values()].map((p) => p.columns)).toEqual([3, 3, 3])
    expect([...laid.values()].map((p) => p.column).sort()).toEqual([0, 1, 2])
  })

  test('a chain reuses a freed column: three events, two abreast', () => {
    const laid = pack([at('a', 540, 600), at('b', 570, 630), at('c', 600, 660)])
    expect(laid.get('a')).toEqual({ column: 0, columns: 2 })
    expect(laid.get('b')).toEqual({ column: 1, columns: 2 })
    expect(laid.get('c')).toEqual({ column: 0, columns: 2 })
  })

  test('neighbours that only touch are full width', () => {
    const laid = pack([at('a', 540, 600), at('b', 600, 660)])
    expect(laid.get('a')).toEqual({ column: 0, columns: 1 })
    expect(laid.get('b')).toEqual({ column: 0, columns: 1 })
  })

  test('the long one takes the left column and a later cluster starts afresh', () => {
    const laid = pack([at('short', 540, 570), at('long', 540, 720), at('later', 800, 860)])
    expect(laid.get('long')).toEqual({ column: 0, columns: 2 })
    expect(laid.get('short')).toEqual({ column: 1, columns: 2 })
    expect(laid.get('later')).toEqual({ column: 0, columns: 1 })
  })

  test('order of input does not matter and nothing is dropped', () => {
    const events = [at('c', 600, 660), at('a', 540, 600), at('b', 570, 630)]
    expect(pack(events).size).toBe(3)
    expect(pack(events)).toEqual(pack([...events].reverse()))
  })
})

describe('snapping', () => {
  test('rounds to the nearest step inside the day', () => {
    expect(snap(607, 15)).toBe(600)
    expect(snap(608, 15)).toBe(615)
    expect(snap(-4, 15)).toBe(0)
    expect(snap(1439, 15)).toBe(1440)
  })

  test('a moved start keeps the whole event inside the day', () => {
    expect(snapStart(600, 7, 60, 15)).toBe(600)
    expect(snapStart(600, 8, 60, 15)).toBe(615)
    expect(snapStart(1380, 120, 60, 15)).toBe(1380)
    expect(snapStart(10, -100, 60, 15)).toBe(0)
  })

  test('a pulled end never comes closer than one step to the start', () => {
    expect(snapEnd(600, 660, 7, 15)).toBe(660)
    expect(snapEnd(600, 660, 8, 15)).toBe(675)
    expect(snapEnd(600, 660, -200, 15)).toBe(615)
    expect(snapEnd(600, 1430, 100, 15)).toBe(1440)
  })
})

describe('pixels', () => {
  test('one pixel is one wall-clock minute in the business zone', () => {
    // 14:00Z on a summer day is 10:00 in Toronto.
    expect(wallMinutes(new Date('2026-06-15T14:00:00Z'), TORONTO)).toBe(600)
    expect(localDate(new Date('2026-06-15T02:00:00Z'), TORONTO)).toBe('2026-06-14')
    expect(instantAt('2026-06-15', 600, TORONTO).toISOString()).toBe('2026-06-15T14:00:00.000Z')
    expect(instantAt('2026-06-15', 1440, TORONTO).toISOString()).toBe('2026-06-16T04:00:00.000Z')
    expect(addDays('2026-02-28', 1)).toBe('2026-03-01')
    expect(addDays('2026-01-01', -1)).toBe('2025-12-31')
  })

  test('spring forward: 03:00 sits on the 03:00 line, and 02:30 resolves an hour on', () => {
    // 2026-03-08, America/Toronto. 06:59Z is 01:59 EST; 07:00Z is 03:00 EDT.
    expect(wallMinutes(new Date('2026-03-08T06:59:00Z'), TORONTO)).toBe(119)
    expect(wallMinutes(new Date('2026-03-08T07:00:00Z'), TORONTO)).toBe(180)
    expect(instantAt('2026-03-08', 180, TORONTO).toISOString()).toBe('2026-03-08T07:00:00.000Z')
    expect(wallMinutes(instantAt('2026-03-08', 150, TORONTO), TORONTO)).toBe(210)
    // 09:00 is 09:00 whatever the offset did overnight.
    expect(wallMinutes(instantAt('2026-03-08', 540, TORONTO), TORONTO)).toBe(540)
  })

  test('fall back: both 01:30s sit on the 01:30 line, and the afternoon is unmoved', () => {
    // 2026-11-01, America/Toronto. 05:30Z is 01:30 EDT; 06:30Z is 01:30 EST.
    expect(wallMinutes(new Date('2026-11-01T05:30:00Z'), TORONTO)).toBe(90)
    expect(wallMinutes(new Date('2026-11-01T06:30:00Z'), TORONTO)).toBe(90)
    expect(instantAt('2026-11-01', 840, TORONTO).toISOString()).toBe('2026-11-01T19:00:00.000Z')
    expect(wallMinutes(instantAt('2026-11-01', 840, TORONTO), TORONTO)).toBe(840)
  })

  test('placement clips to the day', () => {
    const start = new Date('2026-06-15T14:00:00Z')
    const end = new Date('2026-06-15T15:30:00Z')
    expect(placement(start, end, '2026-06-15', TORONTO)).toEqual({ top: 600, height: 90 })
    expect(placement(start, end, '2026-06-16', TORONTO)).toBeNull()
    // Overnight time off: 22:00 on the 15th to 02:00 on the 16th.
    const late = new Date('2026-06-16T02:00:00Z')
    const early = new Date('2026-06-16T06:00:00Z')
    expect(placement(late, early, '2026-06-15', TORONTO)).toEqual({ top: 1320, height: 120 })
    expect(placement(late, early, '2026-06-16', TORONTO)).toEqual({ top: 0, height: 120 })
    // An all-day span ending at midnight ends at the bottom of its day, not the next one.
    const midnight = new Date('2026-06-16T04:00:00Z')
    expect(placement(late, midnight, '2026-06-15', TORONTO)).toEqual({ top: 1320, height: 120 })
    expect(placement(late, midnight, '2026-06-16', TORONTO)).toBeNull()
    expect(DAY_PX).toBe(1440)
  })
})

describe('contrast', () => {
  test('white on the palette blues and near-black on the lifted dark variants', () => {
    expect(readableOn('#1d4ed8')).toBe('#ffffff')
    expect(readableOn('#659dff')).toBe('#0f172a')
    expect(readableOn('#ffffff')).toBe('#0f172a')
    expect(contrast('#000000', '#ffffff')).toBeCloseTo(21, 5)
    expect(contrast('#1d4ed8', '#ffffff')).toBeGreaterThan(4.5)
  })
})
