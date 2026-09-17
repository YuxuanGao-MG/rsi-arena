-- What the run is doing right now.
--
-- One row per run, upserted by the loop as it moves through its phases, so the
-- reader can be a live instrument instead of an archive of finished runs. The
-- shape follows what worked on Xiaomi's MiMo RL page: the run itself is the
-- exhibit, and the page's chrome carries its state.
--
-- Written by the workflow's database role only; anon reads. The view follows
-- migration 004's pattern - security_invoker on, explicit revoke before the
-- one grant - because the default it guards against does not stop applying to
-- new objects just because it burned us once already.

begin;

create table if not exists rsi.progress (
  run_id      text primary key,
  phase       text not null,
  detail      jsonb not null default '{}'::jsonb,
  started_at  timestamptz not null,
  updated_at  timestamptz not null default now()
);

alter table rsi.progress enable row level security;
drop policy if exists progress_read on rsi.progress;
create policy progress_read on rsi.progress for select using (true);

create or replace view public.rsi_progress as select * from rsi.progress;
alter view public.rsi_progress set (security_invoker = on);

revoke all on public.rsi_progress from anon, authenticated;
revoke all on rsi.progress from anon, authenticated;
grant select on rsi.progress to anon, authenticated;
grant select on public.rsi_progress to anon, authenticated;

commit;
