"""The league -> Kalshi series mapping, and the sweep list built from it.

Offline like the rest: the table is data, and what is tested is that it is
internally consistent, that every league in it can reach a fixture feed, and
that the lookup falls back the way callers assume. The live check against the
exchange is ``scripts/check_leagues.py``, which is not a test because a venue
adding a competition is news rather than a regression.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from rsi_arena.kalshi import _series
from rsi_arena.kalshi._gamestate import ESPN_PATHS
from rsi_arena.kalshi._series import (NOT_FIXTURES, SEASONAL, SERIES_BY_LEAGUE,
                                      SWEEP_LEAGUES, UNMAPPED, check_table,
                                      series_for)
from rsi_arena.kalshi._taxonomy import (COMPETITIONS, EXTRA_SCOREBOARD_SLUGS,
                                        SOCCER_STEMS, match_competition,
                                        resolve_league)

ROOT = Path(__file__).resolve().parent.parent


def load_script(name: str):
    """A script under ``scripts/`` as a module. They are entry points, not a package."""
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# -- the table itself ------------------------------------------------------------

def test_table_is_internally_consistent():
    assert check_table() == []


def test_every_mapped_league_reaches_a_fixture_feed():
    """A series with no game-state feed is a market nobody can grade.

    ``ESPN_PATHS`` is what ``todays_games`` and ``match_timeline`` route on, so a
    league missing from it links to nothing however well its Kalshi side works.
    """
    missing = [lg for lg in SERIES_BY_LEAGUE if lg not in ESPN_PATHS]
    assert missing == []


def test_no_two_leagues_claim_one_series():
    tickers = list(SERIES_BY_LEAGUE.values())
    assert len(tickers) == len(set(tickers))


def test_every_series_looks_like_a_kalshi_series():
    for league, series in SERIES_BY_LEAGUE.items():
        assert series.startswith("KX"), league
        assert series.endswith("GAME"), league
        assert series.isupper() and series.isalnum(), league


def test_the_three_residue_tables_do_not_overlap_the_mapping():
    mapped = set(SERIES_BY_LEAGUE.values())
    assert mapped.isdisjoint({s for s, _ in UNMAPPED.values()})
    assert mapped.isdisjoint({s for s, _ in SEASONAL.values()})
    assert mapped.isdisjoint(set(NOT_FIXTURES))


def test_every_unmapped_entry_says_why():
    for league, (series, why) in UNMAPPED.items():
        assert series.startswith("KX"), league
        assert len(why) > 10, f"{league} gives no reason"


def test_extra_scoreboard_slugs_belong_to_mapped_leagues():
    """A qualifying feed is only useful for a competition the sweep visits."""
    for league in EXTRA_SCOREBOARD_SLUGS:
        assert league in SERIES_BY_LEAGUE, league
        assert league in ESPN_PATHS, league


# -- the bug this exists to prevent ---------------------------------------------

def test_uefa_nations_league_is_KXUEFANLGAME():
    """The regression. A day of Nations League fixtures produced no forecasts.

    The sweep derived the series as ``KX{league}GAME``, which for UEFA_NATIONS
    asks Kalshi for ``KXUEFANATIONSGAME``. No such series exists, the listing
    comes back empty, and an empty listing is indistinguishable from an evening
    with no football in it — so the collection reported a quiet night and the
    whole card went unforecast, silently.
    """
    assert series_for("UEFA_NATIONS") == "KXUEFANLGAME"
    assert series_for("UEFA_NATIONS") != "KXUEFANATIONSGAME"
    assert ESPN_PATHS["UEFA_NATIONS"] == ("soccer", "uefa.nations")


@pytest.mark.parametrize("league,series", [
    ("UEFA_NATIONS", "KXUEFANLGAME"),
    ("LIBERTADORES", "KXCONMEBOLLIBGAME"),
    ("SUDAMERICANA", "KXCONMEBOLSUDGAME"),
    ("UEFA_SUPERCUP", "KXUEFASCGAME"),
    ("CONCACAF_CL", "KXCONCACAFCCUPGAME"),
    # The other shape of the same bug: a league whose code and whose series stem
    # are different words. The rule would ask for KXEFLGAME, which does not exist.
    ("EFL", "KXEFLCHAMPIONSHIPGAME"),
    ("ARGENTINA", "KXARGPREMDIVGAME"),
    ("ARGENTINA2", "KXARGNACBGAME"),
    ("COLOMBIA", "KXDIMAYORGAME"),
    ("PARAGUAY", "KXAPFDDHGAME"),
    ("SWEDEN", "KXALLSVENSKANGAME"),
    ("NORWAY", "KXELITESERIENGAME"),
])
def test_series_the_rule_would_have_got_wrong(league, series):
    assert series_for(league) == series
    assert series != f"KX{league.replace('_', '')}GAME", (
        f"{league} is not a case the rule got wrong; put it in another test")


def test_every_league_code_resolves_to_itself():
    """The second half of the same gap, and the quieter half.

    ``espn_scoreboard`` routes through ``resolve_league``, which used to strip
    underscores out of a code *before* looking it up — so ``UEFA_NATIONS`` became
    ``UEFANATIONS``, matched no key and no ticker stem, came back None, and the
    scoreboard call raised. Even with the right series ticker, the Nations League
    could not have reached its fixture feed. Seven codes were in that state.
    """
    assert [lg for lg in COMPETITIONS if resolve_league(lg) != lg] == []


def test_resolve_league_still_takes_stems_and_slugs():
    assert resolve_league("uefa.nations") == "UEFA_NATIONS"
    assert resolve_league("soccer/eng.1") == "EPL"
    assert resolve_league("SAUDIPL") == "SAUDI"
    assert resolve_league("uefa_nations") == "UEFA_NATIONS"


def test_us_open_cup_and_usl_cup_are_different_competitions():
    """They were one entry, pointing at the wrong ESPN feed.

    ``USLCUP`` used to resolve to US_OPEN_CUP and ``usa.open``. They are two
    cups: the USL Cup is ``usa.usl.l1.cup`` and the US Open Cup is ``usa.open``,
    and Kalshi prices both.
    """
    assert series_for("USL_CUP") == "KXUSLCUPGAME"
    assert series_for("US_OPEN_CUP") == "KXUSOPENCUPGAME"
    assert ESPN_PATHS["USL_CUP"] == ("soccer", "usa.usl.l1.cup")
    assert ESPN_PATHS["US_OPEN_CUP"] == ("soccer", "usa.open")


# -- the lookup's behaviour ------------------------------------------------------

def test_series_for_falls_back_and_says_so():
    said: list[str] = []
    _series._FELL_BACK.discard("NOTALEAGUE")
    assert _series.series_for("NOTALEAGUE", log=said.append) == "KXNOTALEAGUEGAME"
    assert said and "KXNOTALEAGUEGAME" in said[0]
    # Once per league, not once per call: this runs inside a sweep loop.
    said.clear()
    assert _series.series_for("NOTALEAGUE", log=said.append) == "KXNOTALEAGUEGAME"
    assert said == []
    _series._FELL_BACK.discard("NOTALEAGUE")


def test_series_for_knows_the_leagues_it_cannot_grade():
    """Which series prices a league has an answer even when nothing can grade it.

    Only the sweep list is about what can be forecast. Asking for the
    Ekstraklasa by hand should get the ticker that exists, not a guess.
    """
    assert series_for("POLAND") == "KXEKSTRAKLASAGAME"
    assert series_for("CONCACAF_CL") == "KXCONCACAFCCUPGAME"
    assert "POLAND" not in SWEEP_LEAGUES
    assert "CONCACAF_CL" not in SWEEP_LEAGUES


def test_series_for_is_case_and_space_insensitive():
    assert series_for(" uefa_nations ") == "KXUEFANLGAME"


def test_series_for_strips_underscores_in_the_fallback():
    _series._FELL_BACK.discard("MADE_UP")
    assert _series.series_for("MADE_UP", log=lambda _m: None) == "KXMADEUPGAME"
    _series._FELL_BACK.discard("MADE_UP")


# -- the sweep list -------------------------------------------------------------

def test_sweep_list_parses_the_way_the_collector_parses_it():
    """``--league`` is a comma-separated string, and every entry must resolve."""
    leagues = [x.strip().upper() for x in ",".join(SWEEP_LEAGUES).split(",") if x.strip()]
    assert leagues == list(SWEEP_LEAGUES)
    for league in leagues:
        assert league in COMPETITIONS, league
        assert resolve_league(league) == league, league
        assert series_for(league) == SERIES_BY_LEAGUE[league]


def test_sweep_list_leads_with_the_big_competitions():
    """A sweep that runs out of clock should have spent it on the majors."""
    assert SWEEP_LEAGUES[:5] == ("EPL", "LALIGA", "SERIEA", "BUNDESLIGA", "LIGUE1")
    head = set(SWEEP_LEAGUES[:20])
    for big in ("UCL", "UEL", "UEFA_NATIONS", "MLS", "LIGAMX", "EREDIVISIE"):
        assert big in head, big


def test_the_eight_leagues_the_sweep_used_to_cover_are_still_in_it():
    for league in ("EPL", "LALIGA", "SERIEA", "BUNDESLIGA", "LIGUE1",
                   "MLS", "LIGAMX", "EREDIVISIE"):
        assert league in SWEEP_LEAGUES


def test_the_sweep_is_much_wider_than_it_was():
    assert len(SWEEP_LEAGUES) > 60


# -- the scripts that consume it -------------------------------------------------

def test_discover_fixtures_series_for_is_the_shared_lookup():
    df = load_script("discover_fixtures")
    assert df.series_for("UEFA_NATIONS") == "KXUEFANLGAME"
    assert df.series_for("EPL") == "KXEPLGAME"


def test_discover_fixtures_reads_the_day_off_a_ticker():
    df = load_script("discover_fixtures")
    assert df.event_day("KXUEFANLGAME-26SEP10ITAISR") == "2026-09-10"
    assert df.event_day("KXEPLTEAMPOINTS-27") == ""


def test_collect_live_defaults_to_every_verified_competition():
    cl = load_script("collect_live")
    assert cl.SWEEP_LEAGUES == SWEEP_LEAGUES
    assert cl.series_for("UEFA_NATIONS") == "KXUEFANLGAME"


# -- the taxonomy side ----------------------------------------------------------

def test_new_stems_classify_their_own_competition():
    """A second tier is not its top flight, and a nations league is not a club cup."""
    for stem, league in [("UEFANLGAME", "UEFA_NATIONS"),
                         ("CONCACAFNLGAME", "CONCACAF_NL"),
                         ("LALIGA2GAME", "LALIGA2"),
                         ("BUNDESLIGA2GAME", "BUNDESLIGA2"),
                         ("LIGUE2GAME", "LIGUE2"),
                         ("EFLL1GAME", "EFL_L1"),
                         ("BRASILEIROBGAME", "BRASILEIRAO_B"),
                         ("EREDIVISIEWGAME", "EREDIVISIE_W"),
                         ("INTLFRIENDLYGAME", "INTL_FRIENDLY"),
                         ("CLUBFGAME", "CLUB_FRIENDLY")]:
        hit = match_competition(stem)
        assert hit is not None, stem
        assert hit[0] == league, stem


def test_a_cups_match_winner_is_a_game_winner_not_a_championship():
    """``is_game_level`` was False for every cup's match-winner series.

    The market-type patterns test CHAMPIONSHIP (which matches "CUP") before
    GAME_WINNER, so `KXFACUPGAME`, `KXEFLCUPGAME`, `KXCOPADELREYGAME` and
    `KXUSLCUPGAME` all came out as season futures — and a discovery pass that
    filters on game-level markets would have dropped every cup fixture.
    """
    from rsi_arena.kalshi._taxonomy import MarketType, classify_series
    for ticker in ("KXFACUPGAME", "KXEFLCUPGAME", "KXCOPADELREYGAME",
                   "KXUSLCUPGAME", "KXUSOPENCUPGAME", "KXUEFASCGAME",
                   "KXUEFANLGAME", "KXCONCACAFNLGAME"):
        cls = classify_series(ticker, category="Sports", tags=["Soccer"])
        assert cls.market_type is MarketType.GAME_WINNER, ticker
        assert cls.is_game_level, ticker


def test_every_soccer_stem_names_a_competition_in_the_registry():
    for stem, (league, slug) in SOCCER_STEMS.items():
        assert league in COMPETITIONS, stem
        assert COMPETITIONS[league][1] == f"soccer/{slug}", stem
