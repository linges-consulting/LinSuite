/**
 * The digits the server stores, as a person writes a North American number. Anything else
 * is printed as it is: a rule for every country's grouping is not this screen's to have.
 */
export function formatPhone(digits: string): string {
  const local = digits.length === 11 && digits.startsWith('1') ? digits.slice(1) : digits
  if (local.length !== 10) return digits
  const formatted = `(${local.slice(0, 3)}) ${local.slice(3, 6)}-${local.slice(6)}`
  return local === digits ? formatted : `+1 ${formatted}`
}
