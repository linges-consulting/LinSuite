import { useMutation, useQuery } from '@tanstack/react-query'
import { CircleCheck } from 'lucide-react'
import { useEffect, useState, useRef } from 'react'
import { useLocation } from 'react-router'
import { DeadEnd } from '@/components/dead-end'
import { Form } from '@/components/form'
import { FormFieldInput } from '@/components/form-fields'
import { SIGNATURE_TOO_SMALL } from '@/components/signature-pad'
import { Button } from '@/components/ui/button'
import { Skeleton } from '@/components/ui/skeleton'
import { ApiError, fetchPublicForm, submitPublicForm, type FormSubmitPayload } from '@/lib/api'
import { useBranding } from '@/lib/branding'
import { keptAnswers, validateAnswers, visibleKeys, type Answers, type FormField } from '@/lib/forms'
import { activeFormToken, rememberActiveForm, forgetActiveForm, readCachedForm, saveCachedForm, deleteCachedForm } from '@/lib/form-cache'

/**
 * `/f/#<token>` — the page a client opens from a form link (#46; pre-flight C6).
 *
 * **The token is the URL's fragment** (fix round 2): a browser never sends a fragment to any
 * server, so no static host, proxy or access log records it. It is read once, kept in memory,
 * and cleared from the address bar and this history entry straight away. A short-lived
 * IndexedDB draft and a session-storage pointer support reloads; successful or terminal
 * submissions remove them, and expired drafts are purged on the next page load. A path under `/f` (the old `/f/<token>`) is
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
  const { hash, pathname } = useLocation()
  // Read once, on mount; the router's copy of the location keeps the fragment, the address
  // bar does not.
  const [token] = useState(() => hash.replace(/^#/, '') ||
    ((pathname === '/f/' || pathname === '/f') ? activeFormToken() : ''))
  useEffect(() => {
    window.history.replaceState(null, '', '/f/')
  }, [])
  const branding = useBranding()
  const [answers, setAnswers] = useState<Answers>({})
  const [submissionId, setSubmissionId] = useState<string>(() => crypto.randomUUID())
  const [savedLocally, setSavedLocally] = useState(false)
  const [offlineReloadReady, setOfflineReloadReady] = useState(false)
  useEffect(() => {
    // Production assets are hashed and precached; Vite's changing development modules are not.
    if (!import.meta.env.PROD || !('serviceWorker' in navigator)) return
    let mounted = true
    void navigator.serviceWorker.register('/form-offline-worker.js', { scope: '/f/' })
      .then(() => navigator.serviceWorker.ready)
      .then(() => { if (mounted) setOfflineReloadReady(true) })
      .catch(() => { /* Draft recovery still works while this page remains open. */ })
    return () => { mounted = false }
  }, [])
  const [queued, setQueued] = useState(false)
  const queuedPayload = useRef<FormSubmitPayload | null>(null)
  const inFlight = useRef(false)
  const durable = useRef(false)
  const form = useQuery({
    queryKey: ['public-form', token],
    queryFn: async () => {
      const cached = await readCachedForm(token).catch(() => undefined)
      let data
      try { data = cached?.queued ? cached.form : await fetchPublicForm(token) }
      catch (error) {
        if (error instanceof ApiError && error.status === 404) {
          await deleteCachedForm(token).catch(() => {})
          forgetActiveForm()
          throw error
        }
        if (!cached) throw error
        data = cached.form
      }
      if (cached && cached.form.version_id === data.version_id) {
        setAnswers(cached.answers)
        setSubmissionId(cached.submissionId)
        if (cached.queued) {
          queuedPayload.current = { token, submission_id: cached.submissionId,
            version_id: cached.form.version_id, answers: keptAnswers(cached.form.schema, cached.answers) }
          setQueued(true)
        }
      }
      rememberActiveForm(token)
      return data
    },
    enabled: token !== '',
    retry: false,
    staleTime: Infinity,
    gcTime: 0,
    refetchOnWindowFocus: false,
  })
  const [errors, setErrors] = useState<Record<string, string>>({})
  // A drawn or typed signature that does not clear the server's ink thresholds: kept back
  // from Submit rather than round-tripped for the server to refuse the same way.
  const [weakSignature, setWeakSignature] = useState(false)
  const submit = useMutation({
    mutationFn: async (payload: FormSubmitPayload) => {
      durable.current = false
      if (form.data) {
        try {
          await saveCachedForm({ token, form: form.data, answers: payload.answers,
            submissionId: payload.submission_id, queued: true })
          durable.current = true
          setSavedLocally(true)
        } catch { /* Storage unavailable: the on-screen retry still works. */ }
      }
      return submitPublicForm(payload)
    },
    onSuccess: async () => {
      queuedPayload.current = null
      setQueued(false)
      setAnswers({})
      forgetActiveForm()
      await deleteCachedForm(token).catch(() => {})
    },
    onError: async (error, payload) => {
      const refused = error instanceof ApiError && error.code === 'invalid_answers'
      if (refused) setErrors((error.body as { errors?: Record<string, string> }).errors ?? {})
      const dead = error instanceof ApiError && (error.status === 404 || error.code === 'version_mismatch')
      if (dead) {
        queuedPayload.current = null
        setQueued(false)
        setAnswers({})
        forgetActiveForm()
        await deleteCachedForm(token).catch(() => {})
      } else {
        const transient = !(error instanceof ApiError) || error.status >= 500 || error.status === 429 || error.code === 'try_again'
        const retry = transient && durable.current
        queuedPayload.current = retry ? payload : null
        setQueued(retry)
        if (!retry && form.data) await saveCachedForm({ token, form: form.data,
          answers: payload.answers, submissionId: payload.submission_id, queued: false }).catch(() => {})
      }
    },
    onSettled: () => { inFlight.current = false },
  })
  const dispatch = (payload: FormSubmitPayload) => {
    if (inFlight.current) return
    inFlight.current = true
    submit.mutate(payload)
  }
  useEffect(() => {
    if (!form.data || submit.isSuccess || submit.isPending || queued ||
      (submit.error instanceof ApiError && (submit.error.status === 404 || submit.error.code === 'version_mismatch'))) return
    const timer = window.setTimeout(() => {
      void saveCachedForm({ token, form: form.data!, answers, submissionId, queued: false }).catch(() => {})
    }, 500)
    return () => window.clearTimeout(timer)
  }, [token, form.data, answers, submissionId, submit.isSuccess, submit.isPending, submit.error, queued])
  useEffect(() => {
    if (!queued || !form.data) return
    const retry = () => {
      if (queuedPayload.current && navigator.onLine && !inFlight.current) dispatch(queuedPayload.current)
    }
    window.addEventListener('online', retry)
    const timer = window.setInterval(retry, 15_000)
    return () => { window.removeEventListener('online', retry); window.clearInterval(timer) }
  })

  // Without a fragment or a remembered draft, an old path is a dead link.
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
          <DeadEnd title="This form has changed" text="Please ask staff for a new link." />
        ) : dead ? (
          <DeadEnd title="This link is no longer valid" text={savedLocally ? 'Please ask staff for a new link.' : `Please contact ${business} for a new one.`} />
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
    if (queuedPayload.current) { dispatch(queuedPayload.current); return }
    const found = validateAnswers(schema, answers)
    setErrors(found)
    if (Object.keys(found).length === 0 && !weakSignature) {
      dispatch({ token, submission_id: submissionId, version_id, answers: keptAnswers(schema, answers) })
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
        {offlineReloadReady && <p role="status" className="text-sm text-muted-foreground">Ready for offline reload on this device.</p>}
        <fieldset disabled={queued || submit.isPending} className={`flex flex-col gap-6 ${queued ? 'pointer-events-none' : ''}`}>
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
        </fieldset>
        {queued && <p role="status" className="text-sm text-muted-foreground">
          Saved on this device — will send when the connection returns.
        </p>}
        {unsent && !queued && (
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

function Page({ children }: { children: React.ReactNode }) {
  return (
    <main className="mx-auto flex min-h-dvh w-full max-w-2xl flex-col gap-6 px-4 py-8">{children}</main>
  )
}
