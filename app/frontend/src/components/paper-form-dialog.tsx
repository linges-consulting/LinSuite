import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { toast } from 'sonner'
import { ChoiceSelect } from '@/components/choice-select'
import { Form } from '@/components/form'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Skeleton } from '@/components/ui/skeleton'
import {
  ApiError,
  fetchBlankForm,
  fetchPaperVersions,
  fetchSendableForms,
  uploadFormScan,
} from '@/lib/api'
import {
  COMPLIANCE,
  CUSTOMER_PROFILE,
  FORMS_NEEDED,
  FORM_SUBMISSIONS,
  SENDABLE_FORMS,
} from '@/lib/query-keys'
import { downsampleScan } from '@/lib/scan-images'

export function PaperFormDialog(props: {
  customerId: string
  mode: 'print' | 'scan'
  onClose: () => void
}) {
  const queryClient = useQueryClient()
  const forms = useQuery({
    queryKey: SENDABLE_FORMS,
    queryFn: fetchSendableForms,
  })
  const [templateId, setTemplateId] = useState('')
  const [version, setVersion] = useState(1)
  const [pages, setPages] = useState<string[]>([])
  const [error, setError] = useState('')
  const [preparing, setPreparing] = useState(false)
  const [submissionId] = useState(() => crypto.randomUUID())
  const versions = useQuery({
    queryKey: ['paper-versions', templateId],
    queryFn: () => fetchPaperVersions(templateId),
    enabled: Boolean(templateId),
  })
  const save = useMutation({
    gcTime: 0,
    mutationFn: async (preview: Window | null) => {
      if (props.mode === 'print') {
        const html = await fetchBlankForm(templateId, version)
        const printable = html.replace(
          '</body>',
          '<script>window.addEventListener("load",()=>window.print())</script></body>',
        )
        const url = URL.createObjectURL(
          new Blob([printable], { type: 'text/html' }),
        )
        preview!.location.replace(url)
        window.setTimeout(() => URL.revokeObjectURL(url), 60_000)
      } else {
        await uploadFormScan(props.customerId, {
          submission_id: submissionId,
          template_id: templateId,
          version_number: version,
          pages: pages.map((page) => page.split(',')[1]),
        })
      }
    },
    onSuccess: () => {
      toast.success(
        props.mode === 'scan'
          ? 'Scanned form saved'
          : 'Blank form opened for printing',
      )
      if (props.mode === 'scan') {
        queryClient.invalidateQueries({
          queryKey: [...FORM_SUBMISSIONS, props.customerId],
        })
        queryClient.invalidateQueries({ queryKey: COMPLIANCE })
        queryClient.invalidateQueries({ queryKey: FORMS_NEEDED })
        queryClient.invalidateQueries({
          queryKey: [...CUSTOMER_PROFILE, props.customerId],
        })
      }
      props.onClose()
    },
    onError: (_, preview) => preview?.close(),
  })
  const send = () => {
    let preview: Window | null = null
    if (props.mode === 'print') {
      preview = window.open('', '_blank')
      if (!preview) {
        setError('Allow pop-ups to print the form.')
        return
      }
      preview.opener = null
    }
    save.mutate(preview)
  }
  // The server may already have received a failed request. Keep its id bound to these pages.
  const unconfirmed =
    props.mode === 'scan' &&
    save.isError &&
    (!(save.error instanceof ApiError) ||
      save.error.status >= 500 ||
      save.error.status === 429 ||
      save.error.code === 'try_again')
  const prepare = async (files: FileList | null) => {
    save.reset()
    setError('')
    setPages([])
    if (!files || files.length === 0) return
    if (files.length > 8) {
      setError('Choose up to eight pages.')
      return
    }
    setPreparing(true)
    try {
      const result: string[] = []
      for (const file of Array.from(files))
        result.push(await downsampleScan(file))
      setPages(result)
    } catch (reason) {
      setError(
        reason instanceof Error
          ? reason.message
          : 'Could not prepare the pages.',
      )
    } finally {
      setPreparing(false)
    }
  }
  return (
    <Dialog
      open
      onOpenChange={(open) => !open && !save.isPending && props.onClose()}
    >
      <DialogContent>
        <DialogHeader>
          <DialogTitle>
            {props.mode === 'print' ? 'Print blank form' : 'Upload scan'}
          </DialogTitle>
          <DialogDescription>
            {props.mode === 'print'
              ? 'Print a published version for the client to fill in.'
              : 'Choose the version the client signed and up to eight JPEG or PNG pages.'}
          </DialogDescription>
        </DialogHeader>
        <Form onSubmit={send}>
          <div className="grid gap-2">
            <Label htmlFor="paper-template">Form template</Label>
            {forms.isPending ? (
              <Skeleton className="h-9 w-full" />
            ) : (
              <ChoiceSelect
                id="paper-template"
                value={templateId}
                disabled={save.isPending || unconfirmed}
                placeholder="Choose a form"
                onValueChange={(value) => {
                  setTemplateId(value)
                  setVersion(
                    forms.data?.find((f) => f.template_id === value)?.version ??
                      1,
                  )
                }}
                options={
                  forms.data?.map((f) => ({
                    value: f.template_id,
                    label: f.name,
                  })) ?? []
                }
              />
            )}
          </div>
          <div className="grid gap-2">
            <Label htmlFor="paper-version">Published version</Label>
            {templateId && versions.isPending ? (
              <Skeleton className="h-9 w-full" />
            ) : (
              <ChoiceSelect
                id="paper-version"
                value={String(version)}
                disabled={
                  !versions.data?.length || save.isPending || unconfirmed
                }
                onValueChange={(value) => setVersion(Number(value))}
                options={
                  versions.data?.map((v) => ({
                    value: String(v.number),
                    label: `Version ${v.number} — ${v.name}`,
                  })) ?? []
                }
              />
            )}
          </div>
          {props.mode === 'scan' && (
            <div className="grid gap-2">
              <Label htmlFor="scan-pages">Scanned pages</Label>
              <Input
                id="scan-pages"
                type="file"
                accept="image/jpeg,image/png"
                multiple
                capture="environment"
                disabled={save.isPending || preparing || unconfirmed}
                onChange={(e) => void prepare(e.target.files)}
              />
              {preparing && (
                <p role="status" className="text-sm text-muted-foreground">
                  Preparing pages…
                </p>
              )}
              <div className="grid grid-cols-4 gap-2">
                {pages.map((page, i) => (
                  <img
                    key={i}
                    src={page}
                    alt={`Scanned page ${i + 1}`}
                    className="h-28 w-full rounded border object-contain"
                  />
                ))}
              </div>
            </div>
          )}
          {(error || save.error || forms.error || versions.error) && (
            <p role="alert" className="text-sm text-destructive">
              {error ||
                save.error?.message ||
                forms.error?.message ||
                versions.error?.message}
            </p>
          )}
          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              disabled={save.isPending}
              onClick={props.onClose}
            >
              Cancel
            </Button>
            <Button
              type="submit"
              disabled={
                !templateId ||
                !versions.data?.length ||
                preparing ||
                save.isPending ||
                (props.mode === 'scan' && !pages.length)
              }
            >
              {props.mode === 'print' ? 'Print' : 'Save scan'}
            </Button>
          </DialogFooter>
        </Form>
      </DialogContent>
    </Dialog>
  )
}
