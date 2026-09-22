-- More than one topic through the same tables.
--
-- Run this by hand against the project; nothing in the repository applies it.
--
--     psql "$SUPABASE_DB_URL" -f supabase/migrations/008_topics.sql
--
-- The arena began as one question - a Kalshi soccer contract's five-minute
-- move, in cents of a 0-1 price - and every table assumed it. Two more loops
-- now run the same machinery on two more questions: a US stock or ETF's price
-- five minutes after a news item, and BTC/ETH/SOL spot five minutes on, both
-- in basis points relative to the mid at the time. `rsi.runs` already carries
-- `topic`; everything hanging off it did not, and the live and progress
-- tables had no way to say which loop wrote them.
--
-- What changes, and the one rule that keeps every existing number the same:
--
--   * `rsi.rollouts` gains `topic` (backfilled from its run) and `unit`
--     ('cents' | 'bps', backfilled 'cents'). Prices stay prices in both units;
--     `err`, `naive_error` and `skill` are in the topic's unit, which is how
--     the benchmark floor - one tick - can differ per topic without a second
--     formula: 0.01 of price for cents, 5 for basis points.
--   * `rsi.live_forecasts` gains `topic`, `symbol`, `venue`, `context`, `unit`.
--     `league` and `game_id` stay nullable; the new topics have neither.
--   * `rsi.progress` gains `topic`, so the header's dot reads one loop.
--   * `rsi.guesses` gains `topic`; one open guess per (voter, topic), and
--     `rsi_cast_guess` gains an overload that names the topic. The old
--     signature stays and means Kalshi, so a page published before this
--     migration keeps working.
--   * `rsi.book_snapshots` is new: the order book a live forecast was made
--     against, keyed (topic, symbol, at). Read-only from the API.
--   * `rsi_cast_vote` floors the benchmark at the rollout's own tick instead
--     of a hard-coded 0.01, so a vote on a crypto generation pools the way the
--     site does.
--
-- Every view this touches is re-created with migration 004's recipe, because
-- the default it guards against does not stop applying to new objects: create
-- or replace, security_invoker on, revoke all from anon and authenticated on
-- the view and the table beneath it, grant select, and the default-privilege
-- revokes restated. A view's column list is fixed at creation, so a table that
-- grows a column is invisible through its view until the view is replaced.

begin;

-- ---------------------------------------------------------------------------
-- 1. Rollouts: which topic, which unit.

alter table rsi.rollouts add column if not exists topic text;
alter table rsi.rollouts add column if not exists unit  text;

-- Every row published before this migration is Kalshi's: its run says so.
update rsi.rollouts r
   set topic = ru.topic
  from rsi.runs ru
 where r.run_id = ru.id
   and r.topic is null;
update rsi.rollouts set unit = 'cents' where unit is null;

alter table rsi.rollouts alter column unit set default 'cents';
alter table rsi.rollouts drop constraint if exists rollouts_unit_check;
alter table rsi.rollouts add constraint rollouts_unit_check
  check (unit in ('cents', 'bps'));

comment on column rsi.rollouts.topic is
  'The topic of the run this window belongs to. Backfilled from rsi.runs by 008.';
comment on column rsi.rollouts.unit is
  'The unit err, naive_error and skill are in: cents (price units of a 0-1 '
  'contract, tick 0.01) or bps (basis points of a relative move, tick 5). '
  'mid_now, realised, predicted and half_width are prices in both.';

create index if not exists rollouts_topic_run on rsi.rollouts (topic, run_id, side, split);
create index if not exists runs_topic_created on rsi.runs (topic, created desc);

-- ---------------------------------------------------------------------------
-- 2. Live forecasts: which topic, what it was on, what it saw.

alter table rsi.live_forecasts
  add column if not exists topic   text not null default 'kalshi-horizon-5m';
alter table rsi.live_forecasts add column if not exists symbol  text;
alter table rsi.live_forecasts add column if not exists venue   text;
alter table rsi.live_forecasts add column if not exists context jsonb;
alter table rsi.live_forecasts
  add column if not exists unit    text not null default 'cents';
