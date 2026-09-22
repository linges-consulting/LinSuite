import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  ArrowDown,
  ArrowLeft,
  ArrowUp,
  Archive,
  ClipboardList,
  MoreHorizontal,
  Plus,
  Trash2,
} from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { toast } from 'sonner'
import { EmptyState } from '@/components/empty-state'
import { Field, Form } from '@/components/form'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { Skeleton } from '@/components/ui/skeleton'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Textarea } from '@/components/ui/textarea'
import {
  createFormTemplate,
  deleteFormTemplate,
  fetchFormTemplates,
  fetchFormVersions,
  publishFormTemplate,
  retireFormTemplate,
  saveFormDraft,
  type FormKind,
  type FormTemplate,
} from '@/lib/api'
import {
  CHOICE,
  CONDITION_SOURCES,
  DISPLAY,
  FIELD_TYPE_LABELS,
  FIELD_TYPES,
  YES_NO,
  newField,
  schemaProblems,
  visibleKeys,
  type Answers,
  type FieldType,
  type FormField,
} from '@/lib/forms'
import { FORM_TEMPLATES, FORM_VERSIONS } from '@/lib/query-keys'

/**
 * Settings → Forms: intake forms, consents and waivers (PRD §2, tech-stack §18).
 *
 * **Draft, then publish.** The builder edits one draft; "Publish" freezes it as the next
 * numbered version, which the database will never let anybody rewrite. Links and signed
 * submissions point at a version, so what a client signed is always what renders later.
 *
 * **Every field keeps its key.** A field gets a UUID when it is added and keeps it through
 * every rewording, so answers to "Any diabetes?" and "Any diabetes diagnosis?" stay one
 * question across versions. A field's type is fixed once added — change it by removing the
 * field and adding a new one.
 *
 * Needs `forms.manage` and Admin Mode, like every other panel here.
 */

const KINDS: { value: FormKind; label: string }[] = [
  { value: 'intake', label: 'Intake' },
  { value: 'consent', label: 'Consent' },
  { value: 'waiver', label: 'Waiver' },
  { value: 'other', label: 'Other' },
]
const kindLabel = (kind: FormKind) => KINDS.find((k) => k.value === kind)?.label ?? kind

const HEALTH_FORM_HINT =
  'A submission counts as a clinical entry in the client’s chart. For a regulated health ' +
  'practice that starts or extends the client’s retention hold, so the record is kept for the ' +
  'legal period and cannot be erased early.'
const MANDATORY_HINT =
  'Every client is expected to have a current signed copy. It appears on the essential-forms ' +
  'checklist, and clients without one are flagged.'
const RESIGNATURE_HINT =
  'Tick this when this version adds a materially new clause. Clients who signed an earlier ' +
  'version must sign this one before the form counts as complete again. For rewording that ' +
  'changes nothing material, leave it unticked.'
const FLAGS_TAKE_EFFECT = 'Both apply from the next version you publish.'

export function FormsPanel() {
  const templates = useQuery({ queryKey: FORM_TEMPLATES, queryFn: fetchFormTemplates })
  const [openId, setOpenId] = useState<string | null>(null)
  const [creating, setCreating] = useState(false)

  if (templates.isPending) return <Skeleton className="h-64 w-full" />
  if (templates.isError) {
    return (
      <p role="alert" className="text-sm text-destructive">
        {templates.error.message}
      </p>
    )
  }

  const open = templates.data.find((t) => t.id === openId)
  if (open) return <FormBuilder key={open.id} template={open} onBack={() => setOpenId(null)} />

  const newButton = (
    <Button onClick={() => setCreating(true)}>
      <Plus aria-hidden />
      New form
    </Button>
  )

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-4">
        <p className="max-w-3xl text-sm text-muted-foreground">
          Intake questionnaires, consents and waivers. Each publish is a numbered version that
          never changes, so a signed form always shows exactly what the client signed.
        </p>
        {templates.data.length > 0 && newButton}
      </div>

      {templates.data.length === 0 ? (
        <EmptyState
          icon={ClipboardList}
          title="No forms yet"
          description="Build an intake form, a consent or a waiver, then publish it for clients to fill in."
          action={newButton}
        />
      ) : (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Name</TableHead>
              <TableHead>Kind</TableHead>
              <TableHead>Version</TableHead>
              <TableHead>Status</TableHead>
              <TableHead className="text-right">Actions</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {templates.data.map((t) => (
              <TemplateLine key={t.id} template={t} onEdit={() => setOpenId(t.id)} />
            ))}
          </TableBody>
        </Table>
      )}

      {creating && (
        <CreateDialog
          onClose={() => setCreating(false)}
          onCreated={(created) => {
            setCreating(false)
            setOpenId(created.id)
          }}
        />
      )}
    </div>
  )
}

