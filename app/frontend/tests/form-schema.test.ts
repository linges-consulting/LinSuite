import { describe, expect, it } from 'vitest'
import cases from '../../shared/form-schema-cases.json'
import {
  keptAnswers,
  schemaProblems,
  validateAnswers,
  visibleKeys,
  type FormSchema,
} from '@/lib/forms'

/**
 * `lib/forms.ts` is the twin of the backend's `forms/schema.py`: the builder's preview and
 * the fill page run these rules in the browser, the server runs them again at the trust
 * boundary. Both suites read the same fixture, so the two cannot drift apart silently.
 */

const base = cases.base as FormSchema

describe('schema cases', () => {
  for (const c of cases.schemas) {
    it(c.name, () => {
      const problems = schemaProblems(c.schema as unknown as FormSchema)
      if (c.valid) expect(problems).toEqual([])
      else expect(problems).not.toEqual([])
    })
  }
})

describe('answer cases', () => {
  for (const c of cases.answers) {
    it(c.name, () => {
      expect(visibleKeys(base, c.answers)).toEqual(c.visible)
      expect(Object.keys(keptAnswers(base, c.answers))).toEqual(c.kept)
      expect(validateAnswers(base, c.answers)).toEqual(c.errors)
    })
  }
})
