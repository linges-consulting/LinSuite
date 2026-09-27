import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { TriangleAlert } from 'lucide-react'
import { toast } from 'sonner'
import { Field, Form } from '@/components/form'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { Skeleton } from '@/components/ui/skeleton'
import { Textarea } from '@/components/ui/textarea'
import {
  fetchNotificationSettings,
  fieldErrors,
  sendTestEmail,
  sendTestSms,
  updateNotificationSettings,
  updateNotificationTemplate,
  type NotificationSettings,
  type NotificationSettingsChange,
  type NotificationTemplate,
} from '@/lib/api'
import { NOTIFICATION_SETTINGS } from '@/lib/query-keys'

type EmailSender = 'resend' | 'smtp' | 'none'

type Draft = {
  email_sender: EmailSender
  resend_from_address: string
  resend_api_key: string
  smtp_host: string
  smtp_port: string
  smtp_username: string
  smtp_password: string
  smtp_from_address: string
  sms_enabled: boolean
  twilio_account_sid: string
  twilio_auth_token: string
  twilio_from_number: string
  /** Edited as one comma-separated field — "24, 2" — and split on save; this is the only
   *  field of the twelve here that isn't a 1:1 mirror of a server column. */
  reminder_intervals_hours: string
  /** Booking-portal policy (Phase 6 Task 4, #10) — a new section below, same panel. */
  online_booking_enabled: boolean
  online_cancellation_enabled: boolean
  cancellation_cutoff_hours: string
  booking_daily_cap_per_ip: string
  booking_daily_cap_per_email: string
}

function draftFrom(data: NotificationSettings): Draft {
  return {
    email_sender: data.email_sender ?? 'none',
    resend_from_address: data.resend_from_address ?? '',
    resend_api_key: '',
    smtp_host: data.smtp_host ?? '',
    smtp_port: data.smtp_port ? String(data.smtp_port) : '',
    smtp_username: data.smtp_username ?? '',
    smtp_password: '',
    smtp_from_address: data.smtp_from_address ?? '',
    sms_enabled: data.sms_enabled,
    twilio_account_sid: data.twilio_account_sid ?? '',
    twilio_auth_token: '',
    twilio_from_number: data.twilio_from_number ?? '',
    reminder_intervals_hours: data.reminder_intervals_hours.join(', '),
    online_booking_enabled: data.online_booking_enabled,
    online_cancellation_enabled: data.online_cancellation_enabled,
    cancellation_cutoff_hours: String(data.cancellation_cutoff_hours),
    booking_daily_cap_per_ip: String(data.booking_daily_cap_per_ip),
    booking_daily_cap_per_email: String(data.booking_daily_cap_per_email),
  }
}

/** The three secret fields are omitted unless the admin actually typed something into them —
 *  a blank "new value" box means "leave the stored one alone" (module-level rule, #11). */
function toChange(draft: Draft): NotificationSettingsChange {
  const change: NotificationSettingsChange = {
    email_sender: draft.email_sender,
    resend_from_address: draft.resend_from_address || null,
    smtp_host: draft.smtp_host || null,
    smtp_port: draft.smtp_port ? Number(draft.smtp_port) : null,
    smtp_username: draft.smtp_username || null,
    smtp_from_address: draft.smtp_from_address || null,
    sms_enabled: draft.sms_enabled,
    twilio_account_sid: draft.twilio_account_sid || null,
    twilio_from_number: draft.twilio_from_number || null,
    reminder_intervals_hours: draft.reminder_intervals_hours
      .split(',')
      .map((piece) => piece.trim())
      .filter(Boolean)
      .map(Number),
    online_booking_enabled: draft.online_booking_enabled,
    online_cancellation_enabled: draft.online_cancellation_enabled,
    cancellation_cutoff_hours: Number(draft.cancellation_cutoff_hours),
    booking_daily_cap_per_ip: Number(draft.booking_daily_cap_per_ip),
    booking_daily_cap_per_email: Number(draft.booking_daily_cap_per_email),
  }
  if (draft.resend_api_key) change.resend_api_key = draft.resend_api_key
  if (draft.smtp_password) change.smtp_password = draft.smtp_password
  if (draft.twilio_auth_token) change.twilio_auth_token = draft.twilio_auth_token
  return change
}

