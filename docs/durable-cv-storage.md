# Durable CV storage

The mobile deployment uses the existing SQLAlchemy PostgreSQL backend in the
private Supabase `resume_engine` schema. `DATABASE_URL` uses a dedicated login
with access only to that schema. `DATABASE_SCHEMA=resume_engine` selects it for
both mobile and standalone engine sessions; it never falls back to SQLite.
Server credentials stay in Railway variables. Processing, prompts, verification,
scoring and the PDF renderer are unchanged.

Set up the private schema and non-superuser `resume_engine_writer` login through
administrative database access before applying the migration. Set its generated
password only in Railway's server-side DATABASE_URL. Use Supavisor's session
pooler on port 5432 with TLS. Never expose this URL in a client environment.

`MIGRATE_ENGINE_SQLITE=1` invokes a fail-closed startup migration before workers
or requests. Both SQLite files are backed up with SQLite's WAL-aware backup API.
The private archive also preserves config, the encryption key and existing
uploads. Its downloaded checksum is verified before the import. Every model row
is copied in one PostgreSQL transaction with original IDs, ownership, JSON,
formatting, revision numbers, analyses and mutation receipts. Canonical row hashes
and counts must match before `migration_manifest` becomes complete. Unknown
schemas, conflicting identities or a nonempty destination abort the cutover.
Completed migrations never reimport retired files. No source file/volume is
removed. Do not roll back to a SQLite-writing deployment after cutover.

Authenticated clients read security-invoker views `cv_originals`,
`cv_saved_library`, and `cv_saved_documents` through Supabase. Underlying engine
tables have owner SELECT policies; unrelated engine data, keys and backups have
no customer access. Existing registry RLS stays in force. The views do not invoke
Railway. Editor mutations retain server validation, revision conflicts and
idempotent receipts, and are acknowledged only after PostgreSQL commit.

Canonical generated PDFs are persisted in private `cv-pdf-artifacts` Storage at
`owner/tailoring_id/revision.pdf` before publishing a checksummed manifest. They
are immutable and never substituted across revisions. Page-size overrides are
render-only; the mobile app saves layout before requesting a canonical artifact.
`BACKFILL_SAVED_PDFS=1` creates missing latest-revision artifacts using the existing
deterministic renderer, with no AI requests. Old exports were response streams,
not a retained archive. New rendering still needs Railway; existing artifacts do
not. Source uploads remain private in `resume-files`.

Account deletion fences writes, erases live engine/registry rows and PDF Storage,
and scrubs that owner's rows from retired SQLite files. A migration archive is
retired in full if any included account is deleted. Backup tracking is server-only.
Keep the retired volume until a separately approved retention/retirement task.