alter table rsi.live_forecasts drop constraint if exists live_forecasts_unit_check;
alter table rsi.live_forecasts add constraint live_forecasts_unit_check
  check (unit in ('cents', 'bps'));

comment on column rsi.live_forecasts.symbol is
  'What the forecast was on, for topics that have no Kalshi ticker: a stock, an ETF, a coin.';
comment on column rsi.live_forecasts.venue is
  'Where the quote came from - the exchange or feed - for topics that are not Kalshi.';
comment on column rsi.live_forecasts.context is
  'What the harness was shown besides the price: the news item, the book, the tape. '
  'The Kalshi game state stays in `game`.';

create index if not exists live_forecasts_topic_at on rsi.live_forecasts (topic, at desc);
create index if not exists live_forecasts_symbol   on rsi.live_forecasts (topic, symbol, at desc);

-- ---------------------------------------------------------------------------
-- 3. Progress: which loop is heartbeating.

alter table rsi.progress
  add column if not exists topic text not null default 'kalshi-horizon-5m';
create index if not exists progress_topic_updated on rsi.progress (topic, updated_at desc);

-- ---------------------------------------------------------------------------
-- 4. Guesses: one open guess per voter per topic.

alter table rsi.guesses
  add column if not exists topic text not null default 'kalshi-horizon-5m';

-- The one-open-guess rule was per voter; it is per (voter, topic) now, so a
-- reader can say "yes" about the crypto loop and "no" about the soccer one.
drop index if exists rsi.guesses_one_open;
create unique index if not exists guesses_one_open on rsi.guesses (voter, topic);
create index if not exists guesses_topic_created on rsi.guesses (topic, created desc);

-- The overload that names the topic. Same validation as 006's; the crowd it
-- returns is the topic's crowd, because a yes about crypto says nothing about
-- soccer.
create or replace function public.rsi_cast_guess(
  guess boolean, voter text, topic text
) returns jsonb
language plpgsql security definer
set search_path = pg_catalog, public
as $$
#variable_conflict use_column
declare
  v_guess boolean := $1;
  v_voter text    := $2;
  v_topic text    := $3;
begin
  if v_voter is null or length(v_voter) < 4 or length(v_voter) > 64 then
    raise exception 'voter id out of range' using errcode = '22023';
  end if;
  -- Shaped like a topic id, and nothing more: a guess about a loop whose first
  -- generation has not landed yet is still a forecast, and the page only ever
  -- reads the topics it knows, so a stray id costs one row and misleads nobody.
  if v_topic is null or v_topic !~ '^[a-z0-9][a-z0-9-]{0,63}$' then
    raise exception 'topic must be a short lowercase id' using errcode = '22023';
  end if;
  insert into rsi.guesses (guess, voter, topic) values (v_guess, v_voter, v_topic)
  on conflict (voter, topic) do update set guess = excluded.guess, created = now();
  return jsonb_build_object('stored', true, 'topic', v_topic,
    'crowd', (select jsonb_build_object(
       'yes', count(*) filter (where guess), 'no', count(*) filter (where not guess))
     from rsi.guesses where topic = v_topic));
end $$;

-- The old signature, kept: it means Kalshi. Re-created rather than left,
-- because its `on conflict (voter)` named a unique index that no longer exists.
create or replace function public.rsi_cast_guess(
  guess boolean, voter text
) returns jsonb
language sql security definer
set search_path = pg_catalog, public
as $$
  select public.rsi_cast_guess($1, $2, 'kalshi-horizon-5m');
$$;

revoke all on function public.rsi_cast_guess(boolean, text)       from public;
revoke all on function public.rsi_cast_guess(boolean, text, text) from public;
grant execute on function public.rsi_cast_guess(boolean, text)       to anon, authenticated;
grant execute on function public.rsi_cast_guess(boolean, text, text) to anon, authenticated;

-- ---------------------------------------------------------------------------
-- 5. Book snapshots: what the market looked like when a live forecast was made.
--
-- Written by the collectors' database role; anon reads. One row per
-- (topic, symbol, instant) so a re-run of a sweep upserts rather than errors.

