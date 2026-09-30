-- The pooled statistic, computed once instead of per page load, and floored at
-- each topic's own tick.
--
-- Migration 011 moved the sum into the database, which was the right move and
-- not enough. A plain view is a stored query: every request still read every
-- held-out rollout of the topic to add about forty numbers, and kalshi's 87,380
-- of them still took 2.7 seconds against a three-second statement timeout. The
-- overview kept answering 500 for the largest topic and 206 for the others,
-- which looked like an intermittent fault and was really a row count.
--
-- A run's rollouts do not change after they are published, so the sum is a fact
-- from the moment they are written rather than something to recompute on
-- demand. Materialised, it is read in 0.03 seconds. `scripts/publish_runs.py`
-- refreshes it after each publish, concurrently, which is what the unique index
-- below is for: a refresh must never block a reader mid-page. The watchdog
-- checks hourly that every run with rollouts has a row here, so a refresh that
-- silently stops does not quietly serve a stale generation.
--
-- The second change is the tick. 011 floored every basis-point row at 5, which
-- is the news topic's tick; crypto's is 2, and `web/topics.js` gives each topic
-- its own. The view has the `topic` column, so it can simply use the right one,
-- and the discrepancy 011 recorded as a known limitation is gone rather than
-- documented. `tests/test_pooled_view.py` holds this SQL and
-- `web/stats.js::pooled` to the same answers on the same rows, including the
-- ticks.
--
--     removed   = sum(naive_error - err)
--     benchmark = sum(greatest(naive_error, tick))
--     skill     = removed / benchmark
--     quiet     = count(naive_error < tick / 100)
--
-- Refusals (`not ok`, or `not scored`) are excluded exactly as `refused()`
-- excludes them, and counted, so the reader can still say how much of an
-- evaluation was silence.
begin;

drop view if exists public.rsi_run_side_stats;

-- Whichever kind is there. `drop view if exists` does not skip a materialized
-- view, it raises "is not a view", so a migration that guesses fails on exactly
-- one of the two databases it has to work on: this one, where 011 left a plain
-- view, and a fresh one built from 012 onwards.
do $$
begin
  if exists (select 1 from pg_matviews
              where schemaname = 'rsi' and matviewname = 'run_side_stats') then
    drop materialized view rsi.run_side_stats;
  elsif exists (select 1 from pg_views
                 where schemaname = 'rsi' and viewname = 'run_side_stats') then
    drop view rsi.run_side_stats;
  end if;
end $$;

create materialized view rsi.run_side_stats as
  with r as (
    select rollouts.*,
           -- One tick, the topic's own, mirroring `web/topics.js::topicOf`:
           -- a row carries its topic since 008, and a row without one falls
           -- back on its unit the way the browser does.
           case when rollouts.topic = 'news-equity-5m'    then 5.0
                when rollouts.topic = 'crypto-horizon-1m' then 2.0
                when rollouts.unit = 'bps'                then 2.0
                else 0.01 end as tick,
           (coalesce(rollouts.ok, true) and coalesce(rollouts.scored, true)) as kept
      from rsi.rollouts
  )
  select r.topic,
         r.run_id,
         r.side,
         r.split,
         count(*)                                                   as rows_total,
         count(*) filter (where r.kept and r.err is not null
                            and r.naive_error is not null)          as scored,
         count(*) filter (where not r.kept)                         as refusals,
         count(*) filter (where r.kept and r.err is not null
                            and r.naive_error is not null
                            and r.naive_error < r.tick / 100)       as quiet,
         -- Zero, not null, when a generation scored nothing at all: the
         -- JavaScript starts both accumulators at 0 and a group of pure
         -- refusals leaves them there. `sum() filter` returns null instead, and
         -- while the reader's `Number(null)` happens to be 0, a statistic that
         -- agrees by coincidence is one the next reader of it cannot trust.
         coalesce(sum(r.naive_error - r.err) filter (where r.kept and r.err is not null
                                                       and r.naive_error is not null), 0)
                                                                    as removed,
         coalesce(sum(greatest(r.naive_error, r.tick)) filter (where r.kept
                                                                 and r.err is not null
                                                                 and r.naive_error is not null), 0)
                                                                    as benchmark
    from r
   group by r.topic, r.run_id, r.side, r.split;

comment on materialized view rsi.run_side_stats is
  'Pooled skill per run, side and split, summed once where the rows are and '
  'refreshed by scripts/publish_runs.py. Mirrors web/stats.js::pooled; '
  'tests/test_pooled_view.py holds the two to the same answers.';

-- Required by `refresh materialized view concurrently`, and the key the reader
-- filters on.
create unique index run_side_stats_key
  on rsi.run_side_stats (topic, run_id, side, split);

create or replace view public.rsi_run_side_stats as
  select * from rsi.run_side_stats;

alter view public.rsi_run_side_stats set (security_invoker = on);

revoke all on public.rsi_run_side_stats from anon, authenticated;
revoke all on rsi.run_side_stats from anon, authenticated;
grant usage on schema rsi to anon, authenticated;
grant select on rsi.run_side_stats to anon, authenticated;
grant select on public.rsi_run_side_stats to anon, authenticated;

alter default privileges in schema public revoke all on tables from anon, authenticated;
alter default privileges in schema rsi revoke all on tables from anon, authenticated;

commit;
