import { useQuery } from '@tanstack/react-query'
import { Navigate, Route, Routes } from 'react-router'
import { AppShell } from '@/components/app-shell'
import { fetchSetupStatus } from '@/lib/api'
import { useSession } from '@/lib/auth'
import { useApplyBranding } from '@/lib/branding'
import { ChangePasswordPage } from '@/routes/change-password'
import { ClientPage, ClientsPage } from '@/routes/clients'
import { ForgotPasswordPage } from '@/routes/forgot-password'
import { HomePage } from '@/routes/home'
import { LoginPage } from '@/routes/login'
import { MfaEnrolPage } from '@/routes/mfa-enrol'
import { MfaVerifyPage } from '@/routes/mfa-verify'
import { PlaceholderPage } from '@/routes/placeholder'
import { ResetPasswordPage } from '@/routes/reset-password'
import { SchedulePage } from '@/routes/schedule'
import { SecurityPage } from '@/routes/security'
import { SettingsPage } from '@/routes/settings'
import { SetupPage } from '@/routes/setup'

/**
 * Six states, in order: an unclaimed instance goes to the wizard, an anonymous visitor to the
 * login screen, then the three things a session can owe — the second factor it has not
 * presented, a new password, the enrolment its business requires — and everyone else into
 * the shell. `/setup` and `/login` stay routed in every state so each can explain itself
 * rather than bounce.
 *
 * **The order is which screen's own calls the server will answer**, not the order the checks
 * happen to be written in. `CurrentUser` asks for the password change first, but
 * `POST /auth/password/change` itself refuses a session that has not presented its second
 * factor — so a session owing both has to verify before it can change anything, and sending
 * it to the change screen first would be a form that cannot be submitted. Verify, then the
 * password, then the enrolment.
 *
 * Each gate is a redirect rather than a swapped element, so the address bar says what is
 * happening and a reload lands back where it was. They sit *above* the shell: while anything
 * is owed there is no route that renders the application.
 */
export default function App() {
  // Outside the gates: the login screen and the browser tab are this business's too, and
  // the branding document is anonymous precisely so they can be.
  useApplyBranding()
  return <AppRoutes />
}

function AppRoutes() {
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
  ) : user.mfa.pending ? (
    <Navigate to="/mfa" replace />
  ) : user.must_change_password ? (
    <Navigate to="/change-password" replace />
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
          ) : user.mfa.pending ? (
            // The change endpoint refuses a pending session, so this screen would be a form
            // that cannot be submitted. Verifying comes first and the change is still owed
            // afterwards.
            <Navigate to="/mfa" replace />
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
          ) : user.must_change_password ? (
            // Verified, and now the change is what is owed. Not "/" — the gate would only
            // send the browser here again.
            <Navigate to="/change-password" replace />
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
          ) : user.must_change_password ? (
            // The server's `enrolling_user` refuses every endpoint on this screen with
            // `password_change_required` while a change is owed, so rendering it would be a
            // dead end: a QR code that cannot be confirmed, on a screen with no way out.
            // The order here is the server's order.
            <Navigate to="/change-password" replace />
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
        <Route path="schedule" element={<SchedulePage />} />
        <Route path="clients" element={<ClientsPage />} />
        <Route path="clients/:id" element={<ClientPage />} />
        <Route path="catalog" element={<PlaceholderPage title="Catalog" />} />
        <Route path="settings" element={<SettingsPage />} />
        <Route path="security" element={<SecurityPage />} />
      </Route>
    </Routes>
  )
}
