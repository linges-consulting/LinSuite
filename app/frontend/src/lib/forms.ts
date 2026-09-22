/**
 * The form schema and its answer rules — the browser twin of the backend's `forms/schema.py`.
 *
 * The builder's live preview and (Task 4) the fill page run these; the server runs its own
 * copy again at the trust boundary. Both test suites read `app/shared/form-schema-cases.json`,
 * so a rule changed on one side and not the other fails a test rather than a client.
 *
 * Keys are UUIDs minted by the builder (`crypto.randomUUID()`) when a field is added and kept
 * through every rewording — the key is the question's identity, the label is its prose.
 */

export const FIELD_TYPES = [
  'heading',
  'paragraph',
  'short_text',
  'long_text',
  'yes_no',
  'single_choice',
  'multi_choice',
  'date',
  'acknowledgement',
  'signature',
] as const
export type FieldType = (typeof FIELD_TYPES)[number]

export type ShowIf = { key: string; equals: string[] }

export type FormField = {
  key: string
  type: FieldType
  label: string
  help?: string
  required: boolean
  options?: string[]
  show_if?: ShowIf
}

export type FormSchema = { fields: FormField[] }

export type Answers = Record<string, unknown>
export type AnswerError = 'required' | 'invalid'

export const DISPLAY: ReadonlySet<FieldType> = new Set(['heading', 'paragraph'])
export const CHOICE: ReadonlySet<FieldType> = new Set(['single_choice', 'multi_choice'])
/** What "show only if" may point at: a field whose answer is one of a known set of values. */
export const CONDITION_SOURCES: ReadonlySet<FieldType> = new Set([...CHOICE, 'yes_no'])
export const YES_NO = ['yes', 'no'] as const

export const MAX_FIELDS = 200
export const MAX_OPTIONS = 50
const LONG_LABEL_TYPES: ReadonlySet<FieldType> = new Set(['paragraph', 'acknowledgement'])
const MAX_LABEL = 500
const MAX_LONG_LABEL = 20_000
const MAX_HELP = 2_000
const MAX_OPTION = 200
const MAX_TEXT: Partial<Record<FieldType, number>> = { short_text: 500, long_text: 10_000 }

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i
const DATE = /^\d{4}-\d{2}-\d{2}$/

/** What each type is called in the builder, and what it asks the client to do. */
export const FIELD_TYPE_LABELS: Record<FieldType, string> = {
  heading: 'Heading',
  paragraph: 'Paragraph of text',
  short_text: 'Short answer',
  long_text: 'Long answer',
  yes_no: 'Yes / no',
  single_choice: 'Radio buttons (pick one)',
  multi_choice: 'Checkboxes (pick any)',
  date: 'Date',
  acknowledgement: 'Checkbox to agree',
  signature: 'Signature',
}

/** Kinds whose acknowledgements are clauses the client must agree to: never conditional. */
const BINDING_KINDS: ReadonlySet<string> = new Set(['consent', 'waiver'])

/** Every problem with a schema, as sentences; empty means the server will accept it. `kind`
 *  is the template's, because some rules depend on it. */
export function schemaProblems(schema: FormSchema, kind = 'other'): string[] {
  const problems: string[] = []
  const fields = Array.isArray(schema?.fields) ? schema.fields : []
  if (fields.length > MAX_FIELDS) problems.push(`A form has at most ${MAX_FIELDS} fields.`)
  const earlier = new Map<string, FormField>()
  let signatures = 0
  fields.forEach((field, index) => {
    const at = `Field ${index + 1}`
    if (typeof field.key !== 'string' || !UUID.test(field.key)) {
      problems.push(`${at}: its key is not a UUID.`)
    }
    if (!FIELD_TYPES.includes(field.type)) problems.push(`${at}: unknown field type.`)
    const label = typeof field.label === 'string' ? field.label.trim() : ''
    const limit = LONG_LABEL_TYPES.has(field.type) ? MAX_LONG_LABEL : MAX_LABEL
    if (!label) problems.push(`${at}: it needs a label.`)
    else if (label.length > limit) problems.push(`${at}: the label is over ${limit} characters.`)
    if ((field.help?.length ?? 0) > MAX_HELP) problems.push(`${at}: the help text is too long.`)
    if (DISPLAY.has(field.type) && field.required) {
      problems.push(`${at}: a heading or paragraph cannot be required.`)
    }
    if (field.type === 'signature' && !field.required) {
      problems.push(`${at}: a signature block is always required.`)
    }
    if (CHOICE.has(field.type)) {
      const options = (field.options ?? []).map((o) => o.trim())
      if (options.length === 0) problems.push(`${at}: add at least one option.`)
      if (options.length > MAX_OPTIONS) problems.push(`${at}: at most ${MAX_OPTIONS} options.`)
      if (options.some((o) => !o || o.length > MAX_OPTION)) {
        problems.push(`${at}: every option needs text.`)
      }
      if (new Set(options).size !== options.length) {
        problems.push(`${at}: the same option is listed twice.`)
      }
    } else if (field.options !== undefined && field.options !== null) {
      problems.push(`${at}: only a choice field has options.`)
    }
    if (field.show_if && field.type === 'signature') {
      // A signature block present means signing is required; no answer may skip it.
      problems.push(`${at}: a signature block cannot be conditional.`)
    }
    if (field.show_if && field.type === 'acknowledgement' && BINDING_KINDS.has(kind)) {
      problems.push(`${at}: an acknowledgement on a ${kind} cannot be conditional.`)
    }
    if (field.show_if) {
      const source = earlier.get(field.show_if.key?.toLowerCase())
      const equals = field.show_if.equals ?? []
      if (!source) problems.push(`${at}: “show only if” must refer to an earlier field.`)
      else if (!CONDITION_SOURCES.has(source.type)) {
        problems.push(`${at}: “show only if” must refer to a yes/no or choice field.`)
      } else if (source.show_if) {
        problems.push(`${at}: “show only if” cannot refer to a field that is itself conditional.`)
      } else {
        const allowed: readonly string[] =
          source.type === 'yes_no' ? YES_NO : (source.options ?? []).map((o) => o.trim())
        if (equals.length === 0) problems.push(`${at}: choose which answers show it.`)
        if (new Set(equals).size !== equals.length) {
          problems.push(`${at}: the same answer is listed twice.`)
        }
        if (equals.some((v) => !allowed.includes(v))) {
          problems.push(`${at}: “show only if” names an answer that field cannot have.`)
        }
      }
    }
    if (field.type === 'signature') signatures += 1
    const key = typeof field.key === 'string' ? field.key.toLowerCase() : ''
    if (earlier.has(key)) problems.push(`${at}: two fields share one key.`)
    earlier.set(key, field)
  })
  if (signatures > 1) problems.push('A form has at most one signature block.')
  return problems
}

