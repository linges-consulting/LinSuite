import { useQuery } from '@tanstack/react-query'
import { Navigate, Route, Routes } from 'react-router'
import { AppShell } from '@/components/app-shell'
import { fetchSetupStatus } from '@/lib/api'
import { useSession } from '@/lib/auth'
import { ChangePasswordPage } from '@/routes/change-password'
import { ForgotPasswordPage } from '@/routes/forgot-password'
import { HomePage } from '@/routes/home'
import { LoginPage } from '@/routes/login'
import { MfaEnrolPage } from '@/routes/mfa-enrol'
import { MfaVerifyPage } from '@/routes/mfa-verify'
import { PlaceholderPage } from '@/routes/placeholder'
import { ResetPasswordPage } from '@/routes/reset-password'
import { SecurityPage } from '@/routes/security'
import { SettingsPage } from '@/routes/settings'
import { SetupPage } from '@/routes/setup'

/**
 * Six states, in order: an unclaimed instance goes to the wizard, an anonymous visitor to the
 * login screen, then the three things a session can owe — a new password, the second factor
 * it has not presented, the enrolment its business requires — and everyone else into the
 * shell. `/setup` and `/login` stay routed in every state so each can explain itself rather
 * than bounce.
 *
 * The order matches the server's (`auth/session.py`), and it has to: routing to a screen
 * whose own calls the server would refuse for a different reason is a loop. A password
 * change comes first, then the code this session owes, then the enrolment.
 *
 * Each gate is a redirect rather than a swapped element, so the address bar says what is
 * happening and a reload lands back where it was. They sit *above* the shell: while anything
 * is owed there is no route that renders the application.
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
  ) : user.mfa.pending ? (
    <Navigate to="/mfa" replace />
  ) : user.mfa.enrolment_required ? (
    <Navigate to="/mfa/enrol" replace />
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
      <Route
        path="/mfa"
        element={
          setup?.required ? (
            toSetup
          ) : !user ? (
            <Navigate to="/login" replace />
          ) : user.mfa.pending ? (
            <MfaVerifyPage />
          ) : (
            // Both directions hang off the one flag, so the gate and this route can never
            // disagree about which screen is showing and bounce the browser between them.
            <Navigate to="/" replace />
          )
        }
      />
      <Route
        path="/mfa/enrol"
        element={
          setup?.required ? (
            toSetup
          ) : !user ? (
            <Navigate to="/login" replace />
          ) : user.mfa.pending ? (
            <Navigate to="/mfa" replace />
          ) : (
            // Unlike the other gate screens this one is also reachable by choice, from the
            // Security page — so it renders whether or not the policy is demanding it, and
            // only its copy and its way out change (`gated`).
            <MfaEnrolPage gated={user.mfa.enrolment_required} />
          )
        }
      />
      <Route element={gate}>
        <Route index element={<HomePage />} />
        <Route path="schedule" element={<PlaceholderPage title="Schedule" />} />
        <Route path="clients" element={<PlaceholderPage title="Clients" />} />
        <Route path="catalog" element={<PlaceholderPage title="Catalog" />} />
        <Route path="settings" element={<SettingsPage />} />
        <Route path="security" element={<SecurityPage />} />
      </Route>
    </Routes>
  )
}
