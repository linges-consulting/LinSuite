/** Short-lived drafts on this device only. Each operation closes its IndexedDB connection. */
import type { PublicForm } from '@/lib/api'
import type { Answers } from '@/lib/forms'

export type CachedForm = {
  token: string
  form: PublicForm
  answers: Answers
  submissionId: string
  queued: boolean
}

export const ACTIVE_FORM = 'linsuite:active-form'

export function activeFormToken(): string {
  try { return sessionStorage.getItem(ACTIVE_FORM) ?? '' }
  catch { return '' }
}

export function rememberActiveForm(token: string): void {
  try { sessionStorage.setItem(ACTIVE_FORM, token) }
  catch { /* Drafts still work for this page when the browser blocks session storage. */ }
}

export function forgetActiveForm(): void {
  try { sessionStorage.removeItem(ACTIVE_FORM) }
  catch { /* No pointer was stored. */ }
}

async function transact<T>(mode: IDBTransactionMode, operation: (store: IDBObjectStore) => IDBRequest<T>): Promise<T> {
  const db = await new Promise<IDBDatabase>((resolve, reject) => {
    const request = indexedDB.open('linsuite-forms', 1)
    request.onupgradeneeded = () => request.result.createObjectStore('drafts', { keyPath: 'token' })
    request.onsuccess = () => resolve(request.result)
    request.onerror = () => reject(request.error)
  })
  try {
    return await new Promise<T>((resolve, reject) => {
      const tx = db.transaction('drafts', mode)
      const request = operation(tx.objectStore('drafts'))
      tx.oncomplete = () => resolve(request.result)
      tx.onerror = () => reject(tx.error)
      tx.onabort = () => reject(tx.error)
    })
  } finally { db.close() }
}

export async function purgeExpiredForms(): Promise<void> {
  await transact('readwrite', (store) => {
    const request = store.openCursor()
    request.onsuccess = () => {
      const cursor = request.result
      if (!cursor) return
      if (Date.parse((cursor.value as CachedForm).form.expires_at) <= Date.now()) cursor.delete()
      cursor.continue()
    }
    return request
  })
}

export async function readCachedForm(token: string): Promise<CachedForm | undefined> {
  await purgeExpiredForms()
  return transact('readonly', (store) => store.get(token))
}

export async function saveCachedForm(entry: CachedForm): Promise<void> {
  await transact('readwrite', (store) => store.put(entry))
}

export async function deleteCachedForm(token: string): Promise<void> {
  await transact('readwrite', (store) => store.delete(token))
}