function StatusBadge({ template }: { template: FormTemplate }) {
  if (template.retired_at) return <Badge variant="secondary">Retired</Badge>
  if (template.latest_version === null) return <Badge variant="outline">Draft only</Badge>
  if (template.has_unpublished_changes) return <Badge variant="warning">Unpublished changes</Badge>
  return <Badge variant="success">Published</Badge>
}

function TemplateLine({ template, onEdit }: { template: FormTemplate; onEdit: () => void }) {
  const queryClient = useQueryClient()
  const refresh = () => queryClient.invalidateQueries({ queryKey: FORM_TEMPLATES })
  const retire = useMutation({
    mutationFn: () => retireFormTemplate(template.id),
    onSuccess: () => {
      toast.success(`Retired ${template.name}`, {
        description: 'No new links can be sent for it. Signed copies are kept.',
      })
      refresh()
    },
    onError: (error) => toast.error(error.message),
  })
  const remove = useMutation({
    mutationFn: () => deleteFormTemplate(template.id),
    onSuccess: () => {
      toast.success(`Deleted ${template.name}`)
      refresh()
    },
    onError: (error) => toast.error(error.message),
  })
  const published = template.latest_version !== null

  return (
    <TableRow className={template.retired_at ? 'opacity-55' : undefined}>
      <TableCell className="font-medium">{template.name}</TableCell>
      <TableCell>
        <Badge variant="outline">{kindLabel(template.kind)}</Badge>
      </TableCell>
      <TableCell className="tabular-nums">
        {published ? `v${template.latest_version}` : <span className="text-muted-foreground">—</span>}
      </TableCell>
      <TableCell>
        <StatusBadge template={template} />
      </TableCell>
      <TableCell className="text-right">
        <div className="flex justify-end gap-1">
          <Button variant="ghost" size="sm" onClick={onEdit} aria-label={`Edit ${template.name}`}>
            {template.retired_at ? 'View' : 'Edit'}
          </Button>
          {!template.retired_at && (
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button variant="ghost" size="sm" aria-label={`Actions for ${template.name}`}>
                  <MoreHorizontal aria-hidden />
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end" className="min-w-40">
                {published ? (
                  <DropdownMenuItem
                    variant="destructive"
                    disabled={retire.isPending}
                    onSelect={() => {
                      if (
                        confirm(
                          `Retire ${template.name}?\n\n` +
                            'No new links can be sent for it. Every published version and every ' +
                            'signed copy is kept.',
                        )
                      )
                        retire.mutate()
                    }}
                  >
                    <Archive aria-hidden />
                    Retire
                  </DropdownMenuItem>
                ) : (
                  <DropdownMenuItem
                    variant="destructive"
                    disabled={remove.isPending}
                    onSelect={() => {
                      if (confirm(`Delete ${template.name}? It was never published.`)) remove.mutate()
                    }}
                  >
                    <Trash2 aria-hidden />
                    Delete
                  </DropdownMenuItem>
                )}
              </DropdownMenuContent>
            </DropdownMenu>
          )}
        </div>
      </TableCell>
    </TableRow>
  )
}

function FlagCheckbox(props: {
  id: string
  label: string
  hint: string
  checked: boolean
  onChange: (on: boolean) => void
}) {
  return (
    <div className="flex items-start gap-3">
      <Checkbox
        id={props.id}
        className="mt-0.5"
        checked={props.checked}
        onCheckedChange={(on) => props.onChange(on === true)}
        aria-describedby={`${props.id}-hint`}
      />
      <div className="flex flex-col gap-1">
        <Label htmlFor={props.id}>{props.label}</Label>
        <p id={`${props.id}-hint`} className="text-xs text-muted-foreground">
          {props.hint}
        </p>
      </div>
    </div>
  )
}