create table if not exists rsi.book_snapshots (
  topic   text        not null,
  symbol  text        not null,
  at      timestamptz not null,
  book    jsonb       not null,
  primary key (topic, symbol, at)
);

comment on table rsi.book_snapshots is
  'The order book (or quote) a live forecast was made against, keyed by topic, '
  'symbol and the instant it was taken. Read-only from the API.';

alter table rsi.book_snapshots enable row level security;
drop policy if exists book_snapshots_read on rsi.book_snapshots;
create policy book_snapshots_read on rsi.book_snapshots for select using (true);

-- ---------------------------------------------------------------------------
-- 6. The vote pools at the rollout's own tick.
--
-- 007's function floored every window's benchmark at 0.01, which is one cent
-- of a 0-1 price and is nothing at all in basis points: a crypto fixture
-- pooled that way would divide by an unfloored sum and a dead window would
-- score a full point again. The tick now comes from the row's `unit`.

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
  -- its own pooled figure by arriving. The tick rides along per window: one
  -- cent of price for 'cents', five basis points for 'bps' - the same floors
  -- web/topics.js applies, so the reveal matches the page.
  with paired as (
    select r.ticker,
           r.at,
           max(case when r.unit = 'bps' then 5.0 else 0.01 end) as tick,
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
       -- database computes what the site means.
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
  select sum(b_naive - b_err) / nullif(sum(greatest(b_naive, tick)), 0),
         sum(c_naive - c_err) / nullif(sum(greatest(c_naive, tick)), 0),
         count(*),
         count(*) filter (where b_naive < tick / 100)
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
  'here from scored forecasts only - refusals excluded, the benchmark floored at '
  'the rollout''s own tick (0.01 for cents, 5 for bps) - as everywhere else. '
  'The only write besides rsi_flag_trace and rsi_cast_guess the anon key can make.';

revoke all on function public.rsi_cast_vote(text, text, text, text, text, text) from public;
grant execute on function public.rsi_cast_vote(text, text, text, text, text, text)
  to anon, authenticated;

-- ---------------------------------------------------------------------------
-- 7. The views, re-created the 004 way.
--
-- Step 1: create or replace. A replaced `select *` view picks up the columns
-- the table grew, appended at the end, which is what `create or replace`
-- allows.

create or replace view public.rsi_rollouts       as select * from rsi.rollouts;
create or replace view public.rsi_live_forecasts as select * from rsi.live_forecasts;
create or replace view public.rsi_progress       as select * from rsi.progress;
create or replace view public.rsi_guesses        as select * from rsi.guesses;
create or replace view public.rsi_book_snapshots as select * from rsi.book_snapshots;

-- Step 2: run as the caller, so the policies on rsi.* actually apply.
alter view public.rsi_rollouts       set (security_invoker = on);
alter view public.rsi_live_forecasts set (security_invoker = on);
alter view public.rsi_progress       set (security_invoker = on);
alter view public.rsi_guesses        set (security_invoker = on);
alter view public.rsi_book_snapshots set (security_invoker = on);

-- Step 3: take back everything the default handed out, view and table alike.
revoke all on public.rsi_rollouts, public.rsi_live_forecasts, public.rsi_progress,
              public.rsi_guesses, public.rsi_book_snapshots
  from anon, authenticated;
revoke all on rsi.rollouts, rsi.live_forecasts, rsi.progress, rsi.guesses,
              rsi.book_snapshots
  from anon, authenticated;

-- Step 4: give back only reading.
grant usage on schema rsi to anon, authenticated;
grant select on rsi.rollouts, rsi.live_forecasts, rsi.progress, rsi.guesses,
                rsi.book_snapshots
  to anon, authenticated;
grant select on public.rsi_rollouts, public.rsi_live_forecasts, public.rsi_progress,
                public.rsi_guesses, public.rsi_book_snapshots
  to anon, authenticated;

-- Step 5: and keep the default from coming back the next time a view is replaced.
alter default privileges in schema public revoke all on tables from anon, authenticated;
alter default privileges in schema rsi    revoke all on tables from anon, authenticated;

commit;
