import { screen } from '@testing-library/react'
import type userEvent from '@testing-library/user-event'

export async function choose(
  user: ReturnType<typeof userEvent.setup>,
  label: string,
  option: string | RegExp,
) {
  await user.click(await screen.findByLabelText(label))
  await user.click(await screen.findByRole('option', { name: option }))
}
