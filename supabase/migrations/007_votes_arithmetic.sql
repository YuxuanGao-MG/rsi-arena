-- The vote's arithmetic matches the site's, and its promise becomes true.
--
-- Two findings from a fresh-eyes review of the redesigned reader.
--
-- rsi_cast_vote pooled budget refusals: rows a starved run stored with
-- err = naive_error = 0 counted as forecasts, so a vote on generation 5
-- returned skills that were three-quarters refusal - presented, with a
-- straight face, as "computed by the database rather than by this page".
--
-- And the page says a vote's note "goes to the team, not published", which was
-- false at the API level: the public view exposed the column to anyone with
-- the page's own key. The view now omits it; notes live only in rsi.votes,
-- which anon cannot read directly.

begin;

create or replace function public.rsi_cast_vote(
  run_id    text,
  fixture   text,
  chose     text,
  left_side text,
  voter     text default null,
  note      text default null
) returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
#variable_conflict use_column
declare
  -- Copied out of the parameters before any statement touches a table: the
  -- parameters are named for PostgREST's benefit and collide with the columns
  -- of both `rsi.votes` and `rsi.rollouts`.
  v_run     text := $1;
  v_fixture text := $2;
  v_chose   text := $3;
  v_left    text := $4;
  v_voter   text := $5;
  v_note    text := $6;
  v_base    double precision;
  v_cand    double precision;
  v_windows integer;
  v_quiet   integer;
  v_id      bigint;
begin
  if v_chose not in ('baseline', 'candidate', 'neither') then
    raise exception 'chose must be baseline, candidate or neither, not %', v_chose
      using errcode = '22023';
  end if;
  if v_left not in ('baseline', 'candidate') then
    raise exception 'left_side must be baseline or candidate, not %', v_left
      using errcode = '22023';
  end if;
  if v_voter is null or length(v_voter) < 4 or length(v_voter) > 64 then
    raise exception 'voter id out of range' using errcode = '22023';
  end if;
  if v_note is not null and length(v_note) > 280 then
    raise exception 'a note may be at most 280 characters' using errcode = '22023';
  end if;

  -- Pair on (ticker, at), which is what makes the two numbers comparable: a
  -- side that was scored on a window the other never saw would otherwise move
  -- its own pooled figure by arriving.
  with paired as (
    select r.ticker,
           r.at,
           max(r.err)          filter (where r.side = 'baseline')  as b_err,
           max(r.naive_error)  filter (where r.side = 'baseline')  as b_naive,
           max(r.err)          filter (where r.side = 'candidate') as c_err,
           max(r.naive_error)  filter (where r.side = 'candidate') as c_naive
      from rsi.rollouts r
     where r.run_id = v_run
       and r.fixture = v_fixture
       -- A refusal is a window the harness never answered. gen5 stored 715 of
       -- them with err = naive_error = 0, so pooled naively they read as
       -- confident echoes and pulled both skills toward zero - while every
       -- page of the site carefully excluded them and told the visitor the
       -- reveal was "computed by the database rather than by this page". The
       -- database now computes what the site means.
       and coalesce(r.ok, true)
       and coalesce(r.scored, true)
     group by r.ticker, r.at
    having count(*) filter (where r.side = 'baseline')  > 0
       and count(*) filter (where r.side = 'candidate') > 0
  ), scored as (
    select * from paired
     where b_err is not null and c_err is not null
       and b_naive is not null and c_naive is not null
  )
  select sum(b_naive - b_err) / nullif(sum(greatest(b_naive, 0.01)), 0),
         sum(c_naive - c_err) / nullif(sum(greatest(c_naive, 0.01)), 0),
         count(*),
         count(*) filter (where b_naive < 0.0001)
    into v_base, v_cand, v_windows, v_quiet
    from scored;

  if coalesce(v_windows, 0) = 0 then
    raise exception 'no paired scored windows for % on %', v_fixture, v_run
      using errcode = '22023';
  end if;

  insert into rsi.votes (run_id, fixture, chose, left_side,
                         baseline_skill, candidate_skill, voter, note, server_computed)
  values (v_run, v_fixture, v_chose, v_left,
          round(v_base::numeric, 6), round(v_cand::numeric, 6), v_voter, v_note, true)
  on conflict (voter, run_id, fixture) do nothing
  returning id into v_id;

  return jsonb_build_object(
    'stored',          v_id is not null,
    'already_voted',   v_id is null,
    'vote_id',         v_id,
    'baseline_skill',  round(v_base::numeric, 4),
    'candidate_skill', round(v_cand::numeric, 4),
    'windows',         v_windows,
    'quiet',           v_quiet
  );
end;
$$;



comment on function public.rsi_cast_vote(text, text, text, text, text, text) is
  'Record one vote and return the pooled held-out skill of both sides, computed '
  'here from scored forecasts only - refusals excluded, as everywhere else. '
  'The only write besides rsi_flag_trace and rsi_cast_guess the anon key can make.';

revoke all on function public.rsi_cast_vote(text, text, text, text, text, text) from public;
grant execute on function public.rsi_cast_vote(text, text, text, text, text, text)
  to anon, authenticated;

-- The note leaves the public surface. Recreate rather than alter: a view's
-- column list is fixed at creation.
drop view public.rsi_votes;
create view public.rsi_votes as
  select id, created, run_id, fixture, chose, left_side,
         baseline_skill, candidate_skill, voter, server_computed
    from rsi.votes;
alter view public.rsi_votes set (security_invoker = on);
revoke all on public.rsi_votes from anon, authenticated;
grant select on public.rsi_votes to anon, authenticated;

commit;
