-- Anyone with the page source could delete the database.
--
-- The anon key is published in the HTML by design — a browser holds it either
-- way, and RLS is what is supposed to make that safe. It was not. Measured
-- against the live project, every `public.rsi_*` view granted anon INSERT,
-- UPDATE, DELETE *and* TRUNCATE, and `security_invoker` was unset on all five,
-- so the views ran as their owner and bypassed the policies on `rsi.*`
-- entirely. The policies were decorative. `DELETE /rest/v1/rsi_rollouts?id=gt.0`
-- would have dropped every scored window; `POST /rest/v1/rsi_runs` with
-- `accepted: true` would have put a fabricated promotion at the top of the
-- front page. An audit demonstrated it by accident and left the row behind.
--
-- The cause is a default, not a line anyone wrote. Supabase grants ALL on new
-- objects in `public` to anon and authenticated; `schema.sql` only ever ran
-- `grant select`, and never revoked what it inherited. Every
-- `create or replace view` since has re-inherited it — including the ones added
-- by 002 earlier tonight.
--
-- So: revoke explicitly rather than trusting a default, and make the views
-- honour the RLS they sit on top of. The one write a visitor may still make is
-- `rsi_cast_vote`, which is `security definer` on purpose and computes the
-- numbers it stores rather than accepting them.

begin;

-- The row the audit left behind, proving the hole was real.
delete from rsi.runs where id = '__probe_do_not_keep__';

-- 1. Take back everything, on the views and on the tables beneath them.
revoke all on public.rsi_runs, public.rsi_rollouts, public.rsi_traces,
              public.rsi_votes, public.rsi_live_forecasts
  from anon, authenticated;
revoke all on rsi.runs, rsi.rollouts, rsi.traces, rsi.votes, rsi.live_forecasts
  from anon, authenticated;
revoke all on sequence rsi.votes_id_seq from anon, authenticated;
revoke all on sequence rsi.rollouts_id_seq from anon, authenticated;

-- 2. Make the views run as the caller, so the policies on rsi.* actually apply.
--    Without this a view is a hole straight through RLS, which is what it was.
alter view public.rsi_runs            set (security_invoker = on);
alter view public.rsi_rollouts        set (security_invoker = on);
alter view public.rsi_traces          set (security_invoker = on);
alter view public.rsi_votes           set (security_invoker = on);
alter view public.rsi_live_forecasts  set (security_invoker = on);

-- 3. Give back only reading, and only through the views.
grant usage on schema rsi to anon, authenticated;
grant select on rsi.runs, rsi.rollouts, rsi.traces, rsi.votes, rsi.live_forecasts
  to anon, authenticated;
grant select on public.rsi_runs, public.rsi_rollouts, public.rsi_traces,
                public.rsi_votes, public.rsi_live_forecasts
  to anon, authenticated;

-- 4. And the one write, which validates what it stores.
grant execute on function public.rsi_cast_vote(text, text, text, text, text, text)
  to anon, authenticated;

-- 5. Stop the default from coming back the next time a view is replaced.
alter default privileges in schema public revoke all on tables from anon, authenticated;
alter default privileges in schema rsi    revoke all on tables from anon, authenticated;

commit;
