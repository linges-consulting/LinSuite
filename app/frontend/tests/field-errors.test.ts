import { expect, test } from 'vitest'
import { fieldErrors } from '@/lib/api'

/**
 * `fieldErrors` is what every edit form (Settings → Business, the client edit dialog) uses
 * to put a FastAPI 422 under the field it is about. Pydantic v2 prefixes a custom
 * `field_validator`'s `ValueError` message with "Value error, " — that is the exception's
 * type, not a word anybody wrote, and a form that showed it verbatim would read as a bug
 * report rather than "date of birth cannot be in the future."
 */

function fakeError(detail: unknown) {
  return { body: { detail } }
}

test('a field error from a custom validator has "Value error, " stripped', () => {
  const errors = fieldErrors(
    fakeError([
      {
        loc: ['body', 'date_of_birth'],
        msg: 'Value error, date of birth cannot be in the future',
        type: 'value_error',
      },
    ]),
  )

  expect(errors).toEqual({ date_of_birth: 'date of birth cannot be in the future' })
})

test('a field error with no "Value error, " prefix is left exactly as sent', () => {
  const errors = fieldErrors(
    fakeError([
      { loc: ['body', 'vip_visit_threshold'], msg: 'Input should be greater than or equal to 2' },
    ]),
  )

  expect(errors).toEqual({ vip_visit_threshold: 'Input should be greater than or equal to 2' })
})

test('a non-array detail (a plain string, from our own 409s) maps to no field', () => {
  expect(fieldErrors(fakeError('A customer with that email already exists.'))).toEqual({})
})

test('a loc outside the body (a path or query param) is not treated as a field', () => {
  const errors = fieldErrors(
    fakeError([{ loc: ['path', 'customer_id'], msg: 'Value error, not a valid UUID' }]),
  )

  expect(errors).toEqual({})
})
