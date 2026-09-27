import type { DiagramId } from '@/lib/api'

export const DIAGRAM_LABELS: Record<DiagramId, string> = {
  body_front: 'Body · front',
  body_back: 'Body · back',
  layout: 'Floor / project layout',
}

// Record data, independent of theme/brand tokens so historical marks keep their colour.
export const ANNOTATION_COLOURS = [
  { value: '#DC2626', label: 'Red' },
  { value: '#2563EB', label: 'Blue' },
  { value: '#16A34A', label: 'Green' },
  { value: '#9333EA', label: 'Purple' },
  { value: '#D97706', label: 'Amber' },
]
