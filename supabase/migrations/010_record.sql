-- The working record: what a window was quoted at, and what the search mutated.
--
-- Run this by hand against the project; nothing in the repository applies it.
--
--     psql "$SUPABASE_DB_URL" -f supabase/migrations/010_record.sql
--
-- Two things the arena already writes to disk and nobody can query.
--
-- The first is the trade record. Migration 009 published the paper books -
-- the trades, the marks, the running stats - but a book is a ledger and a
-- ledger does not say what was *offered*. The engine records that per cycle
-- and attaches it to the rollout as `details["trade"]`: the quote the harness
-- posted (bid, ask, size, and the mid it was posted off), every fill that
-- crossed it, and what the five-minute path did in between - whether it
-- reached either side of the quote at all. That is the difference between
-- "nobody traded there" and "we could not afford it", and between a harness
-- that quotes too tight and one that quotes badly. `rsi.rollouts` and
-- `rsi.live_forecasts` each gain three columns for it.
--
-- The second is the search itself. Across six Kalshi generations GEPA
-- proposed all four components - in gen6 the model twelve times, the context
-- nine, the plan nine, the tools eight - and only `context` rewrites ever
-- survived their minibatch: every generation's seven filed candidates differ
-- from the seed in `context` alone, and that context has grown from 5,470 to
-- 8,757 characters. Nobody could see that without unpickling GEPA's state by
-- hand, so nobody did, for six generations. `rsi.candidates` and
-- `rsi.candidate_scores` are the fix: one row per candidate with the diff
-- against the generation's seed and the length of its context, and one row per
-- (candidate, instance) score, which is the matrix that says what each loser
-- was uniquely good at.
--
-- Nothing here is backfilled and nothing here is written by the anon key. The
-- new columns are nullable: a rollout published before this migration, or one
-- from a topic with no book, simply has no quote. The publishers probe for the
-- columns and the tables before naming them, so the cron beside this file
-- keeps working on either side of it.
--
-- Every view this touches is re-created with migration 004's recipe, because
-- the default it guards against does not stop applying to new objects: create
-- or replace, security_invoker on, revoke all from anon and authenticated on
-- the view and the table beneath it, grant select, and the default-privilege
-- revokes restated. A view's column list is fixed at creation, so a table that
-- grows a column is invisible through its view until the view is replaced -
-- which is exactly why 008 had to re-create these two.

begin;

-- ---------------------------------------------------------------------------
-- 1. Rollouts: what was quoted, what filled, what the path did.

alter table rsi.rollouts add column if not exists quote jsonb;
alter table rsi.rollouts add column if not exists fills jsonb;
alter table rsi.rollouts add column if not exists path  jsonb;

comment on column rsi.rollouts.quote is
  'The quote the harness posted for this window: bid, ask, size_frac, size_usd, '
  'source, the mid_now it was posted off, and the unit those prices are in. '
  'Null for a window with no book - a topic that does not trade, a run '
  'published before migration 010, a refusal.';
comment on column rsi.rollouts.fills is
  'Every fill that crossed the posted quote, as an array of '
  '{side, px, qty, notional_usd, fees_usd, at, bar_ts, effect}. An empty array '
  'is a quote nobody wanted; null is a window that never posted one.';
comment on column rsi.rollouts.path is
  'What the market did between the instant and the horizon: {bars, high, low, '
  'close, crossed_bid, crossed_ask}. `crossed` is not `filled` - a cap can cut '
  'a side the tape reached down to nothing.';

-- ---------------------------------------------------------------------------
-- 2. Live forecasts: the same three, for a forecast the live book traded.

alter table rsi.live_forecasts add column if not exists quote jsonb;
alter table rsi.live_forecasts add column if not exists fills jsonb;
alter table rsi.live_forecasts add column if not exists path  jsonb;

comment on column rsi.live_forecasts.quote is
  'The quote the live book posted for this forecast, shaped like '
  'rsi.rollouts.quote. Null until the paper trader has attached a record.';
comment on column rsi.live_forecasts.fills is
  'The fills that crossed it, shaped like rsi.rollouts.fills.';
comment on column rsi.live_forecasts.path is
  'What the path did, shaped like rsi.rollouts.path.';

-- ---------------------------------------------------------------------------
-- 3. Candidates: every rewrite a generation's search proposed.
--
-- Keyed by the candidate's index in GEPA's own `program_candidates`, because
-- that is the identity everything else in the state is positional against -
-- the parent, the subscore row, the discovery count - and because two
-- candidates in one search can be byte-identical, so the fingerprint is not a
-- key. The fingerprint is still here: it is what joins a candidate to the
-- archive (`runs/archive*.json`) and to `rsi.runs.candidate_fp`.

create table if not exists rsi.candidates (
  topic                   text not null,
  run_id                  text not null,
  candidate_idx           int  not null,
  fingerprint             text,
  parent_idx              int,
  changed_components      text[],
  accepted                bool,
  valset_mean             double precision,
  objectives              jsonb,
  components              jsonb,
  context_chars           int,
  discovered_after_calls  int,
  created                 timestamptz default now(),
  primary key (topic, run_id, candidate_idx)
);

comment on table rsi.candidates is
  'One row per candidate a generation''s GEPA search proposed and filed, '
  'including the six of seven that lost. Read-only from the API; written by '
  'scripts/publish_runs.py from <run_dir>/gepa/.';
