-- Two more ways a visitor can talk back, both through validating functions.
--
-- The pattern is 002/004's and it is not optional: every write the anon key can
-- make goes through a security-definer function that validates what it stores,
-- because the one table that accepted direct inserts was storing whatever the
-- browser sent, and the views once granted TRUNCATE to the world.
--
-- What a visitor can now say:
--   1. rsi_flag_trace  - "this reasoning is good / bad / unsure" on one window's
--      driver. Human labels on trajectories, which for a self-improvement
--      project is not decoration - it is exactly the feedback signal the field
--      keeps wishing it had.
--   2. rsi_cast_guess  - "will the next generation be promoted?" A forecast, on
--      a forecasting site, graded automatically when the run lands. The site
--      pools them into a crowd prior beside the gate's verdict.
-- Free-text already exists: rsi_cast_vote's note column. The page will say
-- notes go to the team; they are not broadcast, so there is nothing to moderate.

begin;

create table if not exists rsi.trace_feedback (
  id          bigserial primary key,
  created     timestamptz not null default now(),
  rollout_id  bigint not null references rsi.rollouts(id) on delete cascade,
  verdict     text not null check (verdict in ('good', 'bad', 'unsure')),
  voter       text not null,
  unique (voter, rollout_id)
);

create table if not exists rsi.guesses (
  id          bigserial primary key,
  created     timestamptz not null default now(),
  guess       boolean not null,          -- true: the next generation is promoted
  voter       text not null
);
-- One open guess per voter: a new guess before the next run lands replaces the
-- old one, so nobody hedges by stacking both answers.
create unique index if not exists guesses_one_open
  on rsi.guesses (voter);

create or replace function public.rsi_flag_trace(
  rollout_id bigint, verdict text, voter text
) returns jsonb
language plpgsql security definer
set search_path = pg_catalog, public
as $$
#variable_conflict use_column
declare
  v_rollout bigint := $1;
  v_verdict text   := $2;
  v_voter   text   := $3;
  v_id      bigint;
begin
  if v_verdict not in ('good', 'bad', 'unsure') then
    raise exception 'verdict must be good, bad or unsure' using errcode = '22023';
  end if;
  if v_voter is null or length(v_voter) < 4 or length(v_voter) > 64 then
    raise exception 'voter id out of range' using errcode = '22023';
  end if;
  if not exists (select 1 from rsi.rollouts r where r.id = v_rollout) then
    raise exception 'no such window' using errcode = '22023';
  end if;
  insert into rsi.trace_feedback (rollout_id, verdict, voter)
  values (v_rollout, v_verdict, v_voter)
  on conflict (voter, rollout_id) do update set verdict = excluded.verdict
  returning id into v_id;
  return jsonb_build_object('stored', true, 'id', v_id,
    'tally', (select jsonb_object_agg(t.verdict, t.n) from (
       select verdict, count(*) n from rsi.trace_feedback
       where rollout_id = v_rollout group by verdict) t));
end $$;

create or replace function public.rsi_cast_guess(
  guess boolean, voter text
) returns jsonb
language plpgsql security definer
set search_path = pg_catalog, public
as $$
#variable_conflict use_column
declare
  v_guess boolean := $1;
  v_voter text    := $2;
begin
  if v_voter is null or length(v_voter) < 4 or length(v_voter) > 64 then
    raise exception 'voter id out of range' using errcode = '22023';
  end if;
  insert into rsi.guesses (guess, voter) values (v_guess, v_voter)
  on conflict (voter) do update set guess = excluded.guess, created = now();
  return jsonb_build_object('stored', true,
    'crowd', (select jsonb_build_object(
       'yes', count(*) filter (where guess), 'no', count(*) filter (where not guess))
     from rsi.guesses));
end $$;

-- Read paths, 004-pattern: invoker views, explicit revokes, select only.
alter table rsi.trace_feedback enable row level security;
alter table rsi.guesses enable row level security;
drop policy if exists trace_feedback_read on rsi.trace_feedback;
drop policy if exists guesses_read on rsi.guesses;
create policy trace_feedback_read on rsi.trace_feedback for select using (true);
create policy guesses_read on rsi.guesses for select using (true);

create or replace view public.rsi_trace_feedback as select * from rsi.trace_feedback;
create or replace view public.rsi_guesses as select * from rsi.guesses;
alter view public.rsi_trace_feedback set (security_invoker = on);
alter view public.rsi_guesses set (security_invoker = on);

revoke all on public.rsi_trace_feedback, public.rsi_guesses from anon, authenticated;
revoke all on rsi.trace_feedback, rsi.guesses from anon, authenticated;
grant select on rsi.trace_feedback, rsi.guesses to anon, authenticated;
grant select on public.rsi_trace_feedback, public.rsi_guesses to anon, authenticated;

revoke all on function public.rsi_flag_trace(bigint, text, text) from public;
revoke all on function public.rsi_cast_guess(boolean, text) from public;
grant execute on function public.rsi_flag_trace(bigint, text, text) to anon, authenticated;
grant execute on function public.rsi_cast_guess(boolean, text) to anon, authenticated;

commit;