/**
 * Settings → Notifications: the sender, SMS, reminder intervals and the twelve message
 * templates (Phase 12 Task 6, #11).
 *
 * One explicit Save button for the sender/SMS/reminder fields — unlike the Security panel's
 * per-toggle autosave, there are too many fields here for a round trip per keystroke to feel
 * right. Templates save independently, one row at a time: each is its own database row with
 * its own id, so "save this template" and "save the sender" are different actions even though
 * they live on the same screen.
 *
 * **Booking Portal's policy toggles (Phase 6 Task 4) are their own `<section>` below** —
 * `BookingPolicySection`, appended after the reminder section, same panel, same form, same
 * Save button. Walk-in Queue (Phase 7) still reserves the layout for its own toggle after
 * that, in this same component, not a second settings surface (m3.md).
 */
export function NotificationsPanel() {
  const queryClient = useQueryClient()
  const settings = useQuery({
    queryKey: NOTIFICATION_SETTINGS,
    queryFn: fetchNotificationSettings,
  })
  const [draft, setDraft] = useState<Draft | null>(null)
  const [errors, setErrors] = useState<Record<string, string>>({})

  const save = useMutation({
    mutationFn: updateNotificationSettings,
    onSuccess: (saved) => {
      queryClient.setQueryData(NOTIFICATION_SETTINGS, saved)
      setDraft(null)
      setErrors({})
      toast.success('Notification settings saved')
    },
    onError: (error) => {
      setErrors(fieldErrors(error))
      toast.error(error.message)
    },
  })

  if (settings.isPending) return <Skeleton className="h-96 w-full" />
  if (settings.isError) {
    return (
      <p role="alert" className="text-sm text-destructive">
        {settings.error.message}
      </p>
    )
  }

  const form = draft ?? draftFrom(settings.data)
  const set = (patch: Partial<Draft>) => setDraft({ ...form, ...patch })

  return (
    <div className="flex max-w-3xl flex-col gap-10">
      <Form onSubmit={() => save.mutate(toChange(form))}>
        <EmailSection data={settings.data} form={form} set={set} errors={errors} />
        <SmsSection data={settings.data} form={form} set={set} errors={errors} />
        <ReminderSection form={form} set={set} error={errors.reminder_intervals_hours} />
        <BookingPolicySection form={form} set={set} errors={errors} />

        {/* Phase 7's walk-in-queue toggle appends its own <section> here, inside this same
            form. */}

        <div className="flex gap-2 border-t pt-6">
          <Button type="submit" disabled={save.isPending || draft === null}>
            {save.isPending ? 'Saving…' : 'Save changes'}
          </Button>
          {draft !== null && (
            <Button
              type="button"
              variant="ghost"
              onClick={() => {
                setDraft(null)
                setErrors({})
              }}
            >
              Discard
            </Button>
          )}
        </div>
      </Form>

      <TemplatesSection templates={settings.data.templates} />
    </div>
  )
}

type SectionProps = {
  data: NotificationSettings
  form: Draft
  set: (patch: Partial<Draft>) => void
  errors: Record<string, string>
}

function ReadinessBadge({ ready, notReadyLabel }: { ready: boolean; notReadyLabel: string }) {
  return ready ? (
    <Badge variant="success">Ready</Badge>
  ) : (
    <Badge variant="warning">{notReadyLabel}</Badge>
  )
}

