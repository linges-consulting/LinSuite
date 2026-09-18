import { useQuery } from '@tanstack/react-query'
import { Navigate, Route, Routes } from 'react-router'
import { AppShell } from '@/components/app-shell'
import { fetchSetupStatus } from '@/lib/api'
import { useSession } from '@/lib/auth'
import { ChangePasswordPage } from '@/routes/change-password'
import { ForgotPasswordPage } from '@/routes/forgot-password'
import { HomePage } from '@/routes/home'
import { LoginPage } from '@/routes/login'
import { PlaceholderPage } from '@/routes/placeholder'
import { ResetPasswordPage } from '@/routes/reset-password'
import { SetupPage } from '@/routes/setup'

/**
 * Four states, in order: an unclaimed instance goes to the wizard, an anonymous visitor to
 * the login screen, an account that must change its password to that screen and nothing
 * else, and everyone else into the shell. `/setup` and `/login` stay routed in every state
 * so each can explain itself rather than bounce.
 *
 * The forced change is a redirect rather than a swapped element, so the address bar says
 * what is happening and a reload lands back where it was. It sits *above* the shell: while
 * the flag is set there is no route that renders the application.
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
  const gate = setup?.required ? (
    toSetup
  ) : !user ? (
    <Navigate to="/login" replace />
  ) : user.must_change_password ? (
    <Navigate to="/change-password" replace />
  ) : (
    <AppShell />
  )
  // An unclaimed instance has no accounts yet, so signing in — or resetting a password
  // there is no account for — is not an option either.
  const anonymous = (element: React.ReactNode) =>
    setup?.required ? toSetup : user ? <Navigate to="/" replace /> : element

  return (
    <Routes>
      <Route path="/setup" element={<SetupPage />} />
      <Route path="/login" element={anonymous(<LoginPage />)} />
      <Route path="/forgot-password" element={anonymous(<ForgotPasswordPage />)} />
      <Route path="/reset-password" element={anonymous(<ResetPasswordPage />)} />
      <Route
        path="/change-password"
        element={
          setup?.required ? (
            toSetup
          ) : !user ? (
            <Navigate to="/login" replace />
          ) : user.must_change_password ? (
            <ChangePasswordPage />
          ) : (
            // Both directions hang off the one flag, so the gate and this route can never
            // disagree about which screen is showing and bounce the browser between them.
            <Navigate to="/" replace />
          )
        }
      />
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
