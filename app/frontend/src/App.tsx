import { Route, Routes } from 'react-router'
import { AppShell } from '@/components/app-shell'
import { HomePage } from '@/routes/home'
import { PlaceholderPage } from '@/routes/placeholder'

export default function App() {
  return (
    <Routes>
      <Route element={<AppShell />}>
        <Route index element={<HomePage />} />
        <Route path="schedule" element={<PlaceholderPage title="Schedule" />} />
        <Route path="clients" element={<PlaceholderPage title="Clients" />} />
        <Route path="catalog" element={<PlaceholderPage title="Catalog" />} />
        <Route path="settings" element={<PlaceholderPage title="Settings" />} />
      </Route>
    </Routes>
  )
}
