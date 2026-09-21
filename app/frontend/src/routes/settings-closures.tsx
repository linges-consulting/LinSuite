import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { CalendarX2, ChevronLeft, ChevronRight, Download, Plus, Trash2 } from 'lucide-react'
import { useState } from 'react'
import { toast } from 'sonner'
import { EmptyState } from '@/components/empty-state'
import { Field, Form, FormError } from '@/components/form'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Skeleton } from '@/components/ui/skeleton'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import {
  addClosure,
  deleteClosure,
  fetchClosures,
  importStatutoryClosures,
  type Closure,
} from '@/lib/api'
import { invalidateScheduling } from '@/lib/query-client'
import { CLOSURES } from '@/lib/query-keys'

/**
 * Settings → Closures: the days the business is shut (PRD §1).
 *
 * **One list, by year.** Statutory holidays are imported a year at a time from the business's
 * province — the point of the feature is that nobody types eleven dates — and a day added by
 * hand sits in the same list, because a staff retreat blocks bookings exactly as Canada Day
 * does. `source` only changes the badge and what an import is allowed to skip.
 *
 * **Deleting one is how a business says it works that day.** There is no overrides table and
 * nothing is computed at read time: the row exists or it does not. The consequence, which
 * the copy has to be honest about, is that an import adds back a statutory day somebody
 * deleted — it only skips dates already on the list. A tombstone would be a second mechanism
 * for one answer, so the button warns instead.
 */
export function ClosuresPanel() {
  const queryClient = useQueryClient()
  const [year, setYear] = useState(() => new Date().getFullYear())
  const [adding, setAdding] = useState(false)

  const closures = useQuery({
    queryKey: [...CLOSURES, year],
    queryFn: () => fetchClosures(year),
    placeholderData: (previous) => previous,
  })
  const refresh = () => {
    queryClient.invalidateQueries({ queryKey: CLOSURES })
    invalidateScheduling(queryClient)
  }

  const importStatutory = useMutation({
    mutationFn: () => importStatutoryClosures(year),
    onSuccess: (result) => {
      toast.success(
        result.added === 0
          ? `Nothing new for ${year}`
          : `Added ${result.added} statutory ${result.added === 1 ? 'holiday' : 'holidays'}`,
        result.skipped
          ? { description: `${result.skipped} were already on the list.` }
          : undefined,
      )
      refresh()
    },
    onError: (error: Error) => toast.error(error.message),
  })

  const remove = useMutation({
    mutationFn: (closure: Closure) => deleteClosure(closure.id),
    onSuccess: (_, closure) => {
      toast.success(`${closure.name} is no longer a closure`, {
        description: 'Importing statutory holidays again would add it back.',
      })
      refresh()
    },
    onError: (error: Error) => toast.error(error.message),
  })

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-4">
        <p className="max-w-3xl text-sm text-muted-foreground">
          Days nobody can be booked. Import the statutory holidays for your province, and add
          anything else — a staff retreat, a long weekend you are taking.
        </p>
        <div className="flex items-center gap-2">
          <Button
            variant="ghost"
            size="sm"
            aria-label="Previous year"
            onClick={() => setYear(year - 1)}
          >
            <ChevronLeft aria-hidden />
          </Button>
          <span className="w-12 text-center text-sm font-medium tabular-nums" data-numeric>
            {year}
          </span>
          <Button variant="ghost" size="sm" aria-label="Next year" onClick={() => setYear(year + 1)}>
            <ChevronRight aria-hidden />
          </Button>
          <Button
            variant="secondary"
            disabled={importStatutory.isPending}
            onClick={() => importStatutory.mutate()}
          >
            <Download aria-hidden />
            {importStatutory.isPending
              ? 'Importing…'
              : `Import statutory holidays for ${year}`}
          </Button>
          <Button onClick={() => setAdding(true)}>
            <Plus aria-hidden />
            Add closure
          </Button>
        </div>
      </div>

      {closures.isPending && <Skeleton className="h-64 w-full" />}
      {closures.isError && (
        <p role="alert" className="text-sm text-destructive">
          {closures.error.message}
        </p>
      )}

      {closures.data?.length === 0 && (
        <EmptyState
          icon={CalendarX2}
          title={`Nothing closed in ${year}`}
          // No button here: the import and the add both sit in the toolbar a few pixels
          // above, and two controls with the same accessible name on one screen is a worse
          // answer than one.
          description="Import the statutory holidays for your province, or add a day of your own."
        />
      )}

      {closures.data && closures.data.length > 0 && (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Date</TableHead>
              <TableHead>Name</TableHead>
              <TableHead>Source</TableHead>
              <TableHead className="text-right">Actions</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {closures.data.map((closure) => (
              <TableRow key={closure.id}>
                <TableCell className="tabular-nums" data-numeric>
                  {closure.date}
                </TableCell>
                <TableCell className="font-medium">{closure.name}</TableCell>
                <TableCell>
                  {/* Never colour alone (DESIGN.md) — the badge carries the word. */}
                  <Badge variant={closure.source === 'statutory' ? 'info' : 'secondary'}>
                    {closure.source === 'statutory' ? 'Statutory' : 'Manual'}
                  </Badge>
                </TableCell>
                <TableCell className="text-right">
                  <Button
                    variant="ghost"
                    size="sm"
                    aria-label={`Remove ${closure.name}`}
                    disabled={remove.isPending}
                    onClick={() => {
                      if (
                        confirm(
                          `Remove ${closure.name} on ${closure.date}?\n\n` +
                            'The business can be booked that day again. Importing statutory ' +
                            'holidays for this year would add it back.',
                        )
                      )
                        remove.mutate(closure)
                    }}
                  >
                    <Trash2 aria-hidden />
                  </Button>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}

      {adding && <ClosureDialog year={year} onClose={() => setAdding(false)} />}
    </div>
  )
}

function ClosureDialog({ year, onClose }: { year: number; onClose: () => void }) {
  const queryClient = useQueryClient()
  const [date, setDate] = useState('')
  const [name, setName] = useState('')

  const save = useMutation({
    mutationFn: () => addClosure({ date, name: name.trim() }),
    onSuccess: (closure) => {
      toast.success(`Closed ${closure.date}`)
      queryClient.invalidateQueries({ queryKey: CLOSURES })
      invalidateScheduling(queryClient)
      onClose()
    },
  })

  const incomplete = !date || !name.trim()

  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Add closure</DialogTitle>
          <DialogDescription>
            A day the business is shut. Nobody can be booked on it.
          </DialogDescription>
        </DialogHeader>
        <Form onSubmit={() => !incomplete && save.mutate()}>
          <Field label="Date" htmlFor="closure-date">
            <Input
              id="closure-date"
              type="date"
              required
              min={`${year}-01-01`}
              max={`${year}-12-31`}
              value={date}
              onChange={(e) => setDate(e.target.value)}
            />
          </Field>
          <Field label="Name" htmlFor="closure-name" hint="What it is called on the calendar.">
            <Input
              id="closure-name"
              required
              maxLength={200}
              value={name}
              onChange={(e) => setName(e.target.value)}
            />
          </Field>

          {save.error && <FormError>{save.error.message}</FormError>}

          <DialogFooter>
            <Button type="button" variant="ghost" onClick={onClose}>
              Cancel
            </Button>
            <Button type="submit" disabled={save.isPending || incomplete}>
              {save.isPending ? 'Adding…' : 'Add closure'}
            </Button>
          </DialogFooter>
        </Form>
      </DialogContent>
    </Dialog>
  )
}
