import { useMutation, useQuery } from '@tanstack/react-query'
import { CircleCheck, Link2Off } from 'lucide-react'
import { useEffect, useState } from 'react'
import { useLocation } from 'react-router'
import { Form } from '@/components/form'
import { FormFieldInput } from '@/components/form-fields'
import { SIGNATURE_TOO_SMALL } from '@/components/signature-pad'
import { Button } from '@/components/ui/button'
import { Skeleton } from '@/components/ui/skeleton'
import { ApiError, fetchPublicForm, submitPublicForm } from '@/lib/api'
import { useBranding } from '@/lib/branding'
import { keptAnswers, validateAnswers, visibleKeys, type Answers, type FormField } from '@/lib/forms'

/**
 * `/f/#<token>` — the page a client opens from a form link (#46; pre-flight C6).
 *
 * **The token is the URL's fragment** (fix round 2): a browser never sends a fragment to any
 * server, so no static host, proxy or access log records it. It is read once, kept in memory,
 * and cleared from the address bar and this history entry straight away — the next person
 * handed a shared tablet cannot go back to it. A path under `/f` (the old `/f/<token>`) is
 * never looked up: it is a dead link.
 *
 * Outside every gate in `App.tsx` and without the app shell: a signed-in browser (the front
 * desk checking a link, or a tablet somebody forgot to sign out) sees exactly what the client
 * sees, and nothing here reads or sends the staff session (`fetchPublicForm` omits
 * credentials). `index.html` asks for no referrer on anything the page loads, and the
 * lookup is a POST, never cached, retried or refetched.
 *
 * **Submitting** (#47). The submission id is minted once, when the page opens, and every
 * retry resends it: if the first answer was lost on the way back, the server recognises the
 * retry ("already received") instead of refusing a used link. Answers are checked here with
 * the shared rules first (`lib/forms.ts`), and a 422's per-field codes land under the same
 * fields. A network failure keeps everything on screen. Only visible answers are sent — a
 * hidden field's leftover never leaves the device. The confirmation shows none of the answers,
 * and they are dropped from memory: the next person holding a shared tablet sees nothing.
 */
