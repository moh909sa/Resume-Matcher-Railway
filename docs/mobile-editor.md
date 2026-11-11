# Authenticated mobile CV editor

All routes below require a Supabase Auth bearer access token and use the
`/api/v1/mobile` prefix. The existing mobile deployment guard continues to block
standalone editor/export APIs. No Supabase schema change is required.

## Saved document and autosave

- `GET /tailored-cvs?status=ready&limit=30&offset=0` returns the existing array
  shape with bounded pagination (limit 1–100). Omitting status includes drafts.
- `GET /tailored-cvs/{id}/editor` returns `{tailoring_id, revision, updated_at,
  resume, layout, layout_options, confirmed_skills, job:{title,company,description}, analysis}`.
- `PATCH /tailored-cvs/{id}/editor` accepts `{expected_revision, mutation_id,
  resume, confirmed_skills, layout?}` and returns the same editor shape. `mutation_id`
  must be a UUID unique to that save. Content is limited to 512 KB, validated
  against the existing structured schema, and stored without dropping unknown
  fields or metadata. User-authored changes never invoke AI grounding or change
  the uploaded original. Confirmed skills are provenance, not independent verification.
- `layout` is the optional formatting object for this save. Start from the full
  object returned by `GET`, change the selected values and send the full object.
  Omitting `layout` or sending `null` keeps the previous formatting. An empty
  object resets to the engine defaults. Layout and resume content are committed
  together under the returned revision. Existing revisions default to Single
  Column and A4.
- Revision starts at 1 and increases per successful save. Canonical engine content,
  the immutable revision, and its mutation receipt commit in one engine transaction.
  A stale expected revision returns 409; retain local work and reconcile explicitly.
- Retrying an identical mutation returns its original acknowledged revision, even
  if later saves exist. Reusing the same UUID with different content returns 409.
  Clients should serialize requests, preserve the exact uncertain payload, and
  persist account-scoped recovery drafts before issuing mutations.
- `mobile_editor_revisions` is created additively by existing engine initialization
  in the private mobile SQLite store. Existing documents initialize lazily. Each
  snapshot remains available for revision-pinned rendering. Resume deletion
  removes its snapshots and receipts. The current engine Resume remains canonical;
  the registry's confirmation snapshot is historical, not the editor source.

### Layout options

`layout_options.templates` contains the existing Resume Matcher template IDs.
`layout_options.page_sizes` contains `A4` and `LETTER`. The saved `layout` uses
the web builder's formatting fields without its session fields:

```json
{
  "template": "modern",
  "pageSize": "A4",
  "margins": {"top": 10, "bottom": 10, "left": 10, "right": 10},
  "spacing": {"section": 3, "item": 2, "lineHeight": 3},
  "fontSize": {"base": 3, "headerScale": 3, "headerFont": "serif", "bodyFont": "sans-serif"},
  "compactMode": false,
  "showContactIcons": false,
  "accentColor": "blue"
}
```

Margins are 5–25 mm. Spacing and size levels are 1–5. Font families are
`serif`, `sans-serif`, and `mono`. Accent colors are `blue`, `green`, `orange`, and
`red`, and apply in templates that use an accent.

## Job Match

`GET /tailored-cvs/{id}/ats-score` returns saved analysis, or an unavailable
explanation. `POST` to the same path with `{expected_revision}` calculates
against exactly the latest saved revision. It uses cached job analysis and the
original deterministic scoring service; it makes no model request.

Analysis contains `status` (`current`, `stale`, `unavailable`), `revision`,
`calculated_at`, `scoring_version`, `job_hash`, and `score` (null if unavailable).
A scorable result also includes complete `matched_keywords`, `missing_keywords`,
`required_skills`, `preferred_skills`, `total_keywords`, and `matched_count`.
The `score` object contains `overall_score`, `sub_scores`, complete
`missing_keywords`, `injectable_keywords`, and `recommendations`.

HTML attributes/markup, hidden sections and condensed descriptions do not earn
matches. Document edits mark prior analysis stale. Job or scorer changes also
invalidate freshness. An absent or outdated job extraction cannot produce a
numeric score. Present this as a job-match estimate, never a hiring prediction.

## Actual PDF preview and download

`GET /tailored-cvs/{id}/pdf?revision=2` returns real PDF bytes from
an immutable owner-scoped snapshot using its saved layout. `revision` is required.
Optional `page_size=A4|LETTER` still overrides paper size for existing clients.
A concurrent newer save cannot mix content into this export. Clients
flush edits and check the current revision before preview/download.

The existing Resume Matcher print templates and Chromium renderer handle all pages
without a forced page/bullet cap. Snapshot contents travel only through the
existing short-lived render-draft token and loopback-only internal route; the token
is discarded after success or failure. No user JWT is placed in a print URL.

Headers: `Content-Type: application/pdf`, `Content-Disposition: attachment; ...`,
`X-Resume-Revision: 2`, `X-PDF-Page-Count` (measured from the generated PDF), and
`Cache-Control: private, no-store`. Disposition, revision and page-count headers
are exposed to configured CORS origins.

`POST /tailored-cvs/{id}/parse-check` accepts `{expected_revision,page_size?}` and
returns `{revision,report}` from the existing PDF text extraction/round-trip checks
on the same frozen content and layout. Omit `page_size` on both PDF requests to
use the saved paper size. Renderer failures return a retryable 503.

## Confirmation handoff

The current Keywords → Changes → Confirm & Save flow remains intact. Confirm
accepts `{expected_preview_id}` and rejects a different preview before claiming
it. Bodyless confirmation remains compatible with older clients. Open the editor
using the acknowledged tailoring ID only after `status=ready`.
