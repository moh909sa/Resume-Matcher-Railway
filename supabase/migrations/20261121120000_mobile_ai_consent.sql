create table public.mobile_ai_consent (
    user_id uuid not null references auth.users(id) on delete cascade,
    version text not null check (version ~ '^[a-f0-9]{64}$'),
    accepted boolean not null,
    decided_at timestamptz not null default now(),
    primary key (user_id, version)
);

alter table public.mobile_ai_consent enable row level security;
revoke all on public.mobile_ai_consent from public, anon, authenticated;
grant select, insert, update on public.mobile_ai_consent to service_role;