function EmailSection({ data, form, set, errors }: SectionProps) {
  return (
    <section className="flex flex-col gap-4">
      <div className="flex items-center gap-2">
        <h2 className="text-base font-medium">Email</h2>
        <ReadinessBadge
          ready={data.email_ready}
          notReadyLabel={form.email_sender === 'none' ? 'Not configured' : 'Not verified yet'}
        />
      </div>
      {form.email_sender === 'none' && (
        <p
          role="status"
          className="flex items-start gap-1.5 rounded-md border border-warning/40 bg-warning/10 p-3 text-sm text-warning"
        >
          <TriangleAlert aria-hidden className="mt-0.5 size-4 shrink-0" />
          <span>
            No email sender is configured. Booking confirmations, reminders, form links and
            everything else this business would email is not sent — nothing is queued behind
            the scenes until you choose a sender below and send a successful test.
          </span>
        </p>
      )}

      <Field label="Sender" htmlFor="email-sender">
        <Select
          value={form.email_sender}
          onValueChange={(value) => set({ email_sender: value as EmailSender })}
        >
          <SelectTrigger id="email-sender" className="w-full max-w-xs">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="none">None</SelectItem>
            <SelectItem value="resend">Resend</SelectItem>
            <SelectItem value="smtp">SMTP</SelectItem>
          </SelectContent>
        </Select>
      </Field>

      {form.email_sender === 'resend' && (
        <>
          <Field
            label="From address"
            htmlFor="resend-from"
            error={errors.resend_from_address}
          >
            <Input
              id="resend-from"
              type="email"
              value={form.resend_from_address}
              onChange={(e) => set({ resend_from_address: e.target.value })}
            />
          </Field>
          <Field
            label="API key"
            htmlFor="resend-key"
            hint={
              data.resend_api_key_set
                ? 'A key is already stored. Leave this blank to keep it, or enter a new one to replace it.'
                : 'Not set yet.'
            }
          >
            <Input
              id="resend-key"
              type="password"
              autoComplete="new-password"
              placeholder={data.resend_api_key_set ? '••••••••••••' : 're_...'}
              value={form.resend_api_key}
              onChange={(e) => set({ resend_api_key: e.target.value })}
            />
          </Field>
        </>
      )}

      {form.email_sender === 'smtp' && (
        <>
          <div className="grid grid-cols-2 gap-4">
            <Field label="Host" htmlFor="smtp-host" error={errors.smtp_host}>
              <Input
                id="smtp-host"
                value={form.smtp_host}
                onChange={(e) => set({ smtp_host: e.target.value })}
              />
            </Field>
            <Field label="Port" htmlFor="smtp-port" error={errors.smtp_port}>
              <Input
                id="smtp-port"
                type="number"
                min={1}
                max={65535}
                value={form.smtp_port}
                onChange={(e) => set({ smtp_port: e.target.value })}
              />
            </Field>
          </div>
          <Field label="Username" htmlFor="smtp-username">
            <Input
              id="smtp-username"
              value={form.smtp_username}
              onChange={(e) => set({ smtp_username: e.target.value })}
            />
          </Field>
          <Field
            label="Password"
            htmlFor="smtp-password"
            hint={
              data.smtp_password_set
                ? 'A password is already stored. Leave this blank to keep it.'
                : 'Not set yet.'
            }
          >
            <Input
              id="smtp-password"
              type="password"
              autoComplete="new-password"
              placeholder={data.smtp_password_set ? '••••••••••••' : ''}
              value={form.smtp_password}
              onChange={(e) => set({ smtp_password: e.target.value })}
            />
          </Field>
          <Field label="From address" htmlFor="smtp-from" error={errors.smtp_from_address}>
            <Input
              id="smtp-from"
              type="email"
              value={form.smtp_from_address}
              onChange={(e) => set({ smtp_from_address: e.target.value })}
            />
          </Field>
        </>
      )}

      {form.email_sender !== 'none' && <TestEmailAction />}
    </section>
  )
}

function TestEmailAction() {
  const queryClient = useQueryClient()
  const [to, setTo] = useState('')
  const test = useMutation({
    mutationFn: sendTestEmail,
    onSuccess: (saved) => {
      queryClient.setQueryData(NOTIFICATION_SETTINGS, saved)
      toast.success(`Test email sent to ${to}`)
    },
    onError: (error) => toast.error(error.message),
  })

  return (
    <div className="flex flex-wrap items-end gap-2 border-t pt-4">
      <Field label="Send a test email to" htmlFor="test-email-to">
        <Input
          id="test-email-to"
          type="email"
          className="w-64"
          value={to}
          onChange={(e) => setTo(e.target.value)}
        />
      </Field>
      <Button
        type="button"
        variant="outline"
        disabled={!to || test.isPending}
        onClick={() => test.mutate(to)}
      >
        {test.isPending ? 'Sending…' : 'Send test email'}
      </Button>
    </div>
  )
}

