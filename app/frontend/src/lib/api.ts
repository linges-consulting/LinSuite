export type Health = { status: 'ok' | 'degraded'; database: 'ok' | 'unreachable' }

export async function fetchHealth(): Promise<Health> {
  const res = await fetch('/api/health')
  // 503 still carries a valid body; anything else is a real failure.
  if (!res.ok && res.status !== 503) throw new Error(`Health check failed: HTTP ${res.status}`)
  return res.json()
}