function KindSelect(props: { id: string; value: FormKind; onChange: (kind: FormKind) => void }) {
  return (
    <Select value={props.value} onValueChange={(v) => props.onChange(v as FormKind)}>
      <SelectTrigger id={props.id} className="w-full">
        <SelectValue />
      </SelectTrigger>
      <SelectContent>
        {KINDS.map((k) => (
          <SelectItem key={k.value} value={k.value}>
            {k.label}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  )
}

function CreateDialog(props: { onClose: () => void; onCreated: (t: FormTemplate) => void }) {
  const queryClient = useQueryClient()
  const [name, setName] = useState('')
  const [kind, setKind] = useState<FormKind>('intake')
  const [health, setHealth] = useState(false)
  const [mandatory, setMandatory] = useState(false)
  const [touched, setTouched] = useState(false)
  const create = useMutation({
    mutationFn: () =>
      createFormTemplate({ name: name.trim(), kind, is_health_form: health, is_mandatory: mandatory }),
    onSuccess: (created) => {
      queryClient.invalidateQueries({ queryKey: FORM_TEMPLATES })
      props.onCreated(created)
    },
  })
  const nameError = touched && !name.trim() ? 'Give the form a name.' : undefined

  return (
    <Dialog open onOpenChange={(open) => !open && props.onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>New form</DialogTitle>
          <DialogDescription>You will add its fields next. Nothing is sent to clients until you publish.</DialogDescription>
        </DialogHeader>
        <Form
          onSubmit={() => {
            setTouched(true)
            if (name.trim()) create.mutate()
          }}
        >
          <Field label="Name" htmlFor="form-name" error={nameError}>
            <Input
              id="form-name"
              value={name}
              maxLength={200}
              onChange={(e) => setName(e.target.value)}
              onBlur={() => setTouched(true)}
              aria-invalid={Boolean(nameError)}
            />
          </Field>
          <Field label="Kind" htmlFor="form-kind">
            <KindSelect id="form-kind" value={kind} onChange={setKind} />
          </Field>
          <FlagCheckbox id="form-health" label="Health form" hint={HEALTH_FORM_HINT} checked={health} onChange={setHealth} />
          <FlagCheckbox
            id="form-mandatory"
            label="Mandatory (essential form)"
            hint={MANDATORY_HINT}
            checked={mandatory}
            onChange={setMandatory}
          />
          {create.isError && (
            <p role="alert" className="text-sm text-destructive">
              {create.error.message}
            </p>
          )}
          <DialogFooter>
            <Button type="button" variant="outline" onClick={props.onClose}>
              Cancel
            </Button>
            <Button type="submit" disabled={create.isPending}>
              Create form
            </Button>
          </DialogFooter>
        </Form>
      </DialogContent>
    </Dialog>
  )
}

// --- the builder -----------------------------------------------------------------------------

type Draft = {
  name: string
  kind: FormKind
  is_health_form: boolean
  is_mandatory: boolean
  fields: FormField[]
}

/** What the label box is called for each type: a heading has no question. */
function labelFor(type: FieldType): string {
  if (type === 'heading') return 'Heading'
  if (type === 'paragraph') return 'Text'
  if (type === 'acknowledgement') return 'Statement to agree to'
  if (type === 'signature') return 'Label'
  return 'Question'
}

function FormBuilder({ template, onBack }: { template: FormTemplate; onBack: () => void }) {
  const queryClient = useQueryClient()
  const [draft, setDraft] = useState<Draft>({
    name: template.name,
    kind: template.kind,
    is_health_form: template.draft.is_health_form,
    is_mandatory: template.draft.is_mandatory,
    fields: template.draft.schema.fields,
  })
  const [dirty, setDirty] = useState(false)
  const [publishing, setPublishing] = useState(false)
  const versions = useQuery({
    queryKey: [...FORM_VERSIONS, template.id],
    queryFn: () => fetchFormVersions(template.id),
  })
  const retired = template.retired_at !== null
  const problems = [
    ...(draft.name.trim() ? [] : ['The form needs a name.']),
    ...schemaProblems({ fields: draft.fields }),
  ]

  const change = (patch: Partial<Draft>) => {
    setDraft((d) => ({ ...d, ...patch }))
    setDirty(true)
  }
  const setField = (index: number, patch: Partial<FormField>) =>
    change({ fields: draft.fields.map((f, i) => (i === index ? { ...f, ...patch } : f)) })
  const move = (index: number, by: -1 | 1) => {
    const fields = [...draft.fields]
    ;[fields[index], fields[index + by]] = [fields[index + by], fields[index]]
    change({ fields })
  }
  const remove = (index: number) => {
    const gone = draft.fields[index].key
    // A field that was only shown because of this one would otherwise point at nothing.
    change({
      fields: draft.fields
        .filter((_, i) => i !== index)
        .map((f) => (f.show_if?.key === gone ? { ...f, show_if: undefined } : f)),
    })
  }

  const save = () =>
    saveFormDraft(template.id, {
      name: draft.name.trim(),
      kind: draft.kind,
      is_health_form: draft.is_health_form,
      is_mandatory: draft.is_mandatory,
      schema: { fields: draft.fields },
    })
  const refresh = () => {
    queryClient.invalidateQueries({ queryKey: FORM_TEMPLATES })
    queryClient.invalidateQueries({ queryKey: [...FORM_VERSIONS, template.id] })
  }
  const saveDraft = useMutation({
    mutationFn: save,
    onSuccess: () => {
      setDirty(false)
      toast.success('Draft saved')
      refresh()
    },
    onError: (error) => toast.error(error.message),
  })
  const publish = useMutation({
    mutationFn: async (requiresResignature: boolean) => {
      await save()
      setDirty(false)
      return publishFormTemplate(template.id, requiresResignature)
    },
    onSuccess: (version) => {
      setPublishing(false)
      toast.success(`Published version ${version.number}`)
      refresh()
    },
    onError: (error) => {
      toast.error(error.message)
      refresh()
    },
  })
  const busy = saveDraft.isPending || publish.isPending
  const canSave = !retired && problems.length === 0 && !busy
  // The list this replaced took the focused "Edit" button with it; start keyboard and screen
  // reader users at the top of the builder rather than nowhere.
  const heading = useRef<HTMLHeadingElement>(null)
  useEffect(() => heading.current?.focus(), [])

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex min-w-0 items-center gap-2">
          <Button variant="ghost" size="sm" onClick={onBack} aria-label="Back to forms">
            <ArrowLeft aria-hidden />
          </Button>
          <h2 ref={heading} tabIndex={-1} className="truncate text-base font-semibold outline-none">
            {template.name}
          </h2>
          {template.latest_version !== null && (
            <Badge variant="outline">Latest v{template.latest_version}</Badge>
          )}
        </div>
        <div className="flex items-center gap-2">
          {dirty && <span className="text-xs text-muted-foreground">Unsaved changes</span>}
          <Button variant="outline" disabled={!canSave} onClick={() => saveDraft.mutate()}>
            Save draft
          </Button>
          <Button disabled={!canSave} onClick={() => setPublishing(true)}>
            Publish…
          </Button>
        </div>
      </div>

      {retired && (
        <p className="rounded-lg border px-4 py-3 text-sm text-muted-foreground">
          This form is retired. Its versions are kept exactly as published and it can no longer
          be edited or sent.
        </p>
      )}

      <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_minmax(0,26rem)]">
        <fieldset disabled={retired} className="flex min-w-0 flex-col gap-6">
          <section className="grid gap-4 rounded-xl border p-4 sm:grid-cols-2">
            <Field label="Form name" htmlFor="builder-name">
              <Input
                id="builder-name"
                value={draft.name}
                maxLength={200}
                onChange={(e) => change({ name: e.target.value })}
              />
            </Field>
            <Field label="Kind" htmlFor="builder-kind">
              <KindSelect id="builder-kind" value={draft.kind} onChange={(kind) => change({ kind })} />
            </Field>
            <div className="flex flex-col gap-4 sm:col-span-2">
              <FlagCheckbox
                id="builder-health"
                label="Health form"
                hint={`${HEALTH_FORM_HINT} ${FLAGS_TAKE_EFFECT}`}
                checked={draft.is_health_form}
                onChange={(on) => change({ is_health_form: on })}
              />
              <FlagCheckbox
                id="builder-mandatory"
                label="Mandatory (essential form)"
                hint={MANDATORY_HINT}
                checked={draft.is_mandatory}
                onChange={(on) => change({ is_mandatory: on })}
              />
            </div>
          </section>

          <section aria-label="Fields" className="flex flex-col gap-3">
            {draft.fields.length === 0 ? (
              <EmptyState
                icon={ClipboardList}
                title="No fields yet"
                description="Add questions, statements to agree to, and a signature block."
              />
            ) : (
              draft.fields.map((field, index) => (
                <FieldEditor
                  key={field.key}
                  field={field}
                  index={index}
                  earlier={draft.fields.slice(0, index)}
                  last={index === draft.fields.length - 1}
                  onChange={(patch) => setField(index, patch)}
                  onMove={(by) => move(index, by)}
                  onRemove={() => remove(index)}
                />
              ))
            )}
            <AddField
              hasSignature={draft.fields.some((f) => f.type === 'signature')}
              onAdd={(type) => change({ fields: [...draft.fields, newField(type)] })}
            />
          </section>

          {problems.length > 0 && (
            <div role="status" className="rounded-lg border border-destructive/40 px-4 py-3">
              <p className="text-sm font-medium">Fix these before saving:</p>
              <ul className="mt-1 list-disc pl-5 text-sm text-destructive">
                {problems.map((p) => (
                  <li key={p}>{p}</li>
                ))}
              </ul>
            </div>
          )}
        </fieldset>

        <div className="flex flex-col gap-6 lg:sticky lg:top-4 lg:self-start">
          <FormPreview fields={draft.fields} />
          <section aria-labelledby="versions-title" className="rounded-xl border p-4">
            <h3 id="versions-title" className="text-base font-medium">
              Versions
            </h3>
            {versions.isPending ? (
              <Skeleton className="mt-3 h-10 w-full" />
            ) : !versions.data?.length ? (
              <p className="mt-1 text-sm text-muted-foreground">Not published yet.</p>
            ) : (
              <ul className="mt-2 flex flex-col divide-y">
                {versions.data.map((v) => (
                  <li key={v.number} className="flex flex-wrap items-center gap-2 py-2 text-sm">
                    <span className="font-medium tabular-nums">v{v.number}</span>
                    <time className="text-muted-foreground" dateTime={v.published_at}>
                      {new Date(v.published_at).toLocaleString()}
                    </time>
                    {v.requires_resignature && <Badge variant="warning">Re-signature required</Badge>}
                    {v.is_health_form && <Badge variant="info">Health</Badge>}
                    {v.is_mandatory && <Badge variant="outline">Mandatory</Badge>}
                  </li>
                ))}
              </ul>
            )}
          </section>
        </div>
      </div>

      {publishing && (
        <PublishDialog
          next={(template.latest_version ?? 0) + 1}
          pending={publish.isPending}
          onClose={() => setPublishing(false)}
          onPublish={(requiresResignature) => publish.mutate(requiresResignature)}
        />
      )}
    </div>
  )
}

function AddField(props: { hasSignature: boolean; onAdd: (type: FieldType) => void }) {
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="outline" className="self-start">
          <Plus aria-hidden />
          Add field
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" className="min-w-56">
        {FIELD_TYPES.map((type) => (
          <DropdownMenuItem
            key={type}
            // One signature block per form: the client signs the form once.
            disabled={type === 'signature' && props.hasSignature}
            onSelect={() => props.onAdd(type)}
          >
            {FIELD_TYPE_LABELS[type]}
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

const ALWAYS = '__always'

function FieldEditor(props: {
  field: FormField
  index: number
  earlier: FormField[]
  last: boolean
  onChange: (patch: Partial<FormField>) => void
  onMove: (by: -1 | 1) => void
  onRemove: () => void
}) {
  const { field, index } = props
  const n = index + 1
  const id = (part: string) => `field-${field.key}-${part}`
  const sources = props.earlier.filter((f) => CONDITION_SOURCES.has(f.type) && !f.show_if)
  const source = props.earlier.find((f) => f.key === field.show_if?.key)
  const values: readonly string[] =
    source?.type === 'yes_no' ? YES_NO : (source?.options ?? [])
  const answerable = !DISPLAY.has(field.type)
  const long = field.type === 'paragraph' || field.type === 'acknowledgement'

  return (
    <article className="flex flex-col gap-4 rounded-xl border p-4" aria-label={`Field ${n}`}>
      <div className="flex items-center justify-between gap-2">
        <p className="text-xs font-medium text-muted-foreground">
          {n}. {FIELD_TYPE_LABELS[field.type]}
        </p>
        <div className="flex gap-1">
          <Button
            variant="ghost"
            size="sm"
            aria-label={`Move field ${n} up`}
            disabled={index === 0}
            onClick={() => props.onMove(-1)}
          >
            <ArrowUp aria-hidden />
          </Button>
          <Button
            variant="ghost"
            size="sm"
            aria-label={`Move field ${n} down`}
            disabled={props.last}
            onClick={() => props.onMove(1)}
          >
            <ArrowDown aria-hidden />
          </Button>
          <Button variant="ghost" size="sm" aria-label={`Remove field ${n}`} onClick={props.onRemove}>
            <Trash2 aria-hidden />
          </Button>
        </div>
      </div>

      <Field label={labelFor(field.type)} htmlFor={id('label')}>
        {long ? (
          <Textarea
            id={id('label')}
            value={field.label}
            onChange={(e) => props.onChange({ label: e.target.value })}
          />
        ) : (
          <Input
            id={id('label')}
            value={field.label}
            maxLength={500}
            onChange={(e) => props.onChange({ label: e.target.value })}
          />
        )}
      </Field>

      {answerable && (
        <Field label="Help text (optional)" htmlFor={id('help')}>
          <Input
            id={id('help')}
            value={field.help ?? ''}
            maxLength={2000}
            onChange={(e) => props.onChange({ help: e.target.value || undefined })}
          />
        </Field>
      )}

      {CHOICE.has(field.type) && (
        <Field label="Options (one per line)" htmlFor={id('options')}>
          <Textarea
            id={id('options')}
            value={(field.options ?? []).join('\n')}
            onChange={(e) => props.onChange({ options: e.target.value.split('\n') })}
          />
        </Field>
      )}

      {field.type === 'signature' ? (
        <p className="text-xs text-muted-foreground">
          Clients sign by drawing their signature and typing their full name. A form with a
          signature block cannot be submitted unsigned.
        </p>
      ) : (
        answerable && (
          <div className="flex items-center gap-2">
            <Checkbox
              id={id('required')}
              checked={field.required}
              onCheckedChange={(on) => props.onChange({ required: on === true })}
            />
            <Label htmlFor={id('required')} className="font-normal">
              Required
            </Label>
          </div>
        )
      )}

      {sources.length > 0 && (
        <div className="flex flex-col gap-2">
          <Label htmlFor={id('show-if')}>Show only if…</Label>
          <Select
            value={field.show_if?.key ?? ALWAYS}
            onValueChange={(key) => {
              if (key === ALWAYS) return props.onChange({ show_if: undefined })
              const picked = sources.find((f) => f.key === key)!
              const first = picked.type === 'yes_no' ? 'yes' : (picked.options?.[0] ?? '')
              props.onChange({ show_if: { key, equals: [first] } })
            }}
          >
            <SelectTrigger id={id('show-if')} aria-label={`Show field ${n} only if`} className="w-full">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={ALWAYS}>Always show</SelectItem>
              {sources.map((f) => (
                <SelectItem key={f.key} value={f.key}>
                  {f.label.trim() || `Field ${props.earlier.indexOf(f) + 1} (no label yet)`}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          {field.show_if && (
            <div className="flex flex-wrap gap-x-4 gap-y-2" role="group" aria-label="…is answered">
              {values.map((value) => {
                const checked = field.show_if!.equals.includes(value)
                return (
                  <div key={value} className="flex items-center gap-2">
                    <Checkbox
                      id={id(`equals-${value}`)}
                      checked={checked}
                      onCheckedChange={(on) =>
                        props.onChange({
                          show_if: {
                            key: field.show_if!.key,
                            equals: on
                              ? [...field.show_if!.equals, value]
                              : field.show_if!.equals.filter((v) => v !== value),
                          },
                        })
                      }
                    />
                    <Label htmlFor={id(`equals-${value}`)} className="font-normal">
                      {value === 'yes' || value === 'no' ? `answered “${value}”` : value}
                    </Label>
                  </div>
                )
              })}
            </div>
          )}
        </div>
      )}
    </article>
  )
}

function PublishDialog(props: {
  next: number
  pending: boolean
  onClose: () => void
  onPublish: (requiresResignature: boolean) => void
}) {
  const [resign, setResign] = useState(false)
  return (
    <Dialog open onOpenChange={(open) => !open && props.onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Publish version {props.next}</DialogTitle>
          <DialogDescription>
            The draft is saved and frozen as version {props.next}. It can never be edited
            afterwards; later changes become version {props.next + 1}.
          </DialogDescription>
        </DialogHeader>
        {props.next > 1 && (
          <FlagCheckbox
            id="publish-resign"
            label="Clients must sign this version again"
            hint={RESIGNATURE_HINT}
            checked={resign}
            onChange={setResign}
          />
        )}
        <DialogFooter>
          <Button variant="outline" onClick={props.onClose}>
            Cancel
          </Button>
          <Button disabled={props.pending} onClick={() => props.onPublish(resign)}>
            Publish
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

// --- the preview -----------------------------------------------------------------------------

/** The form as a client will see it, driven by `lib/forms.ts` — a conditional field appears
 *  only once its condition holds. Answers here are local and never sent anywhere. */
function FormPreview({ fields }: { fields: FormField[] }) {
  const [answers, setAnswers] = useState<Answers>({})
  const shown = new Set(visibleKeys({ fields }, answers))
  const set = (key: string, value: unknown) => setAnswers((a) => ({ ...a, [key]: value }))

  return (
    <section aria-label="Preview" className="rounded-xl border p-4">
      <h3 className="text-base font-medium">Preview</h3>
      <p className="text-xs text-muted-foreground">Try it as a client would. Nothing is saved.</p>
      <div className="mt-4 flex flex-col gap-5">
        {fields.length === 0 && <p className="text-sm text-muted-foreground">Nothing to show yet.</p>}
        {fields
          .filter((f) => shown.has(f.key))
          .map((f) => (
            <PreviewField key={f.key} field={f} value={answers[f.key]} onChange={(v) => set(f.key, v)} />
          ))}
      </div>
    </section>
  )
}

function PreviewField(props: {
  field: FormField
  value: unknown
  onChange: (value: unknown) => void
}) {
  const { field, value, onChange } = props
  const id = `preview-${field.key}`
  const label = (
    <>
      {field.label ? <span>{field.label}</span> : <span className="text-muted-foreground">(no label yet)</span>}
      {field.required && (
        <span className="text-destructive" aria-label="required">
          {' *'}
        </span>
      )}
    </>
  )
  const help = field.help && <p className="text-xs text-muted-foreground">{field.help}</p>
  const choices = (type: 'radio' | 'checkbox', options: readonly string[], text = (o: string) => o) => (
    <fieldset className="flex flex-col gap-2">
      <legend className="mb-1 text-sm font-medium">{label}</legend>
      {help}
      {options.map((o) => {
        const selected = type === 'radio' ? value === o : Array.isArray(value) && value.includes(o)
        return (
          <label key={o} className="flex items-center gap-2 text-sm">
            <input
              type={type}
              name={id}
              className="size-4 accent-primary"
              checked={selected}
              onChange={() =>
                onChange(
                  type === 'radio'
                    ? o
                    : selected
                      ? (value as string[]).filter((v) => v !== o)
                      : [...((value as string[]) ?? []), o],
                )
              }
            />
            {text(o)}
          </label>
        )
      })}
    </fieldset>
  )

  switch (field.type) {
    case 'heading':
      return <h4 className="text-base font-semibold">{field.label}</h4>
    case 'paragraph':
      return <p className="text-sm whitespace-pre-wrap">{field.label}</p>
    case 'yes_no':
      return choices('radio', YES_NO, (o) => (o === 'yes' ? 'Yes' : 'No'))
    case 'single_choice':
      return choices('radio', field.options ?? [])
    case 'multi_choice':
      return choices('checkbox', field.options ?? [])
    case 'acknowledgement':
      return (
        <div className="flex items-start gap-2">
          <Checkbox id={id} className="mt-0.5" checked={value === true} onCheckedChange={(on) => onChange(on === true)} />
          <Label htmlFor={id} className="font-normal whitespace-pre-wrap">
            {label}
          </Label>
        </div>
      )
    case 'signature':
      return (
        <div className="flex flex-col gap-2">
          <Label>{label}</Label>
          <div className="flex h-24 items-center justify-center rounded-lg border border-dashed text-xs text-muted-foreground">
            The client draws their signature here
          </div>
          <Input aria-label="Full name" placeholder="Full name" />
        </div>
      )
    default:
      return (
        <div className="flex flex-col gap-2">
          <Label htmlFor={id}>{label}</Label>
          {help}
          {field.type === 'long_text' ? (
            <Textarea id={id} value={(value as string) ?? ''} onChange={(e) => onChange(e.target.value)} />
          ) : (
            <Input
              id={id}
              type={field.type === 'date' ? 'date' : 'text'}
              value={(value as string) ?? ''}
              onChange={(e) => onChange(e.target.value)}
            />
          )}
        </div>
      )
  }
}
