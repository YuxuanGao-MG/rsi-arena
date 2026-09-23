-- Paper books: what the harness's forecasts would have bought.
--
-- Run this by hand against the project; nothing in the repository applies it.
--
--     psql "$SUPABASE_DB_URL" -f supabase/migrations/009_trading.sql
--
-- Until now the arena scored a forecast and stopped: a skill number per
-- window, pooled per generation. A trading engine now turns the same
-- forecasts into positions - sized, fee'd, held to the horizon or closed
-- early - and keeps a book per harness. Two kinds of book exist and they
-- share these tables:
--
--   * `live`   - one per topic, `book_id = live:<topic>`, appended to by every
--                live sweep. The runner's copy of its state lives in a cache
--                that can be evicted; these rows are the durable copy.
--   * `replay` - one per (run, side, split), `book_id = <run_id>:<side>:<split>`,
--                written by a generation's rollouts and published with them.
--
-- Three tables, all keyed by topic first so a reader's page for one loop
-- never scans another's:
--
--   * `rsi.books`      - one row per book: which harness, which run, and the
--                        running stats as a JSON object (total_return, sharpe,
--                        max_drawdown, hit_rate, turnover, fees, trades, ...).
--                        Replaced whole on every publish; `updated_at` says when.
--   * `rsi.trades`     - one row per position, keyed (topic, book_id,
--                        instrument, opened_at). A trade is published once
--                        when it opens and again when it closes, and the
--                        second write fills in `closed_at`, `exit_px` and
--                        `pnl_usd` on the same row rather than adding one.
--   * `rsi.book_marks` - the equity curve: one row per mark, keyed by instant.
--                        A mark is a fact about an instant, so a row already
--                        there is left alone.
--
-- Nothing here is written by the anon key. The publishers write with the
-- database role; the site reads through the views, which are created with
-- migration 004's recipe: create or replace, security_invoker on, revoke all
-- from anon and authenticated on the view and the table beneath it, grant
-- select, and the default-privilege revokes restated.

begin;

-- ---------------------------------------------------------------------------
-- 1. Books.

create table if not exists rsi.books (
  topic        text        not null,
  book_id      text        not null,
  harness_fp   text,
  harness_name text,
  kind         text        not null,
  run_id       text,
  side         text,
  split        text,
  started_at   timestamptz,
  updated_at   timestamptz default now(),
  stats        jsonb       not null default '{}',
  primary key (topic, book_id)
);

alter table rsi.books drop constraint if exists books_kind_check;
alter table rsi.books add constraint books_kind_check
  check (kind in ('live', 'replay'));

comment on table rsi.books is
  'One paper book per (topic, book_id): live:<topic> for the live loop, '
  '<run_id>:<side>:<split> for a generation''s replay. Stats are the running '
  'summary the engine computed and are replaced whole on every publish.';
comment on column rsi.books.stats is
  'total_return, sharpe, daily_sharpe, max_drawdown, hit_rate, avg_win, avg_loss, '
  'profit_factor, turnover, fees_usd, trades, cycles, open_positions, '
  'start_equity, end_equity, handovers, refusals.';

create index if not exists books_topic_kind_run on rsi.books (topic, kind, run_id);

alter table rsi.books enable row level security;
drop policy if exists books_read on rsi.books;
create policy books_read on rsi.books for select using (true);

-- ---------------------------------------------------------------------------
-- 2. Trades.

create table if not exists rsi.trades (
  id          bigserial   primary key,
  topic       text        not null,
  book_id     text        not null,
  harness_fp  text,
  run_id      text,
  instance_id text,
  side        text        not null,
  instrument  text        not null,
  opened_at   timestamptz not null,
  closed_at   timestamptz,
  entry_px    double precision,
  exit_px     double precision,
  qty         double precision,
  size_usd    double precision,
  fees_usd    double precision,
  pnl_usd     double precision,
  reason      text,
  source      text,
  unique (topic, book_id, instrument, opened_at)
);

comment on table rsi.trades is
  'One row per position in a paper book. Upserted on (topic, book_id, '
  'instrument, opened_at): the open is published with closed_at null and the '
  'close overwrites the same row. reason is agent | horizon | force_close | '
  'settled | handover | cap_refused; source is harness | default.';

create index if not exists trades_topic_book_closed on rsi.trades (topic, book_id, closed_at desc);

alter table rsi.trades enable row level security;
drop policy if exists trades_read on rsi.trades;
create policy trades_read on rsi.trades for select using (true);

-- ---------------------------------------------------------------------------
-- 3. Marks.

create table if not exists rsi.book_marks (
  topic              text             not null,
  book_id            text             not null,
  at                 timestamptz      not null,
  equity_usd         double precision not null,
  cash_usd           double precision,
  gross_exposure_usd double precision,
  open_positions     int,
  drawdown           double precision,
  event              text,
  primary key (topic, book_id, at)
);

comment on table rsi.book_marks is
  'The equity curve of a paper book, one row per mark. A mark is a fact about '
  'an instant: on conflict, the row already there stands.';

-- The primary key is the index (topic, book_id, at) a reader walks a curve
-- with; a second index on the same columns would cost every mark a write and
-- serve no query.

alter table rsi.book_marks enable row level security;
drop policy if exists book_marks_read on rsi.book_marks;
create policy book_marks_read on rsi.book_marks for select using (true);

-- ---------------------------------------------------------------------------
-- 4. The views, created the 004 way.
--
-- Step 1: create or replace.

create or replace view public.rsi_books      as select * from rsi.books;
create or replace view public.rsi_trades     as select * from rsi.trades;
create or replace view public.rsi_book_marks as select * from rsi.book_marks;

-- Step 2: run as the caller, so the policies on rsi.* actually apply.
alter view public.rsi_books      set (security_invoker = on);
alter view public.rsi_trades     set (security_invoker = on);
alter view public.rsi_book_marks set (security_invoker = on);

-- Step 3: take back everything the default handed out, view and table alike.
revoke all on public.rsi_books, public.rsi_trades, public.rsi_book_marks
  from anon, authenticated;
revoke all on rsi.books, rsi.trades, rsi.book_marks
  from anon, authenticated;

-- Step 4: give back only reading.
grant usage on schema rsi to anon, authenticated;
grant select on rsi.books, rsi.trades, rsi.book_marks
  to anon, authenticated;
grant select on public.rsi_books, public.rsi_trades, public.rsi_book_marks
  to anon, authenticated;

-- Step 5: and keep the default from coming back the next time a view is replaced.
alter default privileges in schema public revoke all on tables from anon, authenticated;
alter default privileges in schema rsi    revoke all on tables from anon, authenticated;

commit;
