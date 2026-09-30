import { expect, test } from 'vitest'
import { initialsOf } from '@/lib/initials'

test('initials come from the first letters of the first two words', () => {
  expect(initialsOf('Cedar Lane Clinic')).toBe('CL')
  expect(initialsOf('stillwater massage')).toBe('SM')
})

test('a single word gives one initial', () => {
  expect(initialsOf('Stillwater')).toBe('S')
})

test('punctuation and symbols are skipped', () => {
  expect(initialsOf('Smith & Jones Physio')).toBe('SJ')
  expect(initialsOf("  O'Brien's   Wellness ")).toBe('OW')
  expect(initialsOf('123 Main St')).toBe('1M')
})

test('an empty name gives no initials', () => {
  expect(initialsOf('')).toBe('')
  expect(initialsOf('  & ')).toBe('')
})
