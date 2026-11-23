set search_path=resume_engine,public;

CREATE TABLE IF NOT EXISTS api_keys (
	provider VARCHAR NOT NULL, 
	ciphertext TEXT NOT NULL, 
	updated_at VARCHAR NOT NULL, 
	PRIMARY KEY (provider)
)

;

CREATE TABLE IF NOT EXISTS applications (
	owner_id VARCHAR, 
	application_id VARCHAR NOT NULL, 
	job_id VARCHAR NOT NULL, 
	resume_id VARCHAR NOT NULL, 
	master_resume_id VARCHAR, 
	status VARCHAR NOT NULL, 
	company VARCHAR, 
	role VARCHAR, 
	applied_at VARCHAR, 
	notes TEXT, 
	position INTEGER NOT NULL, 
	created_at VARCHAR NOT NULL, 
	updated_at VARCHAR NOT NULL, 
	PRIMARY KEY (application_id), 
	CONSTRAINT uq_application_job_resume UNIQUE (job_id, resume_id)
)

;
CREATE INDEX IF NOT EXISTS ix_applications_status ON applications (status);
CREATE INDEX IF NOT EXISTS ix_applications_job_id ON applications (job_id);
CREATE INDEX IF NOT EXISTS ix_applications_resume_id ON applications (resume_id);

CREATE TABLE IF NOT EXISTS improvements (
	owner_id VARCHAR, 
	request_id VARCHAR NOT NULL, 
	original_resume_id VARCHAR NOT NULL, 
	tailored_resume_id VARCHAR NOT NULL, 
	job_id VARCHAR NOT NULL, 
	improvements JSON NOT NULL, 
	ats_score JSON, 
	forced_keywords JSON, 
	created_at VARCHAR NOT NULL, 
	PRIMARY KEY (request_id)
)

;
CREATE INDEX IF NOT EXISTS ix_improvements_tailored_resume_id ON improvements (tailored_resume_id);

CREATE TABLE IF NOT EXISTS jobs (
	owner_id VARCHAR, 
	job_id VARCHAR NOT NULL, 
	content TEXT NOT NULL, 
	resume_id VARCHAR, 
	created_at VARCHAR NOT NULL, 
	metadata_json JSON NOT NULL, 
	PRIMARY KEY (job_id)
)

;

CREATE TABLE IF NOT EXISTS mobile_account_deletions (
	owner_id VARCHAR NOT NULL, 
	receipt_hash VARCHAR NOT NULL, 
	complete BOOLEAN NOT NULL, 
	PRIMARY KEY (owner_id), 
	UNIQUE (receipt_hash)
)

;

CREATE TABLE IF NOT EXISTS mobile_editor_revisions (
	resume_id VARCHAR NOT NULL, 
	revision INTEGER NOT NULL, 
	owner_id VARCHAR NOT NULL, 
	document JSON NOT NULL, 
	layout JSON, 
	confirmed_skills JSON NOT NULL, 
	mutation_id VARCHAR, 
	mutation_hash VARCHAR, 
	analysis JSON, 
	created_at VARCHAR NOT NULL, 
	PRIMARY KEY (resume_id, revision), 
	UNIQUE (owner_id, resume_id, mutation_id)
)

;
CREATE INDEX IF NOT EXISTS ix_mobile_editor_revisions_owner_id ON mobile_editor_revisions (owner_id);

CREATE TABLE IF NOT EXISTS queue_items (
	owner_id VARCHAR, 
	item_id VARCHAR NOT NULL, 
	resume_id VARCHAR NOT NULL, 
	job_description TEXT NOT NULL, 
	role VARCHAR NOT NULL, 
	company VARCHAR NOT NULL, 
	job_req_id VARCHAR NOT NULL, 
	status VARCHAR NOT NULL, 
	job_id VARCHAR, 
	tailored_resume_id VARCHAR, 
	title VARCHAR, 
	error TEXT, 
	rate_limit_seconds FLOAT NOT NULL, 
	prompt_id VARCHAR, 
	created_at VARCHAR NOT NULL, 
	updated_at VARCHAR NOT NULL, 
	PRIMARY KEY (item_id)
)