comment on column rsi.candidates.candidate_idx is
  'The candidate''s index in GEPA''s program_candidates. Index 0 is the seed '
  'the search started from, which is not always the incumbent.';
comment on column rsi.candidates.parent_idx is
  'The candidate this one was mutated from, by index. Null for the seed, and '
  'the first parent for a merge, which carries more than one.';
comment on column rsi.candidates.changed_components is
  'Which components differ from the seed candidate (index 0): the column that '
  'answers "what is the search actually mutating". An empty array is a '
  'candidate identical to the seed; the seed''s own row is empty by '
  'definition. Six generations of this array reading {context} and nothing '
  'else is the reason this table exists.';
comment on column rsi.candidates.accepted is
  'Whether this candidate became the incumbent: GEPA chose it as best and the '
  'gate said yes. Not "won its minibatch" - every filed candidate did that.';
comment on column rsi.candidates.valset_mean is
  'The mean of this candidate''s per-instance valset scores, which is the '
  'number GEPA selects on. Null for a candidate the state scored on nothing.';
comment on column rsi.candidates.objectives is
  'The named aggregate objectives GEPA kept for this candidate (skill, cost, '
  'pnl), which are what the hybrid frontier ranks on beside the per-instance '
  'scores.';
comment on column rsi.candidates.components is
  'The candidate itself: the component texts a harness is built from.';
comment on column rsi.candidates.context_chars is
  'length(components->>''context''), so prompt growth is one query away '
  'without pulling every context out of the database to measure it.';
comment on column rsi.candidates.discovered_after_calls is
  'How many metric calls the search had spent when it found this candidate.';

-- Runs come newest-first, one generation at a time; the primary key serves
-- every other read of this table.
create index if not exists candidates_topic_created on rsi.candidates (topic, created desc);

alter table rsi.candidates enable row level security;
drop policy if exists candidates_read on rsi.candidates;
create policy candidates_read on rsi.candidates for select using (true);

-- ---------------------------------------------------------------------------
-- 4. Candidate scores: the candidates-by-instances matrix, one row per cell.
--
-- A valset of 240 windows and seven candidates is 1,680 rows a generation,
-- which is a twentieth of what the rollouts already cost. What it buys is the
-- question the archive's frontier is built on - which candidate was the only
-- thing that ever worked on this window - asked in SQL rather than in pickle.

create table if not exists rsi.candidate_scores (
  topic         text not null,
  run_id        text not null,
  candidate_idx int  not null,
  instance_id   text not null,
  score         double precision,
  primary key (topic, run_id, candidate_idx, instance_id)
);

comment on table rsi.candidate_scores is
  'What each of a generation''s candidates scored on each valset instance, '
  'from GEPA''s prog_candidate_val_subscores. Read-only from the API.';
comment on column rsi.candidate_scores.instance_id is
  'The instance''s own id (<ticker|symbol>@<instant>), not its position in the '
  'valset: the valset moves between generations and positions do not join.';

-- The other direction: one window across every candidate that ever saw it.
create index if not exists candidate_scores_instance
  on rsi.candidate_scores (topic, instance_id, score desc);

alter table rsi.candidate_scores enable row level security;
drop policy if exists candidate_scores_read on rsi.candidate_scores;
create policy candidate_scores_read on rsi.candidate_scores for select using (true);

-- ---------------------------------------------------------------------------
-- 5. The views, created the 004 way.
--
-- Step 1: create or replace. The two existing views are replaced so the three
-- columns their tables just grew become visible through them; a `select *`
-- view picks up columns appended at the end, which is what `create or replace`
-- allows.

create or replace view public.rsi_rollouts         as select * from rsi.rollouts;
create or replace view public.rsi_live_forecasts   as select * from rsi.live_forecasts;
create or replace view public.rsi_candidates       as select * from rsi.candidates;
create or replace view public.rsi_candidate_scores as select * from rsi.candidate_scores;

-- Step 2: run as the caller, so the policies on rsi.* actually apply.
alter view public.rsi_rollouts         set (security_invoker = on);
alter view public.rsi_live_forecasts   set (security_invoker = on);
alter view public.rsi_candidates       set (security_invoker = on);
alter view public.rsi_candidate_scores set (security_invoker = on);

-- Step 3: take back everything the default handed out, view and table alike.
revoke all on public.rsi_rollouts, public.rsi_live_forecasts,
              public.rsi_candidates, public.rsi_candidate_scores
  from anon, authenticated;
revoke all on rsi.rollouts, rsi.live_forecasts, rsi.candidates, rsi.candidate_scores
  from anon, authenticated;

-- Step 4: give back only reading.
grant usage on schema rsi to anon, authenticated;
grant select on rsi.rollouts, rsi.live_forecasts, rsi.candidates, rsi.candidate_scores
  to anon, authenticated;
grant select on public.rsi_rollouts, public.rsi_live_forecasts,
                public.rsi_candidates, public.rsi_candidate_scores
  to anon, authenticated;

-- Step 5: and keep the default from coming back the next time a view is replaced.
alter default privileges in schema public revoke all on tables from anon, authenticated;
alter default privileges in schema rsi    revoke all on tables from anon, authenticated;

commit;
