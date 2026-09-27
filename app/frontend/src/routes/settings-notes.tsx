import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowDown, ArrowUp, ClipboardList, Plus, Trash2 } from 'lucide-react'
import { useState } from 'react'
import { toast } from 'sonner'
import { ChoiceSelect } from '@/components/choice-select'
import { EmptyState } from '@/components/empty-state'
import { Field, Form, FormError } from '@/components/form'
import { DIAGRAM_LABELS } from '@/lib/note-diagrams'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Skeleton } from '@/components/ui/skeleton'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import {
  fetchNoteTemplates,
  saveNoteTemplate,
  type DiagramId,
  type NoteTemplate,
  type NoteTemplateDraft,
} from '@/lib/api'
import { NOTE_TEMPLATES } from '@/lib/query-keys'

const preset = (kind: string): NoteTemplateDraft =>
  kind === 'general'
    ? {
        name: 'General session log',
        active: true,
        diagram_ids: ['layout'],
        fields: [
          { key: 'summary', label: 'Session summary', required: true },
          { key: 'actions', label: 'Actions taken', required: false },
          { key: 'next', label: 'Next steps', required: false },
        ],
      }
    : {
        name: 'SOAP',
        active: true,
        diagram_ids: ['body_front', 'body_back'],
        fields: ['Subjective', 'Objective', 'Assessment', 'Plan'].map(
          (label) => ({ key: label.toLowerCase(), label, required: true }),
        ),
      }

