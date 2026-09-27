import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { FilePenLine, LockKeyhole, Plus } from 'lucide-react'
import { useState } from 'react'
import { toast } from 'sonner'
import { ChoiceSelect } from '@/components/choice-select'
import { EmptyState } from '@/components/empty-state'
import { NoteDiagram } from '@/components/note-diagram'
import { Field, FormError } from '@/components/form'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Skeleton } from '@/components/ui/skeleton'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { Textarea } from '@/components/ui/textarea'
import {
  createSessionNote,
  fetchNoteAppointments,
  fetchNoteTemplates,
  fetchSessionNote,
  fetchSessionNotes,
  lockSessionNote,
  updateSessionNote,
  type NoteAnnotation,
  type NoteContent,
  type SessionNote,
  type NoteTemplate,
} from '@/lib/api'
import {
  CUSTOMER_PROFILE,
  NOTE_APPOINTMENTS,
  NOTE_TEMPLATES,
  SESSION_NOTE,
  SESSION_NOTES,
} from '@/lib/query-keys'

const when = (at: string, timezone: string) =>
  new Date(at).toLocaleString('en-CA', {
    timeZone: timezone,
    dateStyle: 'medium',
    timeStyle: 'short',
  })
type CardProps = {
  customerId: string
  timezone: string
  suppressed: boolean
  canWrite: boolean
}

