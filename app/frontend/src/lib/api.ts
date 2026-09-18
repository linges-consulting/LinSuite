export type Health = { status: 'ok' | 'degraded'; database: 'ok' | 'unreachable' }

export type SetupStatus = { required: boolean }

export type SetupPayload = {
  token: string
  business_name: string
  timezone: string
  admin_email: string
  admin_password: string
}

/** Answers whether this instance is still unclaimed; available before and after setup. */
export async function fetchSetupStatus(): Promise<SetupStatus> {
  const res = await fetch('/api/setup/status')
  if (!res.ok) throw new Error(`Setup status failed: HTTP ${res.status}`)
  return res.json()
}

export async function fetchTimezones(): Promise<string[]> {
  const res = await fetch('/api/setup/timezones')
  if (!res.ok) throw new Error(`Timezone list failed: HTTP ${res.status}`)
  return (await res.json()).timezones
}

export class SetupError extends Error {
  status: number

  constructor(message: string, status: number) {
    super(message)
    this.status = status
  }
}

export async function completeSetup(payload: SetupPayload): Promise<void> {
  const res = await fetch('/api/setup', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
  if (!res.ok) throw new SetupError(await problem(res), res.status)
}

/** FastAPI's `detail` is a string for our own errors and a list for validation failures. */
async function problem(res: Response): Promise<string> {
  const detail = await res
    .json()
    .then((body) => body?.detail)
    .catch(() => null)
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail) && detail[0]?.msg) return detail[0].msg
  return `Setup failed: HTTP ${res.status}`
}

export async function fetchHealth(): Promise<Health> {
  const res = await fetch('/api/health')
  // 503 still carries a valid body; anything else is a real failure.
  if (!res.ok && res.status !== 503) throw new Error(`Health check failed: HTTP ${res.status}`)
  return res.json()
}