function SmsSection({ data, form, set, errors }: SectionProps) {
  return (
    <section className="flex flex-col gap-4 border-t pt-6">
      <div className="flex items-center gap-2">
        <h2 className="text-base font-medium">SMS</h2>
        {form.sms_enabled && <ReadinessBadge ready={data.sms_ready} notReadyLabel="Incomplete" />}
      </div>

      <div className="flex gap-3">
        <Checkbox
          id="sms-enabled"
          className="mt-0.5"
          checked={form.sms_enabled}
          onCheckedChange={(checked) => set({ sms_enabled: checked === true })}
        />
        <div className="flex flex-col gap-1">
          <Label htmlFor="sms-enabled">Send text messages</Label>
          <p className="text-xs text-muted-foreground">
            Off by default — this is an adapter nobody gets until they ask and supply their own
            Twilio credentials. Turning this off leaves nothing attempting to send a text,
            whatever else below is filled in.
          </p>
        </div>
      </div>

      {form.sms_enabled && (
        <>
          <Field label="Twilio Account SID" htmlFor="twilio-sid" error={errors.twilio_account_sid}>
            <Input
              id="twilio-sid"
              value={form.twilio_account_sid}
              onChange={(e) => set({ twilio_account_sid: e.target.value })}
            />
          </Field>
          <Field
            label="Twilio Auth Token"
            htmlFor="twilio-token"
            hint={
              data.twilio_auth_token_set
                ? 'A token is already stored. Leave this blank to keep it.'
                : 'Not set yet.'
            }
          >
            <Input
              id="twilio-token"
              type="password"
              autoComplete="new-password"
              placeholder={data.twilio_auth_token_set ? '••••••••••••' : ''}
              value={form.twilio_auth_token}
              onChange={(e) => set({ twilio_auth_token: e.target.value })}
            />
          </Field>
          <Field label="From number" htmlFor="twilio-from" error={errors.twilio_from_number}>
            <Input
              id="twilio-from"
              placeholder="+15551234567"
              value={form.twilio_from_number}
              onChange={(e) => set({ twilio_from_number: e.target.value })}
            />
          </Field>

          <TestSmsAction />
        </>
      )}
    </section>
  )
}

function TestSmsAction() {
  const [to, setTo] = useState('')
  const test = useMutation({
    mutationFn: sendTestSms,
    onSuccess: () => toast.success(`Test text sent to ${to}`),
    onError: (error) => toast.error(error.message),
  })

  return (
    <div className="flex flex-wrap items-end gap-2 border-t pt-4">
      <Field label="Send a test text to" htmlFor="test-sms-to">
        <Input
          id="test-sms-to"
          className="w-64"
          placeholder="+15551234567"
          value={to}
          onChange={(e) => setTo(e.target.value)}
        />
      </Field>
      <Button
        type="button"
        variant="outline"
        disabled={!to || test.isPending}
        onClick={() => test.mutate(to)}
      >
        {test.isPending ? 'Sending…' : 'Send test text'}
      </Button>
    </div>
  )
}

function ReminderSection(props: {
  form: Draft
  set: (patch: Partial<Draft>) => void
  error?: string
}) {
  return (
    <section className="flex flex-col gap-4 border-t pt-6">
      <h2 className="text-base font-medium">Appointment reminders</h2>
      <Field
        label="Send reminders this many hours before an appointment"
        htmlFor="reminder-intervals"
        error={props.error}
        hint='Comma-separated hours, reckoned on the business clock — "24, 2" sends one reminder a day before and one two hours before. At least one is required.'
      >
        <Input
          id="reminder-intervals"
          className="max-w-xs"
          value={props.form.reminder_intervals_hours}
          onChange={(e) => props.set({ reminder_intervals_hours: e.target.value })}
        />
      </Field>
    </section>
  )
}

/**
 * Booking-portal policy (Phase 6 Task 4, #10) — the section Task 6 reserved layout for.
 * Every one of these toggles is enforced at the API in `scheduling/public.py`; this screen
 * only writes the value, so hiding a disabled action here is cosmetic, never the enforcement.
 */
function BookingPolicySection(props: {
  form: Draft
  set: (patch: Partial<Draft>) => void
  errors: Record<string, string>
}) {
  const { form, set, errors } = props
  return (
    <section className="flex flex-col gap-4 border-t pt-6">
      <h2 className="text-base font-medium">Online booking</h2>

      <div className="flex gap-3">
        <Checkbox
          id="online-booking-enabled"
          className="mt-0.5"
          checked={form.online_booking_enabled}
          onCheckedChange={(checked) => set({ online_booking_enabled: checked === true })}
        />
        <div className="flex flex-col gap-1">
          <Label htmlFor="online-booking-enabled">Allow clients to book online</Label>
          <p className="text-xs text-muted-foreground">
            Off refuses every request to the public booking page, even for a service marked
            bookable online — the same as if the page didn't exist.
          </p>
        </div>
      </div>

      <div className="grid grid-cols-2 gap-4">
        <Field
          label="Per-IP daily booking limit"
          htmlFor="cap-per-ip"
          error={errors.booking_daily_cap_per_ip}
        >
          <Input
            id="cap-per-ip"
            type="number"
            min={1}
            max={1000}
            value={form.booking_daily_cap_per_ip}
            onChange={(e) => set({ booking_daily_cap_per_ip: e.target.value })}
          />
        </Field>
        <Field
          label="Per-email daily booking limit"
          htmlFor="cap-per-email"
          error={errors.booking_daily_cap_per_email}
        >
          <Input
            id="cap-per-email"
            type="number"
            min={1}
            max={1000}
            value={form.booking_daily_cap_per_email}
            onChange={(e) => set({ booking_daily_cap_per_email: e.target.value })}
          />
        </Field>
      </div>

      <div className="flex gap-3 border-t pt-4">
        <Checkbox
          id="online-cancellation-enabled"
          className="mt-0.5"
          checked={form.online_cancellation_enabled}
          onCheckedChange={(checked) => set({ online_cancellation_enabled: checked === true })}
        />
        <div className="flex flex-col gap-1">
          <Label htmlFor="online-cancellation-enabled">
            Allow clients to cancel or reschedule online
          </Label>
          <p className="text-xs text-muted-foreground">
            Off refuses a cancel or reschedule request through the management link, whatever
            the cutoff below says — clients still need to contact you directly.
          </p>
        </div>
      </div>

      <Field
        label="Cancellation cutoff (hours before the appointment)"
        htmlFor="cancellation-cutoff"
        error={errors.cancellation_cutoff_hours}
        hint="Inside this window, an online cancel or reschedule is refused."
      >
        <Input
          id="cancellation-cutoff"
          type="number"
          className="max-w-xs"
          min={0}
          max={8760}
          value={form.cancellation_cutoff_hours}
          onChange={(e) => set({ cancellation_cutoff_hours: e.target.value })}
        />
      </Field>
    </section>
  )
}

