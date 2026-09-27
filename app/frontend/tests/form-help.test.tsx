import { render, screen } from '@testing-library/react'
import { expect, test, vi } from 'vitest'
import { FormFieldInput } from '@/components/form-fields'

test.each(['acknowledgement', 'heading', 'paragraph'] as const)(
  'a client sees the pinned %s help wording included in the archive',
  (type) => {
    render(
      <FormFieldInput
        field={{
          key: 'help',
          type,
          label: 'Published wording',
          help: 'Read <carefully> before agreeing.',
          required: false,
        }}
        value={false}
        onChange={vi.fn()}
      />,
    )
    expect(screen.getByText('Read <carefully> before agreeing.')).toBeVisible()
  },
)
