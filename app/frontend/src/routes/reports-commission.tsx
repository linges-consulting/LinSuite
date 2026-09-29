import { useQuery } from '@tanstack/react-query'
import { BarChart3 } from 'lucide-react'
import { useState } from 'react'
import { EmptyState } from '@/components/empty-state'
import { ExportControl } from '@/components/export-control'
import { Badge } from '@/components/ui/badge'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Skeleton } from '@/components/ui/skeleton'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import {
  downloadCommissionExport,
  fetchCommissionExportStatus,
  fetchCommissionReport,
  fetchRoster,
  requestCommissionExport,
  type CommissionRow,
} from '@/lib/api'
import { centsToDollars } from '@/lib/money'
import { COMMISSION_REPORT, ROSTER } from '@/lib/query-keys'

const money = (cents: number) => `$${centsToDollars(cents)}`

const STATUS_VARIANT: Record<CommissionRow['payment_status'], 'success' | 'info' | 'warning' | 'outline'> = {
  received: 'success',
  partial: 'info',
  pending: 'warning',
  voided: 'outline',
}

/** `5 Jan 2026`: which day a posting or invoice happened, no clock needed for a report row. */
function shortDate(instant: string): string {
  return new Intl.DateTimeFormat(undefined, { dateStyle: 'medium' }).format(new Date(instant))
}

type StaffTotal = {
  staff_id: string
  staff_name: string
  earned_cents: number
  received_cents: number
  pending_cents: number
}

/** Groups the report's own lines by staff — the server totals only by source (service/retail),
 *  so "each person's totals" (spec #95 story 67) is computed here from the rows it already
 *  sends, never a second request. */
function byStaff(rows: CommissionRow[]): StaffTotal[] {
  const totals = new Map<string, StaffTotal>()
  for (const row of rows) {
    const existing = totals.get(row.staff_id) ?? {
      staff_id: row.staff_id,
      staff_name: row.staff_name,
      earned_cents: 0,
      received_cents: 0,
      pending_cents: 0,
    }
    existing.earned_cents += row.amount_cents
    existing.received_cents += row.commission_received_cents
    existing.pending_cents += row.commission_pending_cents
    totals.set(row.staff_id, existing)
  }
  return [...totals.values()].sort((a, b) => b.earned_cents - a.earned_cents)
}

/**
 * Commission (spec #95 user story 67, #110): a date range and staff filter, each person's
 * totals and the lines behind them, CSV export via the shared `ExportControl`. `commission.view`,
 * Admin Mode — `routes/reports.tsx` is what decides whether this tab is even offered.
 */
export function CommissionReportTab() {
  const [range, setRange] = useState<{ from?: string; to?: string }>({})
  const [staffId, setStaffId] = useState<string>('all')
  const staffFilter = staffId === 'all' ? undefined : staffId

  const roster = useQuery({ queryKey: ROSTER, queryFn: fetchRoster })
  const report = useQuery({
    queryKey: [...COMMISSION_REPORT, range.from, range.to, staffFilter],
    queryFn: () => fetchCommissionReport({ from: range.from, to: range.to, staff_id: staffFilter }),
  })

  const from = range.from ?? report.data?.from ?? ''
  const to = range.to ?? report.data?.to ?? ''
  const staffTotals = report.data ? byStaff(report.data.rows) : []

  return (
    <Card>
      <CardHeader className="flex-row flex-wrap items-end justify-between gap-3">
        <CardTitle className="text-base font-medium">
          <h2>Commission</h2>
        </CardTitle>
        <div className="flex flex-wrap items-end gap-2">
          <div className="grid gap-1">
            <Label htmlFor="commission-from" className="text-xs text-muted-foreground">
              From
            </Label>
            <Input
              id="commission-from"
              type="date"
              className="w-40"
              value={from}
              max={to || undefined}
              onChange={(e) => setRange({ from: e.target.value, to })}
            />
          </div>
          <div className="grid gap-1">
            <Label htmlFor="commission-to" className="text-xs text-muted-foreground">
              To
            </Label>
            <Input
              id="commission-to"
              type="date"
              className="w-40"
              value={to}
              min={from || undefined}
              onChange={(e) => setRange({ from, to: e.target.value })}
            />
          </div>
          <div className="grid gap-1">
            <Label htmlFor="commission-staff" className="text-xs text-muted-foreground">
              Staff
            </Label>
            <Select value={staffId} onValueChange={setStaffId}>
              <SelectTrigger id="commission-staff" className="w-44">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all">All staff</SelectItem>
                {(roster.data ?? []).map((member) => (
                  <SelectItem key={member.id} value={member.id}>
                    {member.display_name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <ExportControl
            requestExport={() => requestCommissionExport({ from: range.from, to: range.to, staff_id: staffFilter })}
            pollExport={fetchCommissionExportStatus}
            downloadExport={downloadCommissionExport}
          />
        </div>
      </CardHeader>
      <CardContent className="flex flex-col gap-6">
        {report.isPending ? (
          <Skeleton className="h-40 w-full" />
        ) : report.isError ? (
          <p role="alert" className="text-destructive">
            Could not load the commission report.
          </p>
        ) : report.data.rows.length === 0 ? (
          <EmptyState
            icon={BarChart3}
            title="No commission in this window"
            description="Nothing was earned in this date range and staff filter."
          />
        ) : (
          <>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Staff</TableHead>
                  <TableHead className="text-right">Earned</TableHead>
                  <TableHead className="text-right">Received</TableHead>
                  <TableHead className="text-right">Pending</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {staffTotals.map((s) => (
                  <TableRow key={s.staff_id}>
                    <TableCell className="font-medium">{s.staff_name}</TableCell>
                    <TableCell className="text-right tabular-nums">{money(s.earned_cents)}</TableCell>
                    <TableCell className="text-right tabular-nums">{money(s.received_cents)}</TableCell>
                    <TableCell className="text-right tabular-nums">{money(s.pending_cents)}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>

            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Date</TableHead>
                  <TableHead>Staff</TableHead>
                  <TableHead>Source</TableHead>
                  <TableHead>Invoice #</TableHead>
                  <TableHead>Kind</TableHead>
                  <TableHead className="text-right">Amount</TableHead>
                  <TableHead>Status</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {report.data.rows.map((row) => (
                  <TableRow key={row.id}>
                    <TableCell>{shortDate(row.posted_at)}</TableCell>
                    <TableCell>{row.staff_name}</TableCell>
                    <TableCell className="capitalize">{row.source}</TableCell>
                    <TableCell>#{row.invoice_number}</TableCell>
                    <TableCell className="capitalize">{row.kind}</TableCell>
                    <TableCell className="text-right tabular-nums">{money(row.amount_cents)}</TableCell>
                    <TableCell>
                      <Badge variant={STATUS_VARIANT[row.payment_status]}>{row.payment_status}</Badge>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </>
        )}
      </CardContent>
    </Card>
  )
}
