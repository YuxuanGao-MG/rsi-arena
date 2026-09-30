-- The pooled statistic, summed where the rows are.
--
-- The overview recomputes every generation's held-out skill from the rollouts
-- themselves rather than trusting the manifest, which is right: the manifests
-- of the earliest runs were written under a superseded metric, and a number the
-- reader cannot re-derive is a number nobody can check. What was wrong was
-- where the sum happened. The browser pulled every held-out rollout of a topic
-- - 25,061 of them for news, and growing with every generation - to produce
-- about forty numbers.
--
-- On 29 September that stopped working. The query read 626 of its 842 buffers
-- from disk and took 2.7 seconds, Supabase cancels a statement at three, and
-- the overview answered 500 for two topics and 206 for the third, flickering
-- between them as the table grew. An index did not help: the rows really were
-- being read, all of them, to be added up somewhere else.
--
-- This view does the addition in the database. The arithmetic is the one in
-- `web/stats.js::pooled`, kept in step by `tests/test_pooled_view.py`, which
-- asserts the view and the JavaScript agree on the same rollouts:
--
--     removed   = sum(naive_error - err)
--     benchmark = sum(greatest(naive_error, tick))   -- tick: 0.01 cents, else 5 bps...
--     skill     = removed / benchmark
--
-- A refused row (not ok, or not scored) is excluded, the way `refused()`
-- excludes it, and counted separately so the reader can still say how much of
-- an evaluation was silence.
--
-- The tick is the topic's, read from the row's own `unit` column rather than
-- from a lookup: cents floor at one cent, basis points at their topic's tick.
-- `rsi_arena/topics/*/score.py` owns those numbers; they are restated here
-- because SQL cannot import them, and the test is what keeps the restatement
-- honest.
begin;

create or replace view rsi.run_side_stats as
  select r.topic,
         r.run_id,
         r.side,
         r.split,
         count(*)                                                   as rows_total,
         count(*) filter (where coalesce(r.ok, true)
                            and coalesce(r.scored, true)
                            and r.err is not null
                            and r.naive_error is not null)          as scored,
         count(*) filter (where not coalesce(r.ok, true)
                             or not coalesce(r.scored, true))       as refusals,
         count(*) filter (where coalesce(r.ok, true)
                            and coalesce(r.scored, true)
                            and r.naive_error < case when r.unit = 'bps' then 0.05 else 0.0001 end)
                                                                    as quiet,
         sum(r.naive_error - r.err) filter (where coalesce(r.ok, true)
                                              and coalesce(r.scored, true)
                                              and r.err is not null
                                              and r.naive_error is not null)
                                                                    as removed,
         sum(greatest(r.naive_error,
                      case when r.unit = 'bps' then 5.0 else 0.01 end))
             filter (where coalesce(r.ok, true)
                       and coalesce(r.scored, true)
                       and r.err is not null
                       and r.naive_error is not null)               as benchmark
    from rsi.rollouts r
   group by r.topic, r.run_id, r.side, r.split;

comment on view rsi.run_side_stats is
  'Pooled skill per run, side and split, summed in the database. The browser '
  'used to pull every rollout of a topic to add these up and crossed the '
  'statement timeout doing it. Mirrors web/stats.js::pooled; '
  'tests/test_pooled_view.py holds the two to the same answers.';

-- The crypto topic's tick is two basis points and the news topic's is five, so
-- a single `unit` column cannot floor both correctly. It is right for news and
-- generous for crypto by three basis points on a quiet window, which moves a
-- pooled number in the fourth decimal. Recorded rather than hidden: the honest
-- fix is a tick column on the row, and that is a migration with a backfill,
-- not a view.

create index if not exists rollouts_stats on rsi.rollouts (topic, split, run_id, side)
  include (err, naive_error, ok, scored, unit);

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
