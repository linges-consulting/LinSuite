import { expect, test } from 'vitest'
import pkg from '../package.json'

/**
 * `npx shadcn add` writes components that import `cn` from the bare specifier `"cn"` rather
 * than from `@/lib/utils`. There is a real package on npm by that name — it is a dependency
 * of the shadcn CLI itself — so the obvious way to make the error go away is to install it,
 * and then the import resolves to something that is not this project's `cn` at all.
 *
 * That residue has reached this branch three times. It survives review because nothing
 * visibly breaks: the components get corrected to `@/lib/utils`, the package is simply left
 * behind, and a dependency nothing imports is invisible until somebody wonders what it does.
 *
 * The rule is written down in `docs/DESIGN.md` under the shadcn vendoring section. This is
 * the part that notices when it is not followed.
 */

test('the `cn` package is not a dependency of this project', () => {
  // Nested under the shadcn CLI is fine and expected — that is the CLI's own business. What
  // must never appear is a direct dependency here, which only ever exists to satisfy a
  // generated import that should have been repointed at `@/lib/utils`.
  expect(Object.keys(pkg.dependencies ?? {})).not.toContain('cn')
  expect(Object.keys(pkg.devDependencies ?? {})).not.toContain('cn')
})

test('no source file imports cn from anywhere but @/lib/utils', () => {
  // The other half of the same rule, and the half that fails first: the package can only
  // come back because an import asked for it.
  const sources = import.meta.glob('../src/**/*.{ts,tsx}', {
    eager: true,
    query: '?raw',
    import: 'default',
  }) as Record<string, string>
  const offenders = Object.entries(sources)
    .filter(([, source]) => /from\s+['"]cn['"]/.test(source))
    .map(([path]) => path)

  expect(offenders).toEqual([])
})