function isEmpty(value: unknown): boolean {
  return (
    value === undefined ||
    value === null ||
    value === false ||
    (typeof value === 'string' && !value.trim()) ||
    (Array.isArray(value) && value.length === 0)
  )
}

function isShown(field: FormField, answers: Answers): boolean {
  if (!field.show_if) return true
  const answer = answers[field.show_if.key]
  const chosen = Array.isArray(answer) ? answer : [answer]
  return chosen.some((value) => field.show_if!.equals.includes(value as string))
}

/** The keys of the fields shown for these answers, in form order (display fields too). */
export function visibleKeys(schema: FormSchema, answers: Answers): string[] {
  return schema.fields.filter((f) => isShown(f, answers)).map((f) => f.key)
}

/** The answers worth filing: visible, answerable, non-empty, in form order. A hidden field's
 *  leftover answer is dropped — never filed in the client's chart. */
export function keptAnswers(schema: FormSchema, answers: Answers): Answers {
  const shown = new Set(visibleKeys(schema, answers))
  const kept: Answers = {}
  for (const f of schema.fields) {
    if (shown.has(f.key) && !DISPLAY.has(f.type) && !isEmpty(answers[f.key])) {
      kept[f.key] = answers[f.key]
    }
  }
  return kept
}

function isRealDate(value: string): boolean {
  if (!DATE.test(value)) return false
  const [y, m, d] = value.split('-').map(Number)
  const date = new Date(Date.UTC(y, m - 1, d))
  return date.getUTCFullYear() === y && date.getUTCMonth() === m - 1 && date.getUTCDate() === d
}

function isValid(field: FormField, value: unknown): boolean {
  const options = field.options ?? []
  switch (field.type) {
    case 'yes_no':
      return value === 'yes' || value === 'no'
    case 'single_choice':
      return typeof value === 'string' && options.includes(value)
    case 'multi_choice':
      return (
        Array.isArray(value) &&
        new Set(value).size === value.length &&
        value.every((v) => typeof v === 'string' && options.includes(v))
      )
    case 'short_text':
    case 'long_text':
      return typeof value === 'string' && value.length <= MAX_TEXT[field.type]!
    case 'date':
      return typeof value === 'string' && isRealDate(value)
    case 'acknowledgement':
      return value === true
    case 'signature': {
      if (typeof value !== 'object' || value === null || Array.isArray(value)) return false
      const keys = Object.keys(value).sort()
      const v = value as Record<string, unknown>
      return (
        keys.join() === 'image,name' &&
        keys.every((k) => typeof v[k] === 'string' && (v[k] as string).trim() !== '')
      )
    }
    default:
      return false
  }
}

/** `{key: 'required' | 'invalid'}` for each visible field that fails; `{}` means valid. */
export function validateAnswers(schema: FormSchema, answers: Answers): Record<string, AnswerError> {
  const shown = new Set(visibleKeys(schema, answers))
  const errors: Record<string, AnswerError> = {}
  for (const field of schema.fields) {
    if (!shown.has(field.key) || DISPLAY.has(field.type)) continue
    const value = answers[field.key]
    if (isEmpty(value)) {
      if (field.required) errors[field.key] = 'required'
    } else if (!isValid(field, value)) {
      errors[field.key] = 'invalid'
    }
  }
  return errors
}

/** A new field of `type`, with a fresh key and the defaults the server will accept. */
export function newField(type: FieldType): FormField {
  const field: FormField = {
    key: crypto.randomUUID(),
    type,
    label: '',
    required: type === 'signature',
  }
  if (type === 'signature') field.label = 'Signature'
  if (CHOICE.has(type)) field.options = ['Option 1', 'Option 2']
  return field
}
