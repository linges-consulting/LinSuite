/// <reference types="vitest/config" />
import path from 'node:path'
import { readFileSync } from 'node:fs'
import { createHash } from 'node:crypto'
import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

export default defineConfig({
  plugins: [
    react(),
    tailwindcss(),
    {
      name: 'tablet-form-shell',
      apply: 'build',
      generateBundle(_, bundle) {
        const assets = [
          '/f/',
          ...Object.keys(bundle)
            .filter((name) => /\.(js|css|woff2?)$/.test(name))
            .map((name) => `/${name}`),
        ]
        const version = createHash('sha256')
          .update(JSON.stringify(assets))
          .digest('hex')
          .slice(0, 16)
        const source = readFileSync(
          path.resolve(import.meta.dirname, 'build/form-offline-worker.js'),
          'utf8',
        )
          .replace(
            'const ASSETS = []',
            `const ASSETS = ${JSON.stringify(assets)}`,
          )
          .replace(
            'linsuite-form-shell-build',
            `linsuite-form-shell-${version}`,
          )
        this.emitFile({
          type: 'asset',
          fileName: 'form-offline-worker.js',
          source,
        })
      },
    },
  ],
  resolve: {
    alias: { '@': path.resolve(import.meta.dirname, './src') },
  },
  server: {
    // Backend is reached through traefik (compose) on :80; override with VITE_API_PROXY.
    proxy: { '/api': process.env.VITE_API_PROXY ?? 'http://localhost' },
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./tests/setup.ts'],
    include: ['src/**/*.test.{ts,tsx}', 'tests/**/*.test.{ts,tsx}'],
  },
})
