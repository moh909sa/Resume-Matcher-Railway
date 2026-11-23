-- Account deletion blocks direct reads immediately in either durable fence.
create or replace function public.cv_account_accessible() returns boolean language sql stable security definer set search_path='' as $$
select auth.uid() is not null
and not exists(select 1 from public.mobile_account_deletions d where d.user_id=auth.uid())
and not exists(select 1 from resume_engine.mobile_account_deletions d where d.owner_id=auth.uid()::text)
$$;
revoke all on function public.cv_account_accessible() from public,anon;
grant execute on function public.cv_account_accessible() to authenticated;
alter policy resume_files_select_own on storage.objects using(bucket_id='resume-files' and (storage.foldername(name))[1]=(select auth.uid()::text) and (select public.cv_account_accessible()));

create or replace view public.cv_saved_documents with(security_invoker=true) as
select t.id as tailoring_id,t.user_id,coalesce(v.revision,1) as revision,
 coalesce(v.created_at,r.updated_at) as updated_at,
 coalesce(v.document,r.processed_data) as resume,
 coalesce(v.layout::jsonb,'{"template":"swiss-single","pageSize":"A4","margins":{"top":10,"bottom":10,"left":10,"right":10},"spacing":{"section":3,"item":2,"lineHeight":3},"fontSize":{"base":3,"headerScale":3,"headerFont":"serif","bodyFont":"sans-serif"},"compactMode":false,"showContactIcons":false,"accentColor":"blue"}'::jsonb) as layout,
 coalesce(v.confirmed_skills::jsonb,'[]'::jsonb) as confirmed_skills,
 case when a.analysis is null then null
 when a.revision<>coalesce(v.revision,1) or a.analysis->>'scoring_version' is distinct from 'mobile-visible-v1'
 or a.analysis->>'job_hash' is distinct from encode(sha256(convert_to(coalesce(j.content,''),'UTF8')),'hex')
 then a.analysis::jsonb||'{"status":"stale"}'::jsonb else a.analysis::jsonb end as analysis,
 jsonb_build_object('title',t.job_title,'company',t.company,'description',coalesce(j.content,t.job_description,'')) as job,
 '{"templates":["swiss-single","swiss-two-column","modern","modern-two-column","latex","clean","vivid","vivid-single","modern-deedy","deedy"],"page_sizes":["A4","LETTER"]}'::jsonb as layout_options
from public.user_tailored_cvs t
join resume_engine.resumes r on r.resume_id=t.engine_resume_id::text and r.owner_id=t.user_id::text
left join resume_engine.jobs j on j.job_id=t.job_id::text and j.owner_id=t.user_id::text
left join lateral(select x.* from resume_engine.mobile_editor_revisions x where x.resume_id=r.resume_id and x.owner_id=r.owner_id order by revision desc limit 1) v on true
left join lateral(select x.analysis,x.revision from resume_engine.mobile_editor_revisions x where x.resume_id=r.resume_id and x.owner_id=r.owner_id and x.analysis is not null order by revision desc limit 1) a on true
where t.status='ready' and public.cv_account_accessible();

notify pgrst,'reload schema';