export function SessionNotesCard(props: CardProps) {
  const [opening, setOpening] = useState<string | null>(null)
  const [creating, setCreating] = useState(false)
  const history = useQuery({
    queryKey: [...SESSION_NOTES, props.customerId],
    queryFn: () => fetchSessionNotes(props.customerId),
  })
  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between gap-3">
        <CardTitle className="text-base font-medium">
          <h2>Session notes</h2>
        </CardTitle>
        {props.canWrite && !props.suppressed && (
          <Button variant="outline" size="sm" onClick={() => setCreating(true)}>
            <Plus aria-hidden />
            New session note
          </Button>
        )}
      </CardHeader>
      <CardContent>
        {history.isPending ? (
          <Skeleton className="h-20 w-full" />
        ) : history.isError ? (
          <FormError>{history.error.message}</FormError>
        ) : history.data.length === 0 ? (
          <EmptyState
            icon={FilePenLine}
            title="No session notes yet"
            description="Document an appointment using a note template. Finalized notes can be locked."
          />
        ) : (
          <Table aria-label="Session note history">
            <TableHeader>
              <TableRow>
                <TableHead>Recorded</TableHead>
                <TableHead>Template</TableHead>
                <TableHead>Practitioner</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>
                  <span className="sr-only">Open</span>
                </TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {history.data.map((note) => (
                <TableRow key={note.id}>
                  <TableCell>{when(note.created_at, props.timezone)}</TableCell>
                  <TableCell>{note.template.name}</TableCell>
                  <TableCell>{note.author_name}</TableCell>
                  <TableCell>
                    <Badge variant="secondary">
                      {note.locked_at ? 'Locked' : 'Draft'}
                    </Badge>
                  </TableCell>
                  <TableCell>
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => setOpening(note.id)}
                      aria-label={`Open ${note.template.name}`}
                    >
                      Open
                    </Button>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </CardContent>
      {creating && <NewNote {...props} onClose={() => setCreating(false)} />}
      {opening && (
        <OpenedNote
          {...props}
          noteId={opening}
          onClose={() => setOpening(null)}
        />
      )}
    </Card>
  )
}

function NewNote(props: CardProps & { onClose: () => void }) {
  const templates = useQuery({
    queryKey: NOTE_TEMPLATES,
    queryFn: () => fetchNoteTemplates(),
  })
  const appointments = useQuery({
    queryKey: [...NOTE_APPOINTMENTS, props.customerId],
    queryFn: () => fetchNoteAppointments(props.customerId),
  })
  const [templateId, setTemplateId] = useState('')
  const [appointmentId, setAppointmentId] = useState('')
  const [started, setStarted] = useState(false)
  const template = templates.data?.find((t) => t.id === templateId)
  return (
    <NoteEditor
      {...props}
      key={templateId}
      template={template}
      appointmentId={appointmentId}
      onDirty={() => setStarted(true)}
    >
      {templates.isPending || appointments.isPending ? (
        <Skeleton className="h-20 w-full" />
      ) : templates.isError || appointments.isError ? (
        <FormError>
          {templates.error?.message ?? appointments.error?.message}
        </FormError>
      ) : (
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label="Appointment" htmlFor="note-appointment">
            <ChoiceSelect
              id="note-appointment"
              value={appointmentId}
              onValueChange={setAppointmentId}
              placeholder="Choose your appointment"
              options={appointments.data.map((a) => ({
                value: a.id,
                label: `${when(a.starts_at, props.timezone)} · ${a.service_name}`,
              }))}
            />
            {appointments.data?.length === 0 && (
              <p className="text-sm text-muted-foreground">
                No eligible appointments assigned to you.
              </p>
            )}
          </Field>
          <Field
            label="Note template"
            htmlFor="note-template"
            hint={
              started
                ? 'To use a different template, close this draft and start a new note.'
                : undefined
            }
          >
            <ChoiceSelect
              id="note-template"
              disabled={started}
              value={templateId}
              onValueChange={setTemplateId}
              placeholder="Choose a template"
              options={templates.data
                .filter((t) => t.active)
                .map((t) => ({ value: t.id, label: t.name }))}
            />
            {!templates.data?.some((t) => t.active) && (
              <p className="text-sm text-muted-foreground">
                Ask an administrator to configure note templates in Settings.
              </p>
            )}
          </Field>
        </div>
      )}
    </NoteEditor>
  )
}

function OpenedNote(
  props: CardProps & { noteId: string; onClose: () => void },
) {
  const [accessId] = useState(() => crypto.randomUUID())
  // One logged GET per explicit open, and no retained PHI after closing.
  const note = useQuery({
    queryKey: [...SESSION_NOTE, props.customerId, props.noteId, accessId],
    queryFn: () => fetchSessionNote(props.customerId, props.noteId),
    gcTime: 0,
    staleTime: Infinity,
    retry: false,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
  })
  if (!note.data)
    return (
      <Dialog
        open
        onOpenChange={(open) => {
          if (!open) props.onClose()
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Session note</DialogTitle>
            <DialogDescription>
              Opening the appointment record.
            </DialogDescription>
          </DialogHeader>
          {note.isError ? (
            <FormError>{note.error.message}</FormError>
          ) : (
            <Skeleton className="h-32 w-full" />
          )}
        </DialogContent>
      </Dialog>
    )
  return (
    <NoteEditor
      {...props}
      template={{ ...note.data.template, id: note.data.template_id }}
      initial={note.data}
      appointmentId={note.data.appointment_id}
    />
  )
}

function NoteEditor(
  props: CardProps & {
    onClose: () => void
    onDirty?: () => void
    template?: NoteTemplate
    appointmentId: string
    initial?: SessionNote & NoteContent
    children?: React.ReactNode
  },
) {
  const queryClient = useQueryClient()
  const [metadata, setMetadata] = useState<SessionNote | undefined>(
    props.initial,
  )
  const [answers, setAnswers] = useState<Record<string, string>>(
    props.initial?.answers ?? {},
  )
  const [annotations, setAnnotations] = useState<NoteAnnotation[]>(
    props.initial?.annotations ?? [],
  )
  const [dirty, setDirty] = useState(false)
  const [confirmLock, setConfirmLock] = useState(false)
  const [confirmDiscard, setConfirmDiscard] = useState(false)
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({})
  const readOnly =
    props.suppressed || !props.canWrite || (!!metadata && !metadata.can_edit)
  const changed = () => {
    setDirty(true)
    setConfirmLock(false)
    props.onDirty?.()
  }
  const refresh = () => {
    queryClient.invalidateQueries({
      queryKey: [...SESSION_NOTES, props.customerId],
    })
    // A clinical entry advances the retention hold. Refresh only on the next explicit profile open.
    queryClient.invalidateQueries({
      queryKey: [...CUSTOMER_PROFILE, props.customerId],
      refetchType: 'none',
    })
  }
  const save = useMutation({
    gcTime: 0,
    mutationFn: () => {
      const content: NoteContent = { answers, annotations }
      return metadata
        ? updateSessionNote(props.customerId, metadata.id, {
            ...content,
            revision: metadata.revision,
          })
        : createSessionNote(props.customerId, {
            ...content,
            appointment_id: props.appointmentId,
            template_id: props.template!.id,
            template: {
              name: props.template!.name,
              fields: props.template!.fields,
              diagram_ids: props.template!.diagram_ids,
              active: props.template!.active,
            },
          })
    },
    onSuccess: (result) => {
      setMetadata(result)
      setDirty(false)
      toast.success('Session note saved')
      refresh()
      if (!props.initial) props.onClose()
    },
  })
  const lock = useMutation({
    gcTime: 0,
    mutationFn: () =>
      lockSessionNote(props.customerId, metadata!.id, metadata!.revision),
    onSuccess: (result) => {
      setMetadata(result)
      setConfirmLock(false)
      toast.success('Session note locked')
      refresh()
    },
  })
  const pending = save.isPending || lock.isPending
  const close = () => {
    if (pending) return
    if (dirty) setConfirmDiscard(true)
    else props.onClose()
  }
  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open) close()
      }}
    >
      <DialogContent
        className="max-h-[90dvh] overflow-y-auto sm:max-w-4xl"
        showCloseButton={!pending}
      >
        <DialogHeader className="sticky -top-4 z-10 bg-popover py-3">
          <DialogTitle className="flex items-center gap-3">
            {metadata ? props.template?.name : 'New session note'}
            {metadata && (
              <Badge variant="secondary">
                {metadata.locked_at ? 'Locked' : 'Draft'}
              </Badge>
            )}
          </DialogTitle>
          <DialogDescription>
            {metadata
              ? `${metadata.author_name} · Revision ${metadata.revision} · ${when(metadata.created_at, props.timezone)}`
              : 'Choose an appointment assigned to you. Drafts can be edited until you lock them.'}
          </DialogDescription>
        </DialogHeader>
        {metadata?.locked_at && (
          <p className="flex items-center gap-2 text-sm font-medium">
            <LockKeyhole className="size-4" aria-hidden />
            Locked {when(metadata.locked_at, props.timezone)}. This record is
            immutable.
          </p>
        )}
        {props.children}
        <div className="grid gap-4">
          {props.template?.fields.map((field) => (
            <Field
              key={field.key}
              label={`${field.label}${field.required ? ' *' : ''}`}
              htmlFor={`note-${field.key}`}
              error={fieldErrors[field.key]}
            >
              <Textarea
                id={`note-${field.key}`}
                className="disabled:opacity-100"
                value={answers[field.key] ?? ''}
                disabled={readOnly || pending}
                maxLength={20000}
                rows={4}
                aria-invalid={Boolean(fieldErrors[field.key])}
                onBlur={() =>
                  setFieldErrors((current) => ({
                    ...current,
                    [field.key]:
                      field.required && !answers[field.key]?.trim()
                        ? 'Required before locking. You can still save a draft.'
                        : '',
                  }))
                }
                onChange={(e) => {
                  setAnswers({ ...answers, [field.key]: e.target.value })
                  changed()
                }}
              />
            </Field>
          ))}
        </div>
        {!!props.template?.diagram_ids.length && (
          <NoteDiagram
            diagrams={props.template.diagram_ids}
            annotations={annotations}
            readOnly={readOnly || pending}
            timezone={props.timezone}
            onChange={(value) => {
              setAnnotations(value)
              changed()
            }}
          />
        )}
        {(save.error || lock.error) && (
          <FormError>{save.error?.message ?? lock.error?.message}</FormError>
        )}
        {confirmDiscard ? (
          <div role="alert" className="space-y-3 rounded-lg border p-4">
            <p>Discard unsaved changes?</p>
            <div className="flex gap-2">
              <Button variant="destructive" onClick={props.onClose}>
                Discard changes
              </Button>
              <Button
                variant="outline"
                onClick={() => setConfirmDiscard(false)}
              >
                Keep editing
              </Button>
            </div>
          </div>
        ) : confirmLock ? (
          <div role="alert" className="space-y-3 rounded-lg border p-4">
            <p>
              Lock this note permanently? Its text and diagrams cannot be
              changed afterwards.
            </p>
            <div className="flex gap-2">
              <Button disabled={pending} onClick={() => lock.mutate()}>
                Confirm lock
              </Button>
              <Button
                variant="outline"
                disabled={pending}
                onClick={() => setConfirmLock(false)}
              >
                Keep editing
              </Button>
            </div>
          </div>
        ) : (
          <div className="sticky -bottom-4 z-10 -mx-4 flex flex-wrap justify-end gap-2 border-t bg-popover px-4 py-3">
            <Button variant="outline" disabled={pending} onClick={close}>
              Close
            </Button>
            {!readOnly && (
              <>
                <Button
                  variant="outline"
                  disabled={pending || !props.template || !props.appointmentId}
                  onClick={() => save.mutate()}
                >
                  {save.isPending ? 'Saving…' : 'Save draft'}
                </Button>
                {metadata && (
                  <Button
                    disabled={pending || dirty}
                    onClick={() => {
                      const missing = Object.fromEntries(
                        props
                          .template!.fields.filter(
                            (f) => f.required && !answers[f.key]?.trim(),
                          )
                          .map((f) => [
                            f.key,
                            'Required before locking. You can still save a draft.',
                          ]),
                      )
                      setFieldErrors(missing)
                      if (!Object.keys(missing).length) setConfirmLock(true)
                    }}
                  >
                    <LockKeyhole aria-hidden />
                    Lock note
                  </Button>
                )}
              </>
            )}
          </div>
        )}
        {dirty && metadata && (
          <p className="text-xs text-muted-foreground">
            Save your changes before locking the note.
          </p>
        )}
      </DialogContent>
    </Dialog>
  )
}
