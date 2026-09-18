import { useQuery } from '@tanstack/react-query'
import { Navigate, Route, Routes } from 'react-router'
import { AppShell } from '@/components/app-shell'
import { fetchSetupStatus } from '@/lib/api'
import { HomePage } from '@/routes/home'
import { PlaceholderPage } from '@/routes/placeholder'
import { SetupPage } from '@/routes/setup'

export default function App() {
  const { data, isPending } = useQuery({
    queryKey: ['setup-status'],
    queryFn: fetchSetupStatus,
    staleTime: Infinity,
    retry: false,
  })

  // Nothing renders until we know whether this instance has been claimed — an unclaimed
  // one must not flash the dashboard on its way to the wizard.
  if (isPending) return null

  return (
    <Routes>
      <Route path="/setup" element={<SetupPage />} />
      <Route element={data?.required ? <Navigate to="/setup" replace /> : <AppShell />}>
        <Route index element={<HomePage />} />
        <Route path="schedule" element={<PlaceholderPage title="Schedule" />} />
        <Route path="clients" element={<PlaceholderPage title="Clients" />} />
        <Route path="catalog" element={<PlaceholderPage title="Catalog" />} />
        <Route path="settings" element={<PlaceholderPage title="Settings" />} />
      </Route>
    </Routes>
  )
}
