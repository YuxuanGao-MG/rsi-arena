-- The vote function could not be called.
--
-- `on conflict (voter, run_id, fixture)` resolves its column names against the
-- target table, and every one of them is also a parameter name — PostgREST maps
-- the JSON body to parameters by name, so they have to be called that. Postgres
-- refused with 42702, "column reference voter is ambiguous", on the first real
-- call. The function had gone in untested because the session that wrote it had
-- no database credentials; it was applied and exercised in the same minute, and
-- this is what exercising it found.
--
-- Two changes, both about name resolution and nothing else. `use_column` makes
-- a bare name inside a statement mean the column, which is what `on conflict`
-- needs. The locals are then read from `$1`..`$6`, which always mean the
-- parameters whatever the conflict rule says, so the values the caller sent are
-- still the values that get stored.

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
  'here rather than accepted from the caller. The only write the anon key can make.';

revoke all on function public.rsi_cast_vote(text, text, text, text, text, text) from public;
grant execute on function public.rsi_cast_vote(text, text, text, text, text, text)
  to anon, authenticated;

commit;