export function PublicFormPage() {
  const { hash } = useLocation()
  // Read once, on mount; the router's copy of the location keeps the fragment, the address
  // bar does not.
  const [token] = useState(() => hash.replace(/^#/, ''))
  useEffect(() => {
    window.history.replaceState(null, '', '/f/')
  }, [])
  const branding = useBranding()
  const form = useQuery({
    queryKey: ['public-form', token],
    queryFn: () => fetchPublicForm(token),
    enabled: token !== '',
    retry: false,
    staleTime: Infinity,
    gcTime: 0,
    refetchOnWindowFocus: false,
  })
  const [answers, setAnswers] = useState<Answers>({})
  const [submissionId] = useState(() => crypto.randomUUID())
  const [errors, setErrors] = useState<Record<string, string>>({})
  // A drawn or typed signature that does not clear the server's ink thresholds: kept back
  // from Submit rather than round-tripped for the server to refuse the same way.
  const [weakSignature, setWeakSignature] = useState(false)
  const submit = useMutation({
    mutationFn: (payload: { version_id: string; answers: Answers }) =>
      submitPublicForm({ token, submission_id: submissionId, ...payload }),
    onSuccess: () => setAnswers({}),
    onError: (error) => {
      const refused = error instanceof ApiError && error.code === 'invalid_answers'
      if (refused) setErrors((error.body as { errors?: Record<string, string> }).errors ?? {})
    },
  })

  // No fragment — a reload after it was cleared, or the old path form — is a dead link.
  if (form.isPending && token !== '') {
    return (
      <Page>
        <Skeleton className="h-8 w-2/3" />
        <Skeleton className="h-48 w-full" />
      </Page>
    )
  }
  const refusal = submit.error instanceof ApiError ? submit.error : null
  if (form.isError || !form.data || refusal?.status === 404 || refusal?.code === 'version_mismatch') {
    const business = form.data?.business.name ?? branding.data?.name ?? 'the business'
    const dead = !form.isError || (form.error instanceof ApiError && form.error.status === 404)
    return (
      <Page>
        {refusal?.code === 'version_mismatch' ? (
          <DeadEnd title="This form has changed" text={`Please ask ${business} for a new link.`} />
        ) : dead ? (
          <DeadEnd title="This link is no longer valid" text={`Please contact ${business} for a new one.`} />
        ) : (
          <DeadEnd
            title="This form could not be opened"
            text={`Try again in a minute. If it keeps happening, please contact ${business}.`}
          />
        )}
      </Page>
    )
  }

  const { schema, business, client_first_name, template_name, version_id } = form.data
  if (submit.isSuccess) {
    return (
      <Page>
        <div className="flex flex-col items-center gap-3 rounded-xl border px-6 py-12 text-center">
          <CircleCheck className="size-6 text-primary" aria-hidden />
          <h1 className="text-lg font-semibold">Thank you</h1>
          <p>{business.name} has received your form.</p>
          <p className="max-w-sm text-muted-foreground">
            You can close this page. If you were handed this device, please give it back.
          </p>
        </div>
      </Page>
    )
  }

  const shown = new Set(visibleKeys(schema, answers))
  const send = () => {
    const found = validateAnswers(schema, answers)
    setErrors(found)
    if (Object.keys(found).length === 0 && !weakSignature) {
      submit.mutate({ version_id, answers: keptAnswers(schema, answers) })
    }
  }
  const unsent = submit.isError && refusal?.code !== 'invalid_answers'
  return (
    <Page>
      <header className="flex items-center gap-2.5 font-semibold">
        {business.logo_url ? (
          <img src={business.logo_url} alt="" className="h-8 w-auto max-w-40 object-contain" />
        ) : (
          <span aria-hidden className="size-6 rounded-md bg-primary" />
        )}
        <span>{business.name}</span>
      </header>
      <div>
        <p className="text-muted-foreground">Hi {client_first_name},</p>
        <h1 className="text-xl font-semibold">{template_name}</h1>
      </div>
      <Form onSubmit={send}>
        {schema.fields
          .filter((f) => shown.has(f.key))
          .map((f) => (
            <FormFieldInput
              key={f.key}
              field={f}
              value={answers[f.key]}
              error={errors[f.key] && message(f, errors[f.key], answers[f.key])}
              onChange={(v) => setAnswers((a) => ({ ...a, [f.key]: v }))}
              onSignatureWeakChange={f.type === 'signature' ? setWeakSignature : undefined}
            />
          ))}
        {unsent && (
          <p role="alert" className="text-sm text-destructive">
            Your form could not be sent. Check your connection and try again — your answers are
            still here.
          </p>
        )}
        <Button type="submit" className="self-start" disabled={submit.isPending || weakSignature}>
          Submit
        </Button>
      </Form>
    </Page>
  )
}

/** What a per-field code from `validateAnswers` (or the server's 422) says to the client. A
 *  signature answer with both a name and an image already on it, refused as `invalid`, can
 *  only be the server's own ink check (the client-side one already holds Submit back before
 *  a half-filled pad — an untyped name or an undrawn pad — ever reaches this code at all), so
 *  that specific case gets the specific hint; anything else with a signature gets the general
 *  prompt to finish it. */
function message(field: FormField, code: string, value?: unknown): string {
  if (field.type === 'signature') {
    const signed = value as { name?: string; image?: string } | undefined
    const complete = Boolean(signed?.name?.trim()) && Boolean(signed?.image?.trim())
    return code === 'invalid' && complete
      ? SIGNATURE_TOO_SMALL
      : 'Draw your signature and type your full name.'
  }
  return code === 'required' ? 'This is required.' : 'Check this answer.'
}

function DeadEnd(props: { title: string; text: string }) {
  return (
    <div className="flex flex-col items-center gap-3 rounded-xl border border-dashed px-6 py-12 text-center">
      <Link2Off className="size-6 text-muted-foreground" aria-hidden />
      <h1 className="text-lg font-semibold">{props.title}</h1>
      <p className="max-w-sm text-muted-foreground">{props.text}</p>
    </div>
  )
}

function Page({ children }: { children: React.ReactNode }) {
  return (
    <main className="mx-auto flex min-h-dvh w-full max-w-2xl flex-col gap-6 px-4 py-8">{children}</main>
  )
}