;
CREATE INDEX IF NOT EXISTS ix_queue_items_resume_id ON queue_items (resume_id);
CREATE INDEX IF NOT EXISTS ix_queue_items_status ON queue_items (status);

CREATE TABLE IF NOT EXISTS resumes (
	resume_id VARCHAR NOT NULL, 
	owner_id VARCHAR, 
	content TEXT NOT NULL, 
	content_type VARCHAR NOT NULL, 
	filename VARCHAR, 
	is_master BOOLEAN NOT NULL, 
	is_default_master BOOLEAN NOT NULL, 
	parent_id VARCHAR, 
	processed_data JSON, 
	processing_status VARCHAR NOT NULL, 
	processing_token VARCHAR, 
	cover_letter TEXT, 
	outreach_message TEXT, 
	interview_prep TEXT, 
	title VARCHAR, 
	original_markdown TEXT, 
	template_settings JSON, 
	created_at VARCHAR NOT NULL, 
	updated_at VARCHAR NOT NULL, 
	PRIMARY KEY (resume_id)
)

;
CREATE INDEX IF NOT EXISTS ix_resumes_owner ON resumes (owner_id);
CREATE UNIQUE INDEX IF NOT EXISTS ux_resumes_single_default_master ON resumes (is_default_master) WHERE is_default_master AND owner_id IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ux_resumes_owner_default_master ON resumes (owner_id) WHERE is_default_master AND owner_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS tailoring_previews (
	owner_id VARCHAR, 
	improvements JSON, 
	source_data JSON, 
	preview_id VARCHAR NOT NULL, 
	source_id VARCHAR NOT NULL, 
	job_id VARCHAR NOT NULL, 
	payload_hash VARCHAR NOT NULL, 
	source_hash VARCHAR NOT NULL, 
	job_hash VARCHAR NOT NULL, 
	created_at VARCHAR NOT NULL, 
	expires_at VARCHAR NOT NULL, 
	result_resume_id VARCHAR, 
	claim_token VARCHAR, 
	claim_expires_at VARCHAR, 
	response_data JSON, 
	PRIMARY KEY (preview_id)
)

;
CREATE INDEX IF NOT EXISTS ix_tailoring_previews_expires_at ON tailoring_previews (expires_at);
CREATE INDEX IF NOT EXISTS ix_tailoring_previews_job_id ON tailoring_previews (job_id);
CREATE INDEX IF NOT EXISTS ix_tailoring_previews_source_id ON tailoring_previews (source_id);
CREATE INDEX IF NOT EXISTS ix_tailoring_previews_result_resume_id ON tailoring_previews (result_resume_id);
CREATE INDEX IF NOT EXISTS ix_preview_compatibility ON tailoring_previews (source_id, job_id, payload_hash, created_at);
create table if not exists resume_engine.migration_manifest (
  migration_key text primary key,
  status text not null check(status in ('backed_up','complete')),
  manifest jsonb not null,
  completed_at timestamptz not null default now()
);
revoke all on all tables in schema resume_engine from public,anon,authenticated;
grant all on all tables in schema resume_engine to resume_engine_writer;
grant usage on schema resume_engine to service_role;
grant all on all tables in schema resume_engine to service_role;
create or replace function public.cv_account_accessible() returns boolean
language sql stable security definer set search_path='' as $$
 select auth.uid() is not null and not exists(
 select 1 from public.mobile_account_deletions where user_id=auth.uid())
$$;
revoke all on function public.cv_account_accessible() from public,anon;
grant execute on function public.cv_account_accessible() to authenticated;


alter table resume_engine.resumes enable row level security;
create policy engine_service_access on resume_engine.resumes for all to resume_engine_writer using(true) with check(true);
create policy engine_owner_read on resume_engine.resumes for select to authenticated using(owner_id=(select auth.uid()::text) and (select public.cv_account_accessible()));


