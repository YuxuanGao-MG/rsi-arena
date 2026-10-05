-- Three reads the reader makes that the tables were not built for.
--
-- The News pages stopped loading on 4 October: "the database did not answer
-- within 15s". Not the overview this time - migration 012 fixed that one by
-- materialising the pooled statistic - but the Metrics and Trading pages, which
-- read rows rather than summaries.
--
-- Each was a plan problem, and each was invisible until the table grew into it:
--
--   * `rollouts` filtered by topic and split, selecting `skill` and
--     `unmeasurable`. Those two are not in the covering index 012 added, so
--     40,897 rows were fetched from the heap one at a time: 16.2 seconds, five
--     times the statement timeout. Widening the index to carry them makes it an
--     index-only scan. It replaces `rollouts_stats` rather than sitting beside
--     it, because the view's refresh wants the same leading columns.
--
--   * `book_marks` ordered by `at` across a topic. The primary key leads with
--     `book_id`, so ordering by time alone sorted 41,105 rows: 1.6 seconds.
--
--   * `trades` ordered by `opened_at`. The index that exists is on `closed_at`,
--     which is the column the publisher reads and not the one the page sorts
--     by, so this sorted 13,617 rows - 132ms for the smallest topic, and this
--     table grows fastest of the three.
--
-- The lesson the hourly check already learnt: a query that takes two seconds is
-- not failing, it is on its way there. The watchdog times the reader's own
-- queries; these three were not among the two it knew about.
begin;

-- Carries everything both readers of this table select, so neither returns to
-- the heap: the metrics page's windows, and the refresh of `run_side_stats`.
drop index if exists rsi.rollouts_stats;
create index rollouts_reader on rsi.rollouts (topic, split, run_id, side)
  include (skill, err, naive_error, unmeasurable, ok, scored, unit);

create index if not exists book_marks_topic_at on rsi.book_marks (topic, at);

create index if not exists trades_topic_opened on rsi.trades (topic, opened_at desc);

commit;

analyze rsi.rollouts;
analyze rsi.book_marks;
analyze rsi.trades;
