import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Copy, Send } from 'lucide-react'
import { useState } from 'react'
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
  fetchOpenFormLinks,
  fetchSendableForms,
  issueFormLink,
  revokeFormLink,
  type IssuedFormLink,
} from '@/lib/api'
import { FORM_LINKS, SENDABLE_FORMS } from '@/lib/query-keys'

/**
 * The profile's "Forms" card (`forms.issue`, Staff Mode; #46): send a form, and see and revoke
 * the links still open.
 *
 * **The link is shown once.** The server keeps only its hash, so the URL in the issue answer
 * is the only copy outside the client's email: the dialog shows it with Copy and a QR code —
 * the in-clinic flow is a tablet that is never signed in, which scans the code and is handed
 * to the client — and it is gone when the dialog closes.
 */
export function ClientFormsCard(props: { customerId: string; timezone: string; suppressed: boolean }) {
  const [sending, setSending] = useState(false)
  const queryClient = useQueryClient()
  const links = useQuery({
    queryKey: [...FORM_LINKS, props.customerId],
    queryFn: () => fetchOpenFormLinks(props.customerId),
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
        {!props.suppressed && (
          <Button size="sm" variant="outline" onClick={() => setSending(true)}>
            <Send aria-hidden />
            Send form
          </Button>
        )}
      </CardHeader>
      <CardContent>
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
      </CardContent>
      {sending && (
        <SendFormDialog
          customerId={props.customerId}
          when={when}
          onClose={() => setSending(false)}
        />
      )}
    </Card>
  )
}

function SendFormDialog(props: {
  customerId: string
  when: (instant: string) => string
  onClose: () => void
}) {
  const queryClient = useQueryClient()
  const [templateId, setTemplateId] = useState('')
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
        Shown once — it is not kept. Expires {props.when(link.expires_at)}.
      </p>
      <DialogFooter>
        <Button type="button" onClick={props.onDone}>
          Done
        </Button>
      </DialogFooter>
    </div>
  )
}
