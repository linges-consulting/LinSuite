import { QueryClientProvider } from '@tanstack/react-query'
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router'
import './index.css'
import App from './App.tsx'
import { Toaster } from '@/components/ui/sonner'
import { applyCachedBranding } from '@/lib/branding'
import { createQueryClient } from '@/lib/query-client'
import { ThemeProvider } from '@/lib/theme'

// Before the first render, so a return visit paints this business's colours rather than the
// defaults and then a flash. `<Branding />` replaces them with the server's answer.
applyCachedBranding()

const queryClient = createQueryClient()

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <ThemeProvider>
      <QueryClientProvider client={queryClient}>
        <BrowserRouter>
          <App />
        </BrowserRouter>
        <Toaster position="bottom-right" />
      </QueryClientProvider>
    </ThemeProvider>
  </StrictMode>,
)
