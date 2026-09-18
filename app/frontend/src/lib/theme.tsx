import { createContext, useContext, useEffect, useState, type ReactNode } from 'react'

export type Theme = 'light' | 'dark' | 'system'
type Resolved = 'light' | 'dark'

const STORAGE_KEY = 'theme'
const media = () => window.matchMedia('(prefers-color-scheme: dark)')

function resolve(theme: Theme): Resolved {
  return theme === 'system' ? (media().matches ? 'dark' : 'light') : theme
}

const ThemeContext = createContext<{
  theme: Theme
  resolvedTheme: Resolved
  setTheme: (t: Theme) => void
} | null>(null)

/** Applies `.dark` to <html>. index.html applies it before first paint from the same key. */
export function ThemeProvider({ children }: { children: ReactNode }) {
  const [theme, setThemeState] = useState<Theme>(
    () => (localStorage.getItem(STORAGE_KEY) as Theme | null) ?? 'system',
  )
  const [resolvedTheme, setResolved] = useState<Resolved>(() => resolve(theme))

  useEffect(() => {
    const apply = () => {
      const r = resolve(theme)
      setResolved(r)
      document.documentElement.classList.toggle('dark', r === 'dark')
      document.documentElement.style.colorScheme = r
    }
    apply()
    const mq = media()
    mq.addEventListener('change', apply)
    return () => mq.removeEventListener('change', apply)
  }, [theme])

  const setTheme = (t: Theme) => {
    localStorage.setItem(STORAGE_KEY, t)
    setThemeState(t)
  }

  return <ThemeContext value={{ theme, resolvedTheme, setTheme }}>{children}</ThemeContext>
}

export function useTheme() {
  const ctx = useContext(ThemeContext)
  if (!ctx) throw new Error('useTheme must be used inside <ThemeProvider>')
  return ctx
}
