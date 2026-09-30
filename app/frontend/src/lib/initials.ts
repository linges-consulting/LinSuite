/**
 * The business's initials for its fallback mark: the first letter or digit of each of the
 * first two words, upper-cased. Words that hold no letter or digit ("&", "-") are skipped, so
 * "Smith & Jones Physio" is "SJ", not "S&".
 */
export function initialsOf(name: string): string {
  return name
    .split(/\s+/)
    .map((word) => word.match(/[\p{L}\p{N}]/u)?.[0] ?? '')
    .filter(Boolean)
    .slice(0, 2)
    .join('')
    .toUpperCase()
}
