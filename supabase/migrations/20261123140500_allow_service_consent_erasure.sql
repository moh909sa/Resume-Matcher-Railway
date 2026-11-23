-- Consent remains inaccessible to app clients; only trusted deletion may erase it.
GRANT DELETE ON TABLE public.mobile_ai_consent TO service_role;