alter table resume_engine.jobs enable row level security;
create policy engine_service_access on resume_engine.jobs for all to resume_engine_writer using(true) with check(true);
create policy engine_owner_read on resume_engine.jobs for select to authenticated using(owner_id=(select auth.uid()::text) and (select public.cv_account_accessible()));


alter table resume_engine.improvements enable row level security;
create policy engine_service_access on resume_engine.improvements for all to resume_engine_writer using(true) with check(true);
create policy engine_owner_read on resume_engine.improvements for select to authenticated using(owner_id=(select auth.uid()::text) and (select public.cv_account_accessible()));


alter table resume_engine.applications enable row level security;
create policy engine_service_access on resume_engine.applications for all to resume_engine_writer using(true) with check(true);
create policy engine_owner_read on resume_engine.applications for select to authenticated using(owner_id=(select auth.uid()::text) and (select public.cv_account_accessible()));


alter table resume_engine.queue_items enable row level security;
create policy engine_service_access on resume_engine.queue_items for all to resume_engine_writer using(true) with check(true);
create policy engine_owner_read on resume_engine.queue_items for select to authenticated using(owner_id=(select auth.uid()::text) and (select public.cv_account_accessible()));


alter table resume_engine.tailoring_previews enable row level security;
create policy engine_service_access on resume_engine.tailoring_previews for all to resume_engine_writer using(true) with check(true);
create policy engine_owner_read on resume_engine.tailoring_previews for select to authenticated using(owner_id=(select auth.uid()::text) and (select public.cv_account_accessible()));


alter table resume_engine.mobile_editor_revisions enable row level security;
create policy engine_service_access on resume_engine.mobile_editor_revisions for all to resume_engine_writer using(true) with check(true);
create policy engine_owner_read on resume_engine.mobile_editor_revisions for select to authenticated using(owner_id=(select auth.uid()::text) and (select public.cv_account_accessible()));


alter table resume_engine.api_keys enable row level security;
create policy engine_service_access on resume_engine.api_keys for all to resume_engine_writer using(true) with check(true);

alter table resume_engine.mobile_account_deletions enable row level security;
create policy engine_service_access on resume_engine.mobile_account_deletions for all to resume_engine_writer using(true) with check(true);

alter table resume_engine.migration_manifest enable row level security;
create policy engine_service_access on resume_engine.migration_manifest for all to resume_engine_writer using(true) with check(true);
grant select on resume_engine.resumes,resume_engine.jobs,resume_engine.mobile_editor_revisions to authenticated;

create view public.cv_originals with (security_invoker=true) as
select u.id,u.user_id,u.original_filename,u.display_name,u.is_default,u.mime_type,u.file_size_bytes,
 case when r.resume_id is null and u.processing_status='ready' then 'failed' else coalesce(r.processing_status,u.processing_status) end as processing_status,
 u.created_at,u.updated_at,u.storage_bucket,u.storage_path,
 (r.resume_id is null and u.processing_status='ready') as recovery_required,
 r.processed_data as processed_resume
from public.user_resumes u left join resume_engine.resumes r
on r.resume_id=u.engine_resume_id::text and r.owner_id=u.user_id::text
where public.cv_account_accessible();

create view public.cv_saved_library with(security_invoker=true) as
select t.id,t.user_id,t.job_title,t.company,t.status,t.created_at,
 coalesce(r.updated_at::timestamptz,t.updated_at) as updated_at
from public.user_tailored_cvs t join resume_engine.resumes r
on r.resume_id=t.engine_resume_id::text and r.owner_id=t.user_id::text
where t.status='ready' and public.cv_account_accessible();

