import { LogOut, Menu, ShieldCheck, UserRound, X } from 'lucide-react'
import { useState } from 'react'
import { Link, NavLink, Outlet, useLocation } from 'react-router'
import { BrandMark } from '@/components/brand-mark'
import { ModeSwitcher } from '@/components/mode-switcher'
import { ThemeToggle } from '@/components/theme-toggle'
import { Button } from '@/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { useLogout, useSession } from '@/lib/auth'
import { NAV, navFor } from '@/lib/nav'
import { cn } from '@/lib/utils'

export function AppShell() {
  const [open, setOpen] = useState(false)
  const { pathname } = useLocation()
  const { user } = useSession()
  const nav = navFor(user)
  // The title comes from the full list, not the filtered one: someone who reaches a screen
  // they hold no capability for still deserves a heading over the refusal.
  const title =
    NAV.find((n) => pathname.startsWith(n.to))?.label ??
    (pathname.startsWith('/security') ? 'Security' : 'Home')

  return (
    <div className="flex min-h-dvh">
      {/* Mobile scrim */}
      {open && (
        <button
          aria-label="Close menu"
          className="fixed inset-0 z-30 bg-black/50 md:hidden"
          onClick={() => setOpen(false)}
        />
      )}

      <aside
        className={cn(
          'fixed inset-y-0 left-0 z-40 flex w-60 flex-col border-r bg-sidebar text-sidebar-foreground transition-transform duration-200 ease-out md:static md:translate-x-0',
          open ? 'translate-x-0' : '-translate-x-full',
        )}
      >
        <div className="flex h-14 items-center gap-2.5 border-b px-4">
          <NavLink to="/" className="min-w-0" onClick={() => setOpen(false)}>
            <BrandMark className="flex items-center gap-2.5 font-semibold" />
          </NavLink>
          <Button
            variant="ghost"
            size="icon-sm"
            className="ml-auto md:hidden"
            aria-label="Close menu"
            onClick={() => setOpen(false)}
          >
            <X />
          </Button>
        </div>
        <nav aria-label="Primary" className="flex flex-col gap-0.5 p-2">
          {nav.map(({ to, label, icon: Icon }) => (
            <NavLink
              key={to}
              to={to}
              onClick={() => setOpen(false)}
              className={({ isActive }) =>
                cn(
                  'flex h-9 items-center gap-2.5 rounded-md px-2.5 font-medium text-muted-foreground transition-colors duration-150 hover:bg-sidebar-accent hover:text-foreground',
                  isActive && 'bg-sidebar-accent text-foreground',
                )
              }
            >
              <Icon className="size-4" aria-hidden />
              {label}
            </NavLink>
          ))}
        </nav>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex h-14 items-center gap-2 border-b bg-background px-4 md:px-6">
          <Button
            variant="ghost"
            size="icon-sm"
            className="md:hidden"
            aria-label="Open menu"
            onClick={() => setOpen(true)}
          >
            <Menu />
          </Button>
          <h1 className="text-base font-semibold tracking-tight">{title}</h1>
          <div className="ml-auto flex items-center gap-1">
            <ModeSwitcher />
            <ThemeToggle />
            <UserMenu />
          </div>
        </header>
        <main className="flex-1 p-4 md:p-6">
          <Outlet />
        </main>
      </div>
    </div>
  )
}

function UserMenu() {
  const { user } = useSession()
  const signOut = useLogout()
  if (!user) return null

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="ghost" size="icon-sm" aria-label="Account">
          <UserRound />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-56">
        <DropdownMenuLabel className="truncate font-normal text-muted-foreground">
          {user.email}
        </DropdownMenuLabel>
        <DropdownMenuSeparator />
        {/* Here rather than in the sidebar: it is a page about the person signed in, not a
            section of the business, and every account has one — including the staff accounts
            that are offered no Settings link at all. */}
        <DropdownMenuItem asChild>
          <Link to="/security">
            <ShieldCheck aria-hidden />
            Security
          </Link>
        </DropdownMenuItem>
        <DropdownMenuItem onSelect={() => signOut.mutate()} disabled={signOut.isPending}>
          <LogOut aria-hidden />
          Log out
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
