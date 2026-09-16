-- rsi-arena's run records, in their own schema.
--
-- The Supabase project is shared with another arena, so everything here lives
-- under `rsi` rather than `public`. A run is one generation; a rollout is one
-- scored window of one harness; a trace is what the harness actually did to
-- produce it. Traces are a separate table because they are two orders larger
-- than the row they hang off, and a listing should not have to drag them.

create schema if not exists rsi;

create table if not exists rsi.runs (
  id               text primary key,          -- the run directory's name
  topic            text not null,
  created          timestamptz not null default now(),
  parent           text,
  incumbent        text,
  incumbent_fp     text,
  candidate_fp     text,
  accepted         boolean not null default false,
  reasons          text[],
  -- Whole scoreboards, kept as written rather than flattened: the shape is the
  -- topic's, and a viewer that guesses at it goes stale the moment a topic adds
  -- a number.
  baseline         jsonb,
  candidate        jsonb,
  decision         jsonb,
  search           jsonb,
  llm              jsonb,
  split            jsonb
);

create table if not exists rsi.rollouts (
  id               bigserial primary key,
  run_id           text not null references rsi.runs(id) on delete cascade,
  side             text not null check (side in ('baseline', 'candidate')),
  split            text not null check (split in ('train', 'holdout')),
  fixture          text not null,             -- the match; the unit the split respects
  ticker           text not null,
  at               timestamptz not null,
  mid_now          double precision,
  realised         double precision,
  predicted        double precision,
  half_width       double precision,
  err              double precision,
  naive_error      double precision,
  skill            double precision,
  echoed           boolean,
  unmeasurable     boolean,
  scored           boolean,
  cost_usd         double precision,
  ok               boolean,
  error_text       text,
  output           jsonb,                     -- the forecast, with driver and falsifier
  game             jsonb,
  feedback         text,
  unique (run_id, side, ticker, at)
);

create index if not exists rollouts_run_side on rsi.rollouts (run_id, side, split);
create index if not exists rollouts_fixture  on rsi.rollouts (run_id, fixture);
-- Worst-first is the useful order: a rewriter and a reader both want the
-- windows the harness lost, not the ones it drew.
create index if not exists rollouts_skill    on rsi.rollouts (run_id, skill);

create table if not exists rsi.traces (
  rollout_id       bigint primary key references rsi.rollouts(id) on delete cascade,
  spans            jsonb not null
);

-- The frontend reads with the anon key and writes nothing.
alter table rsi.runs     enable row level security;
alter table rsi.rollouts enable row level security;
alter table rsi.traces   enable row level security;

drop policy if exists runs_read     on rsi.runs;
drop policy if exists rollouts_read on rsi.rollouts;
drop policy if exists traces_read   on rsi.traces;
create policy runs_read     on rsi.runs     for select using (true);
create policy rollouts_read on rsi.rollouts for select using (true);
create policy traces_read   on rsi.traces   for select using (true);

grant usage on schema rsi to anon, authenticated;
grant select on all tables in schema rsi to anon, authenticated;
alter default privileges in schema rsi grant select on tables to anon, authenticated;

-- The API surface.
--
-- PostgREST only exposes `public` unless the project's API settings name another
-- schema, and this project is shared with another arena — changing its exposed
-- schemas would change that arena's API too. Views in `public` reach the same
-- rows without touching anything anyone else depends on, and the `rsi_` prefix
-- keeps them from colliding with the fifty tables already there.

create or replace view public.rsi_runs as select * from rsi.runs;
create or replace view public.rsi_rollouts as select * from rsi.rollouts;
create or replace view public.rsi_traces as select * from rsi.traces;

grant select on public.rsi_runs, public.rsi_rollouts, public.rsi_traces
  to anon, authenticated;

-- Votes on a whole portfolio, not a single forecast.
--
-- One window's forecast is verifiable: five minutes later the price printed and
-- there is an answer. Asking a person which of two is better mostly measures
-- which reads better, and a harness can write a persuasive reason for a bad
-- call — the candidate rejected on 2026-09-15 predicted movement on 27 of 29
-- dead markets and every one of its drivers was plausible on its own.
--
-- A whole match is different. Thirty-four windows show when a harness stays
-- quiet, whether its half-width tracks its uncertainty, and whether its reasons
-- change with the situation or are one argument with the numbers swapped. Those
-- are visible to a reader and invisible in a single row.
--
-- The vote is kept next to the pooled skill of both sides, so the interesting
-- number is not who won but how often the crowd and the arithmetic disagree.

create table if not exists rsi.votes (
  id            bigserial primary key,
  created       timestamptz not null default now(),
  run_id        text not null references rsi.runs(id) on delete cascade,
  fixture       text not null,
  chose         text not null check (chose in ('baseline', 'candidate', 'neither')),
  -- Which side was shown on the left. Recorded because position bias is real
  -- and a vote nobody can correct for is a vote nobody can use.
  left_side     text not null check (left_side in ('baseline', 'candidate')),
  baseline_skill double precision,
  candidate_skill double precision,
  voter         text,                      -- an opaque browser id, not a person
  note          text
);

create index if not exists votes_run on rsi.votes (run_id, fixture);

alter table rsi.votes enable row level security;
drop policy if exists votes_read on rsi.votes;
drop policy if exists votes_cast on rsi.votes;
create policy votes_read on rsi.votes for select using (true);
-- Visitors vote; that is the point. They cannot read a vote back out and change
-- it, and they cannot touch anything else.
create policy votes_cast on rsi.votes for insert with check (true);

create or replace view public.rsi_votes as select * from rsi.votes;
grant select, insert on public.rsi_votes to anon, authenticated;
grant usage, select on sequence rsi.votes_id_seq to anon, authenticated;
