/**
 * Zero overlap (tech-stack §13): events at the same time sit side by side, never stacked
 * and never hidden behind a "+1". Two → 50 % each, three → 33 %, N → 1/N.
 *
 * Hand-written on purpose — the layout rule is the product's, and it should be read here in
 * twenty lines rather than configured in a library. The algorithm is the classic one:
 *
 * 1. Sort by start (longer first on a tie, so the long one takes the left column).
 * 2. Walk them into **clusters** — runs where each next event starts before everything so
 *    far has ended. A cluster is the set of events that have to share the width.
 * 3. Inside a cluster, hand each event the first column whose last event has ended.
 *
 * The cluster's column count is N; every event in it is 1/N wide at its column. A chain
 * (A 9–10, B 9:30–10:30, C 10–11) is one cluster of two columns: C reuses A's, so the three
 * of them are two abreast, not three. Neighbours that only touch (`end === start`) are
 * separate clusters and full width — half-open intervals, the same convention as the
 * engine and the exclusion constraint.
 */

export type Packable = { id: string; start: number; end: number }

export type Placement = { column: number; columns: number }

export function pack(events: Packable[]): Map<string, Placement> {
  const sorted = [...events].sort((a, b) => a.start - b.start || b.end - a.end)
  const out = new Map<string, Placement>()

  let cluster: Packable[] = []
  let clusterEnd = -Infinity
  const flush = () => {
    if (cluster.length === 0) return
    // Column → when its last occupant ends.
    const ends: number[] = []
    const chosen: [Packable, number][] = []
    for (const event of cluster) {
      let column = ends.findIndex((end) => end <= event.start)
      if (column === -1) column = ends.push(event.end) - 1
      else ends[column] = event.end
      chosen.push([event, column])
    }
    for (const [event, column] of chosen) out.set(event.id, { column, columns: ends.length })
    cluster = []
    clusterEnd = -Infinity
  }

  for (const event of sorted) {
    if (event.start >= clusterEnd) flush()
    cluster.push(event)
    clusterEnd = Math.max(clusterEnd, event.end)
  }
  flush()
  return out
}