export function NoteTemplatesPanel() {
  const queryClient = useQueryClient()
  const templates = useQuery({
    queryKey: [...NOTE_TEMPLATES, 'admin'],
    queryFn: () => fetchNoteTemplates(true),
  })
  const [editing, setEditing] = useState<NoteTemplate | 'new' | null>(null)
  const [retiring, setRetiring] = useState<NoteTemplate | null>(null)
  const retire = useMutation({
    mutationFn: (template: NoteTemplate) =>
      saveNoteTemplate(
        {
          name: template.name,
          fields: template.fields,
          diagram_ids: template.diagram_ids,
          active: !template.active,
        },
        template.id,
      ),
    onSuccess: (_, template) => {
      toast.success(
        template.active ? 'Note template retired' : 'Note template restored',
      )
      setRetiring(null)
      queryClient.invalidateQueries({ queryKey: NOTE_TEMPLATES })
    },
  })
  return (
    <section className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">Note templates</h1>
          <p className="text-sm text-muted-foreground">
            SOAP records and general session or project logs. Existing notes
            keep the wording they were created with.
          </p>
        </div>
        <Button onClick={() => setEditing('new')}>
          <Plus aria-hidden />
          New note template
        </Button>
      </div>
      {retire.error && <FormError>{retire.error.message}</FormError>}
      {templates.isPending ? (
        <Skeleton className="h-32 w-full" />
      ) : templates.isError ? (
        <FormError>{templates.error.message}</FormError>
      ) : templates.data.length === 0 ? (
        <EmptyState
          icon={ClipboardList}
          title="No note templates"
          description="Start with SOAP or a general session log and customize its fields and diagrams."
        />
      ) : (
        <Table aria-label="Note templates">
          <TableHeader>
            <TableRow>
              <TableHead>Name</TableHead>
              <TableHead>Fields</TableHead>
              <TableHead>Diagrams</TableHead>
              <TableHead>Status</TableHead>
              <TableHead>
                <span className="sr-only">Actions</span>
              </TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {templates.data.map((t) => (
              <TableRow key={t.id}>
                <TableCell className="font-medium">{t.name}</TableCell>
                <TableCell>{t.fields.length}</TableCell>
                <TableCell>
                  {t.diagram_ids.map((d) => DIAGRAM_LABELS[d]).join(', ') ||
                    'None'}
                </TableCell>
                <TableCell>
                  <Badge variant="secondary">
                    {t.active ? 'Active' : 'Retired'}
                  </Badge>
                </TableCell>
                <TableCell>
                  <div className="flex justify-end gap-2">
                    <Button
                      variant="ghost"
                      size="sm"
                      aria-label={`Edit ${t.name}`}
                      onClick={() => setEditing(t)}
                    >
                      Edit
                    </Button>
                    <Button
                      variant="ghost"
                      size="sm"
                      disabled={retire.isPending}
                      aria-label={`${t.active ? 'Retire' : 'Restore'} ${t.name}`}
                      onClick={() =>
                        t.active ? setRetiring(t) : retire.mutate(t)
                      }
                    >
                      {t.active ? 'Retire' : 'Restore'}
                    </Button>
                  </div>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
      {editing && (
        <TemplateEditor
          initial={editing === 'new' ? undefined : editing}
          onClose={() => setEditing(null)}
        />
      )}
      {retiring && (
        <Dialog
          open
          onOpenChange={(open) =>
            !open && !retire.isPending && setRetiring(null)
          }
        >
          <DialogContent showCloseButton={!retire.isPending}>
            <DialogHeader>
              <DialogTitle>Retire {retiring.name}?</DialogTitle>
              <DialogDescription>
                Staff will no longer be able to use this template for new notes.
                Existing notes keep their wording. You can restore the template
                later.
              </DialogDescription>
            </DialogHeader>
            {retire.error && <FormError>{retire.error.message}</FormError>}
            <div className="flex justify-end gap-2">
              <Button
                variant="outline"
                disabled={retire.isPending}
                onClick={() => setRetiring(null)}
              >
                Keep template
              </Button>
              <Button
                variant="destructive"
                disabled={retire.isPending}
                onClick={() => retire.mutate(retiring)}
              >
                Confirm retirement
              </Button>
            </div>
          </DialogContent>
        </Dialog>
      )}
    </section>
  )
}

function TemplateEditor(props: {
  initial?: NoteTemplate
  onClose: () => void
}) {
  const queryClient = useQueryClient()
  const [body, setBody] = useState<NoteTemplateDraft>(
    props.initial ?? preset('soap'),
  )
  const [starting, setStarting] = useState('soap')
  const [error, setError] = useState('')
  const [touched, setTouched] = useState<Set<string>>(new Set())
  const save = useMutation({
    mutationFn: () =>
      saveNoteTemplate(
        {
          name: body.name.trim(),
          active: body.active,
          fields: body.fields.map((f) => ({ ...f, label: f.label.trim() })),
          diagram_ids: body.diagram_ids,
        },
        props.initial?.id,
      ),
    onSuccess: () => {
      toast.success('Note template saved')
      queryClient.invalidateQueries({ queryKey: NOTE_TEMPLATES })
      props.onClose()
    },
  })
  const valid =
    body.name.trim() &&
    body.fields.length &&
    body.fields.every((f) => f.label.trim())
  const move = (index: number, direction: number) => {
    const fields = [...body.fields]
    ;[fields[index], fields[index + direction]] = [
      fields[index + direction],
      fields[index],
    ]
    setBody({ ...body, fields })
  }
  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open && !save.isPending) props.onClose()
      }}
    >
      <DialogContent
        className="max-h-[90dvh] overflow-y-auto sm:max-w-2xl"
        showCloseButton={!save.isPending}
      >
        <DialogHeader>
          <DialogTitle>
            {props.initial ? 'Edit note template' : 'New note template'}
          </DialogTitle>
          <DialogDescription>
            Choose the fields and named diagrams available when documenting an
            appointment.
          </DialogDescription>
        </DialogHeader>
        <Form
          onSubmit={() => {
            if (!valid) {
              setError('Enter a name and a label for every field.')
              return
            }
            setError('')
            save.mutate()
          }}
        >
          {!props.initial && (
            <Field label="Starting point" htmlFor="note-preset">
              <ChoiceSelect
                id="note-preset"
                value={starting}
                onValueChange={(value) => {
                  setStarting(value)
                  setBody(preset(value))
                }}
                options={[
                  { value: 'soap', label: 'SOAP' },
                  { value: 'general', label: 'General session log' },
                ]}
              />
            </Field>
          )}
          <Field
            label="Template name"
            htmlFor="template-name"
            error={
              !body.name.trim() && error ? 'A name is required.' : undefined
            }
          >
            <Input
              id="template-name"
              maxLength={200}
              value={body.name}
              disabled={save.isPending}
              onBlur={() => {
                if (!body.name.trim()) setError('A name is required.')
              }}
              onChange={(e) => setBody({ ...body, name: e.target.value })}
            />
          </Field>
          <fieldset className="space-y-3" disabled={save.isPending}>
            <legend className="mb-2 text-sm font-medium">Note fields</legend>
            {body.fields.map((field, index) => (
              <div
                key={field.key}
                className="flex flex-wrap items-end gap-2 rounded-lg border p-3"
              >
                <div className="min-w-40 flex-1">
                  <Field
                    label={`Field ${index + 1} label`}
                    htmlFor={`label-${field.key}`}
                    error={
                      touched.has(field.key) && !field.label.trim()
                        ? 'A label is required.'
                        : undefined
                    }
                  >
                    <Input
                      id={`label-${field.key}`}
                      maxLength={200}
                      value={field.label}
                      onBlur={() => {
                        setTouched(
                          (current) => new Set([...current, field.key]),
                        )
                      }}
                      onChange={(e) =>
                        setBody({
                          ...body,
                          fields: body.fields.map((f) =>
                            f.key === field.key
                              ? { ...f, label: e.target.value }
                              : f,
                          ),
                        })
                      }
                    />
                  </Field>
                </div>
                <Label className="flex h-9 items-center gap-2">
                  <Checkbox
                    checked={field.required}
                    onCheckedChange={(checked) =>
                      setBody({
                        ...body,
                        fields: body.fields.map((f) =>
                          f.key === field.key
                            ? { ...f, required: checked === true }
                            : f,
                        ),
                      })
                    }
                  />
                  Required
                </Label>
                <Button
                  type="button"
                  variant="ghost"
                  size="icon"
                  disabled={index === 0}
                  aria-label={`Move field ${index + 1} up`}
                  onClick={() => move(index, -1)}
                >
                  <ArrowUp aria-hidden />
                </Button>
                <Button
                  type="button"
                  variant="ghost"
                  size="icon"
                  disabled={index === body.fields.length - 1}
                  aria-label={`Move field ${index + 1} down`}
                  onClick={() => move(index, 1)}
                >
                  <ArrowDown aria-hidden />
                </Button>
                <Button
                  type="button"
                  variant="ghost"
                  size="icon"
                  disabled={body.fields.length === 1}
                  aria-label={`Remove field ${index + 1}`}
                  onClick={() =>
                    setBody({
                      ...body,
                      fields: body.fields.filter((f) => f.key !== field.key),
                    })
                  }
                >
                  <Trash2 aria-hidden />
                </Button>
              </div>
            ))}
            <Button
              type="button"
              variant="outline"
              disabled={body.fields.length >= 30}
              onClick={() =>
                setBody({
                  ...body,
                  fields: [
                    ...body.fields,
                    { key: crypto.randomUUID(), label: '', required: false },
                  ],
                })
              }
            >
              <Plus aria-hidden />
              Add field
            </Button>
          </fieldset>
          <fieldset className="space-y-3" disabled={save.isPending}>
            <legend className="mb-2 text-sm font-medium">Diagrams</legend>
            {(Object.keys(DIAGRAM_LABELS) as DiagramId[]).map((id) => (
              <Label key={id} className="flex items-center gap-2">
                <Checkbox
                  checked={body.diagram_ids.includes(id)}
                  onCheckedChange={(checked) =>
                    setBody({
                      ...body,
                      diagram_ids: checked
                        ? [...body.diagram_ids, id]
                        : body.diagram_ids.filter((d) => d !== id),
                    })
                  }
                />
                {DIAGRAM_LABELS[id]}
              </Label>
            ))}
          </fieldset>
          {(error || save.error) && (
            <FormError>{save.error?.message ?? error}</FormError>
          )}
          <div className="flex justify-end gap-2">
            <Button
              type="button"
              variant="outline"
              disabled={save.isPending}
              onClick={props.onClose}
            >
              Cancel
            </Button>
            <Button type="submit" disabled={save.isPending}>
              {save.isPending ? 'Saving…' : 'Save template'}
            </Button>
          </div>
        </Form>
      </DialogContent>
    </Dialog>
  )
}
