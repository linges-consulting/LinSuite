import { useQuery } from '@tanstack/react-query'
import { Link2Off } from 'lucide-react'
import { useEffect, useState } from 'react'
import { useLocation } from 'react-router'
import { FormFieldInput } from '@/components/form-fields'
import { Skeleton } from '@/components/ui/skeleton'
import { ApiError, fetchPublicForm } from '@/lib/api'
import { useBranding } from '@/lib/branding'
import { visibleKeys, type Answers } from '@/lib/forms'

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
 * Submitting arrives with Task 4; until then the form renders with no submit button.
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

  // No fragment — a reload after it was cleared, or the old path form — is a dead link.
  if (form.isPending && token !== '') {
    return (
      <Page>
        <Skeleton className="h-8 w-2/3" />
        <Skeleton className="h-48 w-full" />
      </Page>
    )
  }
  if (form.isError || !form.data) {
    const business = branding.data?.name ?? 'the business'
    const dead = !form.isError || (form.error instanceof ApiError && form.error.status === 404)
    return (
      <Page>
        <div className="flex flex-col items-center gap-3 rounded-xl border border-dashed px-6 py-12 text-center">
          <Link2Off className="size-6 text-muted-foreground" aria-hidden />
          <h1 className="text-lg font-semibold">
            {dead ? 'This link is no longer valid' : 'This form could not be opened'}
          </h1>
          <p className="max-w-sm text-muted-foreground">
            {dead
              ? `Please contact ${business} for a new one.`
              : `Try again in a minute. If it keeps happening, please contact ${business}.`}
          </p>
        </div>
      </Page>
    )
  }

  const { schema, business, client_first_name, template_name } = form.data
  const shown = new Set(visibleKeys(schema, answers))
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
      <div className="flex flex-col gap-5">
        {schema.fields
          .filter((f) => shown.has(f.key))
          .map((f) => (
            <FormFieldInput
              key={f.key}
              field={f}
              value={answers[f.key]}
              onChange={(v) => setAnswers((a) => ({ ...a, [f.key]: v }))}
            />
          ))}
      </div>
    </Page>
  )
}

function Page({ children }: { children: React.ReactNode }) {
  return (
    <main className="mx-auto flex min-h-dvh w-full max-w-2xl flex-col gap-6 px-4 py-8">{children}</main>
  )
}