create view public.cv_saved_documents with(security_invoker=true) as
select t.id as tailoring_id,t.user_id,coalesce(v.revision,1) as revision,
 coalesce(v.created_at,r.updated_at) as updated_at,
 coalesce(v.document,r.processed_data) as resume,
 coalesce(v.layout::jsonb,'{"template":"swiss-single","pageSize":"A4","margins":{"top":10,"bottom":10,"left":10,"right":10},"spacing":{"section":3,"item":2,"lineHeight":3},"fontSize":{"base":3,"headerScale":3,"headerFont":"serif","bodyFont":"sans-serif"},"compactMode":false,"showContactIcons":false,"accentColor":"blue"}'::jsonb) as layout,
 coalesce(v.confirmed_skills::jsonb,'[]'::jsonb) as confirmed_skills,
 case when a.analysis is null then null
 when a.revision<>coalesce(v.revision,1) or a.analysis->>'scoring_version'<>'mobile-visible-v1'
 then a.analysis::jsonb||'{"status":"stale"}'::jsonb else a.analysis::jsonb end as analysis,
 jsonb_build_object('title',t.job_title,'company',t.company,'description',coalesce(j.content,t.job_description,'')) as job,
 '{"templates":["swiss-single","swiss-two-column","modern","modern-two-column","latex","clean","vivid","vivid-single","modern-deedy","deedy"],"page_sizes":["A4","LETTER"]}'::jsonb as layout_options
from public.user_tailored_cvs t
join resume_engine.resumes r on r.resume_id=t.engine_resume_id::text and r.owner_id=t.user_id::text
left join resume_engine.jobs j on j.job_id=t.job_id::text and j.owner_id=t.user_id::text
left join lateral(select x.* from resume_engine.mobile_editor_revisions x where x.resume_id=r.resume_id and x.owner_id=r.owner_id order by revision desc limit 1) v on true
left join lateral(select x.analysis,x.revision from resume_engine.mobile_editor_revisions x where x.resume_id=r.resume_id and x.owner_id=r.owner_id and x.analysis is not null order by revision desc limit 1) a on true
where t.status='ready' and public.cv_account_accessible();

create table public.cv_pdf_artifacts(
 tailoring_id uuid not null references public.user_tailored_cvs(id) on delete cascade,
 user_id uuid not null references auth.users(id) on delete cascade,
 revision integer not null check(revision>=1),
 storage_bucket text not null default 'cv-pdf-artifacts' check(storage_bucket='cv-pdf-artifacts'),
 storage_path text not null,
 sha256 text not null check(sha256~'^[a-f0-9]{64}$'),
 file_size_bytes integer not null check(file_size_bytes>0),
 page_count integer check(page_count>0),
 created_at timestamptz not null default now(),
 primary key(tailoring_id,revision),
 check(storage_path=user_id::text||'/'||tailoring_id::text||'/'||revision::text||'.pdf')
);
alter table public.cv_pdf_artifacts enable row level security;
revoke all on public.cv_pdf_artifacts from public,anon,authenticated;
grant select on public.cv_pdf_artifacts to authenticated;
grant all on public.cv_pdf_artifacts to service_role;
create policy pdf_owner_read on public.cv_pdf_artifacts for select to authenticated using(user_id=(select auth.uid()) and (select public.cv_account_accessible()));
create policy pdf_storage_owner_read on storage.objects for select to authenticated using(
 bucket_id='cv-pdf-artifacts' and (storage.foldername(name))[1]=(select auth.uid()::text)
 and (select public.cv_account_accessible())
 and exists(select 1 from public.cv_pdf_artifacts p where p.storage_path=name and p.user_id=(select auth.uid()))
);
revoke all on public.cv_originals,public.cv_saved_library,public.cv_saved_documents from public,anon;
grant select on public.cv_originals,public.cv_saved_library,public.cv_saved_documents to authenticated;
notify pgrst,'reload schema';

create table public.cv_engine_backup_objects(storage_path text not null,user_id uuid not null,created_at timestamptz not null default now(),primary key(storage_path,user_id));
alter table public.cv_engine_backup_objects enable row level security;
revoke all on public.cv_engine_backup_objects from public,anon,authenticated;
grant all on public.cv_engine_backup_objects to service_role;
grant insert,select on public.cv_engine_backup_objects to resume_engine_writer;
create policy engine_track_backup on public.cv_engine_backup_objects for all to resume_engine_writer using(true) with check(true);
