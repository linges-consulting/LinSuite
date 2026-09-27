# M2 intake forms completion

Completed 2026-09-27 against `.superpowers/sdd/m2-forms.md` and the subsequent owner decisions in its progress ledger. This finishes the eight **intake forms** tasks; later M2 session-notes work is separate.

| Task | Implementation |
| --- | --- |
| 1. Document store | Existing encrypted store, authenticated digest, immutable database guards and atomic crypto-shredding retained and regression-tested. |
| 2. Form builder | Existing drafts and immutable published versions retained; flag-only publishing now has explicit regression coverage. |
| 3. Secure links | Existing pinned, single-use, 48-hour fragment links retained. The issue dialog adds a larger tablet QR view. |
| 4. Signed submissions | Existing sealed, version-bound, idempotent submissions retained, including signature validation and chart-hold atomicity. |
| 5. PDF archival | Celery now renders escaped HTML with the pinned wording, visible answers, embedded signature/logo and record footer. Field help is retained in both blank printouts and submitted archives. A periodic reconciliation task repairs submissions whose archive enqueueing was interrupted. PDFs are encrypted through the document store. Staff fetches are authenticated, logged and marked `no-store`; metadata polls while rendering. |
| 6. Tablet/offline | Native IndexedDB stores drafts and queued submissions. The production build precaches the public form shell and its static assets in a service worker; APIs and authenticated pages are excluded. Reload restores the pinned version and answers; online events and a 15-second timer retry the same id and payload. Success, invalid links, version mismatch and expired-cache cleanup remove local answers. A lost acknowledgement can reconcile after reload without looking up a consumed link. |
| 7. Print/scan | Staff can print a blank published version and upload 1–8 JPEG/PNG pages. Browser downsampling keeps the JSON request below the edge limit. Server image validation and grayscale conversion produce a 150-DPI PDF; submission, document, key and any clinical hold commit together. |
| 8. Compliance | Existing review fixes are retained: any qualifying submission satisfies compliance; a re-signature publication revokes older open links; applicability uses the business's local day; inactive services and unknown/suppressed clients are refused. Signature threshold drift has shared-fixture coverage. |

The tablet flow follows the owner's QR-only decision: an unsigned clinic tablet scans the code. No staff-session handoff or kiosk mode was added.

Implementation used successive TDD slices at the already agreed HTTP/PostgreSQL, pure-function, rendered-HTML, database-guard and frontend user-flow seams. Additional browser findings were reproduced in tests before fixing them: asynchronous popup opening, blocked session storage, and changed scan pages after an unconfirmed upload. Print/PDF tabs now open during the user gesture and immediately detach their opener; failed fetches close the preview. Scan retries keep their original pages and id.

## Verification

All final checks passed:

- Backend: `uv run ruff check .`, `uv run ruff format --check .`, and `DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib uv run pytest -q --tb=short` — **1,177 passed**.
- Frontend: `npm run lint`, `npm run build`, and `npm test` — **348 passed across 47 files**.
- `git diff --check` passed.
- The production frontend build retains the existing advisory about a JavaScript chunk larger than 500 kB; it does not fail the build.

- Backend: real PostgreSQL and Redis test containers, migrated database, application-role HTTP requests, grant/trigger checks, encrypted document integrity, erasure retries and atomic-failure checks.
- Frontend: offline draft recovery, queued replay after reload, duplicate-online-event prevention, version refusal, expiry, blocked session storage, PDF readiness and preview opening, large-photo downsampling, scan error/retry handling, and readable access-history labels.
- Docker images build with Pango, HarfBuzz and DejaVu fonts. CI installs the same native renderer packages.
- An isolated disposable clinic ran the host API/frontend and the actual Linux Celery worker image. A browser submission produced an encrypted archive; the worker reported success, the profile showed a ready PDF, its HTTP fetch returned 200, and Admin Mode showed the PDF access row.
- Browser QA also covered persisted signature state after reload, completion without displayed answers, the large tablet QR, blank print wording, two camera-sized synthetic scans, their PDF fetch, and light/dark layouts.
- Screenshots: `.superpowers/sdd/m2-forms/forms-light.png`, `scan-dark.png`, and `print-blank.png`. All use synthetic disposable data.

Production offline browser QA passed with both owned API and frontend servers stopped: a full reload restored the shell and synthetic draft, submission entered the local queue, and restarting the servers automatically delivered it and cleared the answers. The screenshot is `.superpowers/sdd/m2-forms/offline-reload.png`. Offline loading requires an earlier online visit and service-worker support on HTTPS or localhost. The manual DevTools toggle itself was unavailable; stopping both servers exercised the actual network failure instead. The browser security policy prevents inspecting `blob:` preview tabs; their creation and authenticated fetches were verified, and archive content is covered at the renderer and HTTP seams.

## Development notes

Docker includes the renderer's native libraries. For direct macOS backend tests, install Pango with Homebrew and use `DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib` on Apple Silicon when the loader cannot find it.

Existing in-progress compliance and signature changes were preserved. The required [Standards and Spec review](m2-review.md) passed after its findings were fixed. These tasks and session notes are included in the local M2 completion commit. No remote issues were closed or pull requests created.
