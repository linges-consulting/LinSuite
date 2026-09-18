import { useQuery } from '@tanstack/react-query'
import { Navigate, Route, Routes } from 'react-router'
import { AppShell } from '@/components/app-shell'
import { fetchSetupStatus } from '@/lib/api'
import { useSession } from '@/lib/auth'
import { HomePage } from '@/routes/home'
import { LoginPage } from '@/routes/login'
import { PlaceholderPage } from '@/routes/placeholder'
import { SetupPage } from '@/routes/setup'

/**
 * Three states, in order: an unclaimed instance goes to the wizard, an anonymous visitor to
 * the login screen, and everyone else into the shell. `/setup` and `/login` stay routed in
 * every state so each can explain itself rather than bounce.
 */
export default function App() {
  const { data: setup, isPending: setupPending } = useQuery({
    queryKey: ['setup-status'],
    queryFn: fetchSetupStatus,
    staleTime: Infinity,
    retry: false,
  })
  const { user, isPending: sessionPending } = useSession()

  // Nothing renders until both answers are in — an unclaimed instance must not flash the
  // dashboard on its way to the wizard, nor a signed-in user the login screen.
  if (setupPending || sessionPending) return null

  const toSetup = <Navigate to="/setup" replace />
  const gate = setup?.required ? toSetup : user ? <AppShell /> : <Navigate to="/login" replace />
  // An unclaimed instance has no accounts yet, so signing in is not an option there either.
  const loginRoute = setup?.required ? toSetup : user ? <Navigate to="/" replace /> : <LoginPage />

  return (
    <Routes>
      <Route path="/setup" element={<SetupPage />} />
      <Route path="/login" element={loginRoute} />
      <Route element={gate}>
        <Route index element={<HomePage />} />
        <Route path="schedule" element={<PlaceholderPage title="Schedule" />} />
        <Route path="clients" element={<PlaceholderPage title="Clients" />} />
        <Route path="catalog" element={<PlaceholderPage title="Catalog" />} />
        <Route path="settings" element={<PlaceholderPage title="Settings" />} />
      </Route>
    </Routes>
  )
}
