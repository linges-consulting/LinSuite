import { Badge } from '@/components/ui/badge'
import type { Classification } from '@/lib/api'

const LABEL: Record<Classification, { word: string; variant: 'info' | 'secondary' | 'success' }> = {
  new: { word: 'New', variant: 'info' },
  repeat: { word: 'Repeat', variant: 'secondary' },
  vip: { word: 'VIP', variant: 'success' },
}

/** The one place `new` / `repeat` / `vip` becomes a badge — the list, the profile and the
 *  booking dialog's search all show the same word in the same colour. Always the word: a
 *  colour alone is not a classification anybody can read out over the phone. */
export function ClassificationBadge({ classification }: { classification: Classification }) {
  const { word, variant } = LABEL[classification]
  return <Badge variant={variant}>{word}</Badge>
}
