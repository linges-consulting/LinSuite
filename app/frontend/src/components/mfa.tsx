import { Check, Copy, Download } from 'lucide-react'
import { useState } from 'react'
import { Field } from '@/components/form'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'

/**
 * The two pieces both multi-factor screens need: the field a code is typed into, and the
 * one-time display of a set of recovery codes.
 *
 * Here rather than duplicated because the verify screen, the enrolment flow and the Admin
 * Mode dialog all collect a code, and a second spelling of `inputMode="numeric"` is a second
 * chance to leave it off — on a phone that is the difference between a number pad and a
 * keyboard.
 */
export function CodeField(props: {
  id: string
  label: string
  value: string
  onChange: (value: string) => void
  error?: string
  hint?: string
  autoFocus?: boolean
}) {
  return (
    <Field label={props.label} htmlFor={props.id} error={props.error} hint={props.hint}>
      <Input
        id={props.id}
        // `text`, not `number`: a code is a string of digits with meaningful leading zeros,
        // and a number input drops them, offers a spinner and accepts `1e5`.
        type="text"
        inputMode="numeric"
        autoComplete="one-time-code"
        autoFocus={props.autoFocus}
        required
        // Long enough for a recovery code; the server is what actually decides.
        maxLength={24}
        className="font-mono tracking-widest"
        value={props.value}
        onChange={(e) => props.onChange(e.target.value)}
        aria-invalid={props.error ? true : undefined}
      />
    </Field>
  )
}

/**
 * Recovery codes, shown the one time they exist in plaintext.
 *
 * Copy *and* download, because the two failure modes are different: a clipboard is lost at
 * the next copy, and a file is what someone actually keeps. The warning is above the codes
 * rather than below them — after the codes, it is read once the page has already been closed.
 */
export function RecoveryCodes({ codes }: { codes: string[] }) {
  const [copied, setCopied] = useState(false)
  const text = codes.join('\n')

  const copy = async () => {
    await navigator.clipboard.writeText(text)
    setCopied(true)
    setTimeout(() => setCopied(false), 2000)
  }

  const download = () => {
    const url = URL.createObjectURL(new Blob([text + '\n'], { type: 'text/plain' }))
    const link = document.createElement('a')
    link.href = url
    link.download = 'linsuite-recovery-codes.txt'
    link.click()
    URL.revokeObjectURL(url)
  }

  return (
    <div className="flex flex-col gap-3">
      <p className="text-sm text-muted-foreground">
        Keep these somewhere safe and away from the device with your authenticator on it. Each
        one works once, and this is the only time they are shown.
      </p>
      <ul
        aria-label="Recovery codes"
        className="grid grid-cols-2 gap-x-6 gap-y-1 rounded-lg bg-muted p-4 font-mono text-sm"
      >
        {codes.map((code) => (
          <li key={code} data-numeric>
            {code}
          </li>
        ))}
      </ul>
      <div className="flex gap-2">
        <Button type="button" variant="outline" size="sm" onClick={copy}>
          {copied ? <Check aria-hidden /> : <Copy aria-hidden />}
          {copied ? 'Copied' : 'Copy'}
        </Button>
        <Button type="button" variant="outline" size="sm" onClick={download}>
          <Download aria-hidden />
          Download
        </Button>
      </div>
    </div>
  )
}

/**
 * The assurance tradeoff, in as many words (tech-stack §14). It appears wherever an emailed
 * code is offered or turned on, rather than only in the settings screen, because the person
 * choosing it at sign-in is not the person who read the setting.
 */
export const EMAIL_OTP_WARNING =
  'Emailed codes are weaker than an authenticator app: if somebody has your password, ' +
  'they often have the inbox it would be sent to.'
