import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Copy, Send } from 'lucide-react'
import { forwardRef, useImperativeHandle, useState } from 'react'
import { toast } from 'sonner'
import { Form } from '@/components/form'
import { QrCode } from '@/components/qr-code'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
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
import {
  fetchClientSubmissions,
  fetchFormSubmission,
  fetchOpenFormLinks,
  fetchSendableForms,
  issueFormLink,
  revokeFormLink,
  type FormSubmissionDetail,
  type IssuedFormLink,
} from '@/lib/api'
import { FORM_LINKS, FORM_SUBMISSION, FORM_SUBMISSIONS, SENDABLE_FORMS } from '@/lib/query-keys'

/**
 * The profile's "Forms" card (`forms.issue`, Staff Mode; #46): send a form, and see and revoke
 * the links still open.
 *
 * **The link is shown once.** The server keeps only its hash, so the URL in the issue answer
 * is the only copy outside the client's email: the dialog shows it with Copy and a QR code —
 * the in-clinic flow is a tablet that is never signed in, which scans the code and is handed
 * to the client — and it is gone when the dialog closes.
 *
 * **Completed forms** (`forms.view`; #47): the list is metadata — form, version, method, when —
 * and opening one is a logged read of the chart that shows the answers with the labels of the
 * version they were given against. The opened answers are never kept in the query cache.
 *
 * **The compliance banner's shortcut** (Task 8, #51): `openSendFor(templateId)`, reached
 * through a ref, opens the same send dialog with that template already chosen — the banner
 * names a specific missing form, so the dialog should not make the front desk pick it again.
 */
export type ClientFormsCardHandle = { openSendFor: (templateId: string) => void }

export const ClientFormsCard = forwardRef<
  ClientFormsCardHandle,
  {
    customerId: string
    timezone: string
    suppressed: boolean
    canSend: boolean
    canView: boolean
  }