// --- templates: their own sub-resource, saved one row at a time -----------------------------

const TYPE_LABELS: Record<string, string> = {
  booking_confirmation: 'Booking confirmed',
  reminder: 'Appointment reminder',
  modification: 'Booking changed',
  cancellation: 'Booking cancelled',
  form_link: 'Form link issued',
  package_notice: 'Package balance notice',
}

function TemplatesSection({ templates }: { templates: NotificationTemplate[] }) {
  return (
    <section className="flex flex-col gap-6 border-t pt-6">
      <div className="flex flex-col gap-1">
        <h2 className="text-base font-medium">Message templates</h2>
        <p className="text-xs text-muted-foreground">
          One template per message type and channel. Each saves on its own.
        </p>
      </div>
      {templates.map((template) => (
        <TemplateRow key={template.id} template={template} />
      ))}
    </section>
  )
}

function TemplateRow({ template }: { template: NotificationTemplate }) {
  const queryClient = useQueryClient()
  const [subject, setSubject] = useState(template.subject_template ?? '')
  const [body, setBody] = useState(template.body_template)
  const [errors, setErrors] = useState<Record<string, string>>({})
  const changed = subject !== (template.subject_template ?? '') || body !== template.body_template

  const save = useMutation({
    mutationFn: () =>
      updateNotificationTemplate(template.id, {
        subject_template: template.channel === 'email' ? subject : null,
        body_template: body,
      }),
    onSuccess: (saved) => {
      queryClient.setQueryData<NotificationSettings | undefined>(
        NOTIFICATION_SETTINGS,
        (current) =>
          current && {
            ...current,
            templates: current.templates.map((t) => (t.id === saved.id ? saved : t)),
          },
      )
      setErrors({})
      toast.success('Template saved')
    },
    onError: (error) => {
      setErrors(fieldErrors(error))
      toast.error(error.message)
    },
  })

  return (
    <div className="flex flex-col gap-3 rounded-md border p-4">
      <div className="flex items-center gap-2">
        <h3 className="text-sm font-medium">{TYPE_LABELS[template.notification_type]}</h3>
        <Badge variant="outline">{template.channel === 'email' ? 'Email' : 'Text'}</Badge>
      </div>
      <p className="text-xs text-muted-foreground">
        Available: {template.merge_fields.join(', ')}
      </p>
      {template.channel === 'email' && (
        <Field
          label="Subject"
          htmlFor={`template-subject-${template.id}`}
          error={errors.subject_template}
        >
          <Input
            id={`template-subject-${template.id}`}
            value={subject}
            onChange={(e) => setSubject(e.target.value)}
          />
        </Field>
      )}
      <Field
        label="Message"
        htmlFor={`template-body-${template.id}`}
        error={errors.body_template}
      >
        <Textarea
          id={`template-body-${template.id}`}
          rows={4}
          value={body}
          onChange={(e) => setBody(e.target.value)}
        />
      </Field>
      <div>
        <Button
          type="button"
          size="sm"
          disabled={!changed || save.isPending}
          onClick={() => save.mutate()}
        >
          {save.isPending ? 'Saving…' : 'Save template'}
        </Button>
      </div>
    </div>
  )
}
