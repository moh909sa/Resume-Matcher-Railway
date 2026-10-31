# Background CV tailoring — mobile API (October 2026)

Mobile CV tailoring no longer requires keeping a multi-minute HTTP POST alive. The existing
`POST /api/v1/mobile/tailor/preview` is **legacy** for older builds and remains
synchronous. New Draftbit builds must use the durable queue described below.

## Endpoints (all require Supabase user Bearer access token)

1. `POST /api/v1/mobile/tailor/jobs` with:
   `{"resume_id":"UUID","job_description":"50–12000 characters","progress_id":"fresh UUID v4"}`.
   Optional `max_pages` 2/3 and `max_bullets_per_entry` 3/5; omit for our
   default No Limit + All + Full Tailor.
   Returns **202 immediately**: `{progress_id,status,tailoring_id,...}`.
   `status` initially `queued`; other states can be returned on an identical
   idempotent retry or matching active/recent result. NEVER retry with a new
   UUID after an ambiguous network failure; first check the existing UUID and
   recent jobs.
2. `GET /api/v1/mobile/tailor/progress/{progress_id}` every 1.75–2 seconds
   **while the app is active**. States: queued, running, succeeded, failed,
   interrupted. Mark checks using actual `completed_steps` only.
3. `GET /api/v1/mobile/tailor/jobs/recent` after app resume or fresh launch
   to discover the latest user-owned unfinished or recently finished work.
4. After `status=succeeded`, GET
   `/api/v1/mobile/tailor/jobs/{progress_id}/result`. It returns the
   unchanged `TailoringPreview` + review data as the former preview POST.
   Then `router.replace("/tailoring/review")` automatically.
   Never call `POST /tailor/jobs` just to reload a finished job.
5. The existing `/tailor/{tailoring_id}/review`, keyword application, and
   Confirm & Save calls are **unchanged**.

## Required Draftbit agent changes

- Replace the *long-lived* preview POST with the quick 202 job enqueue above.
  Remove dependence on an HTTP request remaining open during AI processing.
- Before POST, securely generate and locally persist (AsyncStorage) the job's
  `progress_id`, selected resume and job description (ideally do not persist
  long job text unnecessarily once job is acknowledged).
- Use the returned `progress_id` from 202 if deduplication returned an existing
  job rather than the new UUID. Store the server's canonical ID immediately.
- Poll the progress/status route; show current real stage and elapsed time.
  While `queued`, display "Waiting to begin" without fake progress.
- On success, fetch the saved preview via /jobs/{id}/result and navigate
  automatically to Keywords. On failure, display a recoverable error without
  silently running AI again. On `interrupted`, explain that the server was
  restarted and the job was not automatically retried to avoid duplicate AI
  charges; user may explicitly start a new attempt.
- On AppState return to foreground or relaunch, fetch recent jobs and reconcile
  saved progress. On background, polling can pause, but the Railway worker
  continues in server space; no iOS background execution entitlement needed.
- Preserve prior screens, colors, all seven real stage labels, resume ownership,
  keyword review, Confirm & Save. Do not build new editor/export yet.
- Maintain protection against duplicate taps and authorization failures.

## Safety and failure design

- Durable mobile tailoring has its own `MOBILE_TAILORING_TIMEOUT_SECONDS`
  budget (default 360 seconds, configurable from 60 to 1800). Synchronous
  HTTP operations retain `REQUEST_TIMEOUT_SECONDS` (default 240). An active
  background deadline is not shortened by the HTTP default; explicit provider
  call timeouts and nested earlier deadlines still apply.
- Resume Matcher's unchanged critic/rewrite algorithm may use the remaining
  operation time, reserving 30 seconds for final checks and draft persistence.
  There is no separate 45-second writing cap. If less than 15 seconds of safe
  writing time remains, no additional writing call is started.
- If writing fails or exhausts its allowance, the core tailored draft is kept
  and the review `warnings` explains that writing did not finish. Failed writing
  steps stay out of `completed_steps`, even when draft persistence succeeds.
  Neither deadline expiry nor a worker restart automatically resubmits the
  entire AI operation. Provider-level retry policies remain unchanged.

- One queued/running job per account (partial unique DB index); repeated inputs
  reuse the same in-flight or recent completed job. User-provided UUID is a
  stable idempotency key, not authorization.
- Only the server's Supabase service credential may write job progress.
  Authenticated owners may read their own rows with RLS.
- A Railway lifespan worker atomically claims each queued job and heartbeats its
  3-minute lease. One worker processes at most one job at a time in each server
  instance; PostgreSQL conditional claim makes this safe across replicas.
- AI task is not bound to a mobile connection or app lifecycle.
- A worker crash/redeployment never silently triggers a new AI call. Expired
  in-flight jobs are reconciled to existing saved drafts if available; otherwise
  marked interrupted. This deliberately avoids accidental repeated AI charges.
  The remaining tiny crash window *before result persistence* cannot provide
  full exactly-once external AI billing guarantees; explicit retry is required.
- Saved preview TTL is currently governed by the existing engine
  `preview_ttl_seconds` (default 24 hours). Recovery beyond preview expiry
  requires a separate design, not a secret forced regeneration.
- User-facing status routes never return private job descriptions, CV content,
  service-role tokens, or internal worker claim tokens.