>(function ClientFormsCard(props, ref) {
  const [sending, setSending] = useState(false)
  const [presetTemplateId, setPresetTemplateId] = useState<string | undefined>()
  const [opening, setOpening] = useState<string | null>(null)
  useImperativeHandle(ref, () => ({
    openSendFor: (templateId: string) => {
      setPresetTemplateId(templateId)
      setSending(true)
    },
  }))
  const queryClient = useQueryClient()
  const links = useQuery({
    queryKey: [...FORM_LINKS, props.customerId],
    queryFn: () => fetchOpenFormLinks(props.customerId),
    enabled: props.canSend,
  })
  const completed = useQuery({
    queryKey: [...FORM_SUBMISSIONS, props.customerId],
    queryFn: () => fetchClientSubmissions(props.customerId),
    enabled: props.canView,
  })
  const revoke = useMutation({
    mutationFn: (linkId: string) => revokeFormLink(props.customerId, linkId),
    onSuccess: () => {
      toast.success('Link revoked')
      queryClient.invalidateQueries({ queryKey: [...FORM_LINKS, props.customerId] })
    },
    onError: (error) => toast.error(error.message),
  })
  const when = (instant: string) =>
    new Date(instant).toLocaleString('en-CA', {
      timeZone: props.timezone,
      dateStyle: 'medium',
      timeStyle: 'short',
    })

  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between">
        <CardTitle className="text-base font-medium">
          <h2>Forms</h2>
        </CardTitle>
        {props.canSend && !props.suppressed && (
          <Button size="sm" variant="outline" onClick={() => setSending(true)}>
            <Send aria-hidden />
            Send form
          </Button>
        )}
      </CardHeader>
      <CardContent className="flex flex-col gap-6">
        {props.canView && (
          <section className="flex flex-col gap-2">
            <h3 className="text-sm font-medium">Completed</h3>
            {completed.isPending ? (
              <Skeleton className="h-10 w-full" />
            ) : completed.isError ? (
              <p className="text-sm text-destructive">{completed.error.message}</p>
            ) : completed.data.length === 0 ? (
              <p className="text-sm text-muted-foreground">No completed forms yet.</p>
            ) : (
              <Table aria-label="Completed forms">
                <TableHeader>
                  <TableRow>
                    <TableHead>Form</TableHead>
                    <TableHead>Method</TableHead>
                    <TableHead>Submitted</TableHead>
                    <TableHead className="sr-only">Actions</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {completed.data.map((s) => (
                    <TableRow key={s.id}>
                      <TableCell>
                        {s.template_name} <span className="text-muted-foreground">v{s.version}</span>
                      </TableCell>
                      <TableCell>{s.method === 'link' ? 'Link' : 'Scan'}</TableCell>
                      <TableCell>{when(s.submitted_at)}</TableCell>
                      <TableCell className="text-right">
                        <Button
                          size="sm"
                          variant="ghost"
                          onClick={() => setOpening(s.id)}
                          aria-label={`Open ${s.template_name}`}
                        >
                          Open
                        </Button>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            )}
          </section>
        )}
        {props.canSend && (
          <section className="flex flex-col gap-2">
            {props.canView && <h3 className="text-sm font-medium">Waiting to be filled in</h3>}
            {links.isPending ? (
              <Skeleton className="h-10 w-full" />
            ) : links.isError ? (
              <p className="text-sm text-destructive">{links.error.message}</p>
            ) : links.data.length === 0 ? (
              <p className="text-sm text-muted-foreground">No forms waiting to be filled in.</p>
            ) : (
              <Table aria-label="Open form links">
                <TableHeader>
                  <TableRow>
                    <TableHead>Form</TableHead>
                    <TableHead>Sent</TableHead>
                    <TableHead>Expires</TableHead>
                    <TableHead className="sr-only">Actions</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {links.data.map((link) => (
                    <TableRow key={link.id}>
                      <TableCell>
                        {link.template_name}{' '}
                        <span className="text-muted-foreground">v{link.version}</span>
                      </TableCell>
                      <TableCell>{when(link.issued_at)}</TableCell>
                      <TableCell>{when(link.expires_at)}</TableCell>
                      <TableCell className="text-right">
                        <Button
                          size="sm"
                          variant="ghost"
                          disabled={revoke.isPending}
                          onClick={() => revoke.mutate(link.id)}
                          aria-label={`Revoke ${link.template_name}`}
                        >
                          Revoke
                        </Button>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            )}
          </section>
        )}
      </CardContent>
      {opening && (
        <SubmissionDialog
          customerId={props.customerId}
          submissionId={opening}
          when={when}
          onClose={() => setOpening(null)}
        />
      )}
      {sending && (
        <SendFormDialog
          customerId={props.customerId}
          when={when}
          presetTemplateId={presetTemplateId}
          onClose={() => {
            setSending(false)
            setPresetTemplateId(undefined)
          }}
        />
      )}
    </Card>
  )
})

function SendFormDialog(props: {
  customerId: string
  when: (instant: string) => string
  presetTemplateId?: string
  onClose: () => void
}) {
  const queryClient = useQueryClient()
  const [templateId, setTemplateId] = useState(props.presetTemplateId ?? '')
  const forms = useQuery({ queryKey: SENDABLE_FORMS, queryFn: fetchSendableForms })
  const issue = useMutation({
    mutationFn: () => issueFormLink(props.customerId, templateId),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: [...FORM_LINKS, props.customerId] }),
  })

  return (
    <Dialog open onOpenChange={(open) => !open && props.onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Send a form</DialogTitle>
          <DialogDescription>
            The client gets a private link that works once and expires in 48 hours.
          </DialogDescription>
        </DialogHeader>
        {issue.data ? (
          <IssuedLink link={issue.data} when={props.when} onDone={props.onClose} />
        ) : (
          <Form onSubmit={() => templateId && issue.mutate()}>
            <div className="grid gap-2">
              <Label htmlFor="send-form-template">Form</Label>
              {forms.isPending ? (
                <Skeleton className="h-9 w-full" />
              ) : forms.data?.length === 0 ? (
                <p className="text-sm text-muted-foreground">
                  No published forms yet. An administrator publishes them in Settings → Forms.
                </p>
              ) : (
                <Select value={templateId} onValueChange={setTemplateId}>
                  <SelectTrigger id="send-form-template" className="w-full">
                    <SelectValue placeholder="Choose a form" />
                  </SelectTrigger>
                  <SelectContent>
                    {(forms.data ?? []).map((f) => (
                      <SelectItem key={f.template_id} value={f.template_id}>
                        {f.name}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              )}
            </div>
            {(issue.isError || forms.isError) && (
              <p role="alert" className="text-sm text-destructive">
                {(issue.error ?? forms.error)?.message}
              </p>
            )}
            <DialogFooter>
              <Button type="button" variant="outline" onClick={props.onClose}>
                Cancel
              </Button>
              <Button type="submit" disabled={!templateId || issue.isPending}>
                Create link
              </Button>
            </DialogFooter>
          </Form>
        )}
      </DialogContent>
    </Dialog>
  )
}

function IssuedLink(props: { link: IssuedFormLink; when: (instant: string) => string; onDone: () => void }) {
  const { link } = props
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(link.url)
      toast.success('Link copied')
    } catch {
      toast.error('Could not copy. Select the link and copy it instead.')
    }
  }
  return (
    <div className="flex flex-col gap-4">
      <p className="text-sm" role="status">
        {link.emailed_to
          ? `Emailed to ${link.emailed_to}.`
          : 'No email on file. Copy the link, or let the client scan the code.'}
      </p>
      <div className="flex justify-center">
        <QrCode value={link.url} label="QR code for the form link" />
      </div>
      <div className="flex gap-2">
        <Input readOnly aria-label="Form link" value={link.url} onFocus={(e) => e.target.select()} />
        <Button type="button" variant="outline" onClick={copy}>
          <Copy aria-hidden />
          Copy
        </Button>
      </div>
      <p className="text-xs text-muted-foreground">
        Shown once — it is not kept. Expires {props.when(link.expires_at)}. Revoke a link the
        client abandons, so it cannot be opened later.
      </p>
      <DialogFooter>
        <Button type="button" onClick={props.onDone}>
          Done
        </Button>
      </DialogFooter>
    </div>
  )
}

function SubmissionDialog(props: {
  customerId: string
  submissionId: string
  when: (instant: string) => string
  onClose: () => void
}) {
  // Each open is a logged read (ADR-0002), and the answers are PHI: fetched fresh, never kept.
  const submission = useQuery({
    queryKey: [...FORM_SUBMISSION, props.customerId, props.submissionId],
    queryFn: () => fetchFormSubmission(props.customerId, props.submissionId),
    gcTime: 0,
    staleTime: Infinity,
    retry: false,
    refetchOnWindowFocus: false,
  })
  const data = submission.data
  return (
    <Dialog open onOpenChange={(open) => !open && props.onClose()}>
      <DialogContent className="max-h-[85dvh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>{data ? `${data.template_name} v${data.version}` : 'Completed form'}</DialogTitle>
          <DialogDescription>
            {data ? `Submitted ${props.when(data.submitted_at)}.` : 'Opening the form…'}
          </DialogDescription>
        </DialogHeader>
        {submission.isPending ? (
          <Skeleton className="h-32 w-full" />
        ) : submission.isError ? (
          <p role="alert" className="text-sm text-destructive">
            {submission.error.message}
          </p>
        ) : (
          <dl className="flex flex-col gap-4">
            {data!.fields
              .filter((f) => f.type === 'heading' || f.key in data!.answers)
              .map((f) =>
                f.type === 'heading' ? (
                  <h4 key={f.key} className="text-base font-semibold">
                    {f.label}
                  </h4>
                ) : (
                  <div key={f.key} className="flex flex-col gap-1">
                    <dt className="text-sm text-muted-foreground whitespace-pre-wrap">{f.label}</dt>
                    <dd className="text-sm">
                      <Answer field={f} value={data!.answers[f.key]} />
                    </dd>
                  </div>
                ),
              )}
          </dl>
        )}
      </DialogContent>
    </Dialog>
  )
}

function Answer(props: { field: FormSubmissionDetail['fields'][number]; value: unknown }) {
  const { field, value } = props
  switch (field.type) {
    case 'yes_no':
      return <>{value === 'yes' ? 'Yes' : 'No'}</>
    case 'multi_choice':
      return <>{(value as string[]).join(', ')}</>
    case 'acknowledgement':
      return <>Agreed</>
    case 'signature': {
      const signature = value as { name: string; image: string }
      return (
        <div className="flex flex-col gap-1">
          <img
            src={signature.image}
            alt={`Signature of ${signature.name}`}
            className="h-20 w-auto self-start rounded border bg-white"
          />
          <span>{signature.name}</span>
        </div>
      )
    }
    default:
      return <span className="whitespace-pre-wrap">{String(value)}</span>
  }
}
