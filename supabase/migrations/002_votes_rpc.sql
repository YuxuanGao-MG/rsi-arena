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
-- comes back five minutes later to see what printed. Same columns as a rollout
-- wherever the meaning is the same, because the scoring is the same function
-- and the page draws both with the same code — but no `run_id`, no `side` and
-- no `split`, because a live forecast belongs to no generation and is not part
-- of any comparison. Nothing here feeds the gate.
--
-- The collector writes `runs/live/forecasts.jsonl`; a publisher maps it as:
--
--     at, league, game_id, ticker, mid_now, realised  → same keys
--     harness                                         → row["harness"]
--     skill, err, naive_error                         → row["scored"]["skill"], ["error"], ["naive_error"]
--     scored                                          → row["scored"] is not null
--     unscored_because                                → row["unscored_because"]
--     ok, error_text                                  → row["ok"], row["error"]
--     cost_usd, spans                                 → row["run"]["cost_usd"], row["run"]["trace"]
--     output, game                                    → row["output"], row["game"]
--
-- `predicted` and `half_width` are not in the jsonl; derive them the way
-- `score.py:quote_from` does, from `mid_now` and the forecast's `delta_cents`
-- and `half_width_cents`, so the scatter on the live page is the same plot as
-- the one on a generation's page.

create table if not exists rsi.live_forecasts (
  id               bigserial primary key,
  at               timestamptz not null,
  league           text,
  game_id          text,
  ticker           text not null,
  harness          text,
  mid_now          double precision,
  realised         double precision,
  predicted        double precision,
  half_width       double precision,
  err              double precision,
  naive_error      double precision,
  skill            double precision,
  -- False until the horizon prints. A forecast is never scored against the
  -- price it was handed, and one that never gets a two-sided quote at the
  -- horizon stays unscored with a reason rather than counting as a miss.
  scored           boolean not null default false,
  unscored_because text,
  ok               boolean,
  error_text       text,
  cost_usd         double precision,
  output           jsonb,                    -- the forecast, with driver and falsifier
  game             jsonb,                    -- match state as it was replayed at that instant
  spans            jsonb,                    -- the trace; kept inline, these are single sweeps
  unique (ticker, at)
);

create index if not exists live_at       on rsi.live_forecasts (at desc);
create index if not exists live_game     on rsi.live_forecasts (game_id, at);
create index if not exists live_unscored on rsi.live_forecasts (scored, at) where not scored;

alter table rsi.live_forecasts enable row level security;
drop policy if exists live_read on rsi.live_forecasts;
create policy live_read on rsi.live_forecasts for select using (true);

grant select on rsi.live_forecasts to anon, authenticated;

create or replace view public.rsi_live_forecasts as
  select * from rsi.live_forecasts;
grant select on public.rsi_live_forecasts to anon, authenticated;

commit;
