-- Votes the database computes for itself, and a table for live forecasts.
--
-- Run this by hand against the project; nothing in the repository applies it.
--
--     psql "$SUPABASE_DB_URL" -f supabase/migrations/002_votes_rpc.sql
--
-- `schema.sql` is left as written. It is the record of what the first shape
-- was, and rewriting it in place would make the diff that fixed this invisible.
--
-- ---------------------------------------------------------------------------
-- Why the votes half exists
--
-- `baseline_skill` and `candidate_skill` are the two numbers the entire "how
-- often does the crowd disagree with the arithmetic" question pivots on, and
-- the browser was sending them. The insert policy was `with check (true)`, the
-- columns were unvalidated, there was no unique constraint, and the page threw
-- the insert result away in a bare `catch {}` — so a rejected vote and a
-- recorded vote showed the reader exactly the same thank-you.
--
-- Anyone could have posted a vote saying the incumbent scored +0.9. Nothing
-- would have rejected it, nothing would have flagged it, and the analysis it
-- fed would have been wrong in a way nobody could later separate from signal.
--
-- After this migration the anon key cannot insert into the table at all. It can
-- call one function, which computes both skills from `rsi.rollouts` the way
-- `topics/kalshi_horizon/score.py:pooled_skill` computes them — sum the error
-- removed, sum the benchmark floored at a tick, then divide — and returns what
-- it stored, so the page can show the reader the answer it just earned rather
-- than the answer it already had in hand.

begin;

-- ---------------------------------------------------------------------------
-- 1. The votes table, tightened.

-- Votes cast before this migration carry whatever the browser posted. They are
-- kept — they are still a record of what a person preferred — but they are
-- marked, because the skills beside them are the very thing under test.
alter table rsi.votes
  add column if not exists server_computed boolean not null default false;

comment on column rsi.votes.server_computed is
  'True when baseline_skill and candidate_skill were computed by rsi_cast_vote '
  'rather than sent by a browser. False rows predate 002_votes_rpc.sql.';

-- One vote per browser per match per generation. Duplicates are dropped
-- oldest-wins first, because the first reading is the one that was not
-- informed by having already seen the answer.
delete from rsi.votes a
      using rsi.votes b
      where a.voter = b.voter
        and a.run_id = b.run_id
        and a.fixture = b.fixture
        and a.id > b.id;

create unique index if not exists votes_one_per_voter
  on rsi.votes (voter, run_id, fixture);

-- ---------------------------------------------------------------------------
-- 2. The only write the anon key can make.

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
declare
  -- Copied out of the parameters before any statement touches a table: the
  -- parameters are named for PostgREST's benefit and collide with the columns
  -- of both `rsi.votes` and `rsi.rollouts`.
  v_run     text := run_id;
  v_fixture text := fixture;
  v_chose   text := chose;
  v_left    text := left_side;
  v_voter   text := voter;
  v_note    text := note;
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

-- ---------------------------------------------------------------------------
-- 3. Take the direct insert away.

drop policy if exists votes_cast on rsi.votes;
revoke insert on rsi.votes from anon, authenticated;
revoke insert on public.rsi_votes from anon, authenticated;
revoke usage, select on sequence rsi.votes_id_seq from anon, authenticated;

revoke all on function public.rsi_cast_vote(text, text, text, text, text, text) from public;
grant execute on function public.rsi_cast_vote(text, text, text, text, text, text)
  to anon, authenticated;

-- ---------------------------------------------------------------------------
-- 4. Live forecasts.
--
-- `scripts/collect_live.py` puts the harness on markets being played now and
-- comes back five minutes later to see what printed. This is the collector
-- author's table, taken as written: the publisher upserts on (ticker, at), so
-- that pair has to be the primary key or every re-run of a sweep errors.
--
-- Deliberately narrower than `rsi.rollouts`. What the page needs and this does
-- not store — the quote the forecast implied, the error it made, the error
-- no-change would have made — is all recoverable from `mid_now`, `realised` and
-- `output.delta_cents` by the same arithmetic `score.py:quote_from` uses, so
-- the reader derives it rather than the collector storing it twice and the two
-- drifting apart.

-- Forecasts on markets that were still trading, from scripts/collect_live.py.
--
-- Not a rollout: nothing here feeds the gate, the split does not apply, and there
-- is no run to hang it off. It is keyed by the contract and the instant it was
-- asked, which is the only uniqueness the collector can promise.
create table if not exists rsi.live_forecasts (
  at           timestamptz not null,      -- when the harness was asked
  league       text,
  game_id      text,                      -- the fixture, from the linker
  ticker       text not null,
  mid_now      double precision,          -- the mid it was given
  realised     double precision,          -- the mid five minutes later, when one printed
  harness      text,
  output       jsonb,                     -- the forecast, with driver and falsifier
  game         jsonb,                     -- match state at that instant
  spans        jsonb,                     -- trimmed trace: tool calls and the model turn
  skill        double precision,
  scored       boolean,                   -- false when the horizon printed no two-sided quote
  ok           boolean,
  error_text   text,                      -- the run's error, or why it went unscored
  primary key (ticker, at)
);

create index if not exists live_forecasts_at   on rsi.live_forecasts (at desc);
create index if not exists live_forecasts_game on rsi.live_forecasts (game_id, at);

alter table rsi.live_forecasts enable row level security;
drop policy if exists live_forecasts_read on rsi.live_forecasts;
create policy live_forecasts_read on rsi.live_forecasts for select using (true);

create or replace view public.rsi_live_forecasts as select * from rsi.live_forecasts;
grant select on public.rsi_live_forecasts to anon, authenticated;

-- ---------------------------------------------------------------------------
-- 5. Two columns the loop grew after the tables were written.

-- The confirmation pass. `cli.py` writes `gen.audit` when a promotion has to be
-- checked against the frozen audit set, and `rsi.runs` had nowhere to put it,
-- so `publish_runs.py` was dropping it on the floor.
alter table rsi.runs add column if not exists audit jsonb;

-- `public.rsi_runs` fixed its column list when it was created, so a new column
-- on the table is invisible through the view until the view is replaced.
create or replace view public.rsi_runs as select * from rsi.runs;
grant select on public.rsi_runs to anon, authenticated;

-- The audit set is a third split, and the check constraint only knew two.
-- `publish_runs.py` writes `baseline.audit.json` and `candidate.audit.json`
-- rows with split='audit', and every one of them would have been rejected.
alter table rsi.rollouts drop constraint if exists rollouts_split_check;
alter table rsi.rollouts add constraint rollouts_split_check
  check (split in ('train', 'holdout', 'audit'));

commit;
