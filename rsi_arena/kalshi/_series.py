"""Which Kalshi series prices a league's match winner.

``KX{LEAGUE}GAME`` was a rule, not a mapping, and it was wrong for most of the
football Kalshi lists. The UEFA Nations League trades as ``KXUEFANLGAME``; the
rule asks for ``KXUEFANATIONSGAME``, gets an empty listing, and reports a quiet
evening. A day of Nations League fixtures therefore produced no forecasts and no
error — the sweep had simply been told there were no matches.

It is a mapping now, and the rule is only the fallback. Every pair below was
**verified against the live exchange and the live fixture feed on 2026-09-27**,
by one procedure:

1. List every series in Kalshi's ``Sports`` category (3,920 of them) and keep
   the ones whose own ``tags`` field says ``Soccer`` (1,415). Sport comes from
   Kalshi's metadata, never from the ticker — pattern-matching the ticker is
   what hid ``KXUEFANLGAME`` in the first place.
2. Sweep each candidate's markets. A per-match moneyline series has events of
   two or three markets whose outcomes are clubs or nations, and event tickers
   that ``_linking.parse_event_ticker`` splits into two team codes. 140 series
   are named ``...GAME``; 108 of those carry fixtures today.
3. Link a few of each series' newest fixtures against the ESPN competition that
   serves it, and build a timeline from one linked game. A pair is recorded here
   only when a link and a timeline both came out; the comment on each line says
   which ESPN competition answered and how many of the sampled fixtures linked.

The residue is in :data:`UNMAPPED`, with the reason, and in :data:`SEASONAL`.
Neither is guessed at: a competition nobody can attach to a fixture feed is a
market the harness would forecast blind, which is the one failure
``collect_live`` exists to make loud.

Re-run :mod:`scripts.check_leagues` occasionally. Kalshi adds competitions
weekly and renames a few, and this file is only true on the day it was written.
"""

from __future__ import annotations

from ._taxonomy import COMPETITIONS

#: league code -> the Kalshi series that prices its match winner.
#:
#: Ordered biggest first, which is the order the live sweep walks: a sweep that
#: runs out of time or budget should have spent it on the Champions League
#: rather than on college soccer.
SERIES_BY_LEAGUE: dict[str, str] = {
    # -- The majors, and the two American leagues the sweep already covered
    "EPL": "KXEPLGAME",                             # eng.1: 2/2 linked at up to 1.0
    "LALIGA": "KXLALIGAGAME",                       # esp.1: 2/2 linked at up to 1.0
    "SERIEA": "KXSERIEAGAME",                       # ita.1: 2/2 linked at up to 1.0
    "BUNDESLIGA": "KXBUNDESLIGAGAME",               # ger.1: 2/2 linked at up to 0.9
    "LIGUE1": "KXLIGUE1GAME",                       # fra.1: 2/2 linked at up to 1.0
    "EREDIVISIE": "KXEREDIVISIEGAME",               # ned.1: 2/2 linked at up to 0.925
    "MLS": "KXMLSGAME",                             # usa.1: 2/2 linked at up to 0.9
    "LIGAMX": "KXLIGAMXGAME",                       # mex.1: 2/2 linked at up to 0.95

    # -- UEFA: the club competitions, and the national-team one that started this
    "UCL": "KXUCLGAME",                             # uefa.champions: 2/2 linked at up to 1.0
    "UEL": "KXUELGAME",                             # uefa.europa: 2/2 linked at up to 0.95
    "UECL": "KXUECLGAME",                           # uefa.europa.conf, linked through the conf_qual feed; the league phase is not listed yet
    "UEFA_NATIONS": "KXUEFANLGAME",                 # uefa.nations: 2/2 linked at up to 1.0 — the series the KX{LEAGUE}GAME rule could never find
    "UCL_W": "KXUCLWGAME",                          # uefa.wchampions: 2/2 linked at up to 1.0
    "UEFA_SUPERCUP": "KXUEFASCGAME",                # uefa.super_cup: its one fixture a year linked at 0.7

    # -- The Americas, continental
    "LIBERTADORES": "KXCONMEBOLLIBGAME",            # conmebol.libertadores: 2/2 linked at up to 0.95
    "SUDAMERICANA": "KXCONMEBOLSUDGAME",            # conmebol.sudamericana: 2/2 linked at up to 0.95
    "CONCACAF_NL": "KXCONCACAFNLGAME",              # concacaf.nations.league: 2/2 linked at up to 0.95
    "LEAGUES_CUP": "KXLEAGUESCUPGAME",              # concacaf.leagues.cup: 2/3 linked at up to 1.0

    # -- National teams and friendlies
    "INTL_FRIENDLY": "KXINTLFRIENDLYGAME",          # fifa.friendly: 2/2 linked at up to 1.0
    "CLUB_FRIENDLY": "KXCLUBFGAME",                 # club.friendly: 2/5 linked at up to 0.617 — a ragged feed, as pre-season friendlies are
    "AFCON": "KXAFCONGAME",                         # caf.nations, linked through caf.nations_qual; the finals are in December
    "FIFA_WWC": "KXFIFAWGAME",                      # fifa.wwc, linked through fifa.wworldq.uefa — today the series is the 2027 qualifying groups

    # -- Spain, Italy, Germany, France: second tiers and domestic cups
    "LALIGA2": "KXLALIGA2GAME",                     # esp.2: 2/2 linked at up to 0.9
    "COPA_DEL_REY": "KXCOPADELREYGAME",             # esp.copa_del_rey: 2/2 linked at up to 0.95
    "SERIEB": "KXSERIEBGAME",                       # ita.2: 2/2 linked at up to 1.0
    "COPPA_ITALIA": "KXCOPPAITALIAGAME",            # ita.coppa_italia: 2/2 linked at up to 0.9
    "BUNDESLIGA2": "KXBUNDESLIGA2GAME",             # ger.2: 2/2 linked at up to 0.95
    "DFB_POKAL": "KXDFBPOKALGAME",                  # ger.dfb_pokal: 2/4 linked at up to 0.9
    "LIGUE2": "KXLIGUE2GAME",                       # fra.2: 2/2 linked at up to 0.95
    "FRA_SUPERCUP": "KXFRASUPERCUPGAME",            # fra.super_cup: its one fixture a year linked at 0.7

    # -- England below and beside the Premier League
    "EFL": "KXEFLCHAMPIONSHIPGAME",                 # eng.2: 2/2 linked at up to 0.925 — note the series is not KXEFLGAME
    "EFL_L1": "KXEFLL1GAME",                        # eng.3: 2/2 linked at up to 0.95
    "ENG_NL": "KXENGNLGAME",                        # eng.5: 2/2 linked at up to 0.95
    "FA_CUP": "KXFACUPGAME",                        # eng.fa, linked through eng.fa_qual; the proper rounds start in November
    "EFL_CUP": "KXEFLCUPGAME",                      # eng.league_cup: 2/2 linked at up to 0.9
    "ENG_SHIELD": "KXENGCSGAME",                    # eng.charity: its one fixture a year linked at 0.9
    "EWSL": "KXEWSLGAME",                           # eng.w.1: 2/2 linked at up to 0.975

    # -- The rest of Europe
    "PRIMEIRA": "KXLIGAPORTUGALGAME",               # por.1: 2/2 linked at up to 0.95
    "TACA_PORTUGAL": "KXTACAPORTGAME",              # por.taca.portugal: 2/2 linked at up to 0.9
    "SCOTTISH": "KXSCOTTISHPREMGAME",               # sco.1: 2/2 linked at up to 1.0
    "BELGIUM": "KXBELGIANPLGAME",                   # bel.1: 2/2 linked at up to 0.7
    "TURKEY": "KXSUPERLIGGAME",                     # tur.1: 2/2 linked at up to 1.0
    "GREECE": "KXSLGREECEGAME",                     # gre.1: 2/2 linked at up to 0.95
    "DENMARK": "KXDENSUPERLIGAGAME",                # den.1: 2/3 linked at up to 0.7
    "SWEDEN": "KXALLSVENSKANGAME",                  # swe.1: 2/2 linked at up to 0.75
    "NORWAY": "KXELITESERIENGAME",                  # nor.1: 2/4 linked at up to 0.7
    "NED2": "KXEERSTEDIVGAME",                      # ned.2: 2/2 linked at up to 0.95
    "KNVB_CUP": "KXKNVBCUPGAME",                    # ned.cup: 2/2 linked at up to 0.9
    "EREDIVISIE_W": "KXEREDIVISIEWGAME",            # ned.w.1: 2/2 linked at up to 1.0

    # -- South America, domestic
    "ARGENTINA": "KXARGPREMDIVGAME",                # arg.1: 2/2 linked at up to 0.95
    "ARGENTINA2": "KXARGNACBGAME",                  # arg.2: 2/2 linked at up to 0.95
    "BRASILEIRAO": "KXBRASILEIROGAME",              # bra.1: 2/2 linked at up to 0.95
    "BRASILEIRAO_B": "KXBRASILEIROBGAME",           # bra.2: 2/2 linked at up to 0.95
    "COPA_DO_BRASIL": "KXCOPADOBRASILGAME",         # bra.copa_do_brazil: 2/2 linked at up to 0.95
    "CHILE": "KXCHLLDPGAME",                        # chi.1: 2/3 linked at up to 0.975
    "COLOMBIA": "KXDIMAYORGAME",                    # col.1: 2/2 linked at up to 0.7
    "ECUADOR": "KXECULPGAME",                       # ecu.1: 2/3 linked at up to 0.95
    "PERU": "KXPERLIGA1GAME",                       # per.1: 2/3 linked at up to 0.9
    "URUGUAY": "KXURYPDGAME",                       # uru.1: 2/2 linked at up to 0.9
    "VENEZUELA": "KXVENFUTVEGAME",                  # ven.1: 2/2 linked at up to 0.975
    "BOLIVIA": "KXBOLPDIVGAME",                     # bol.1: 2/2 linked at up to 1.0
    "PARAGUAY": "KXAPFDDHGAME",                     # par.1: 2/2 linked at up to 0.95

    # -- Asia
    "AFC_CL": "KXAFCCLGAME",                        # afc.champions: 2/2 linked at up to 0.95
    "ASEAN": "KXASEANGAME",                         # aff.championship: 2/2 linked at up to 1.0
    "JLEAGUE": "KXJLEAGUEGAME",                     # jpn.1: 2/2 linked at up to 0.9
    "CHINA": "KXCHNSLGAME",                         # chn.1: 2/2 linked at up to 0.95
    "SAUDI": "KXSAUDIPLGAME",                       # ksa.1: 2/2 linked at up to 0.9

    # -- North America below MLS
    "LIGA_EXPANSION": "KXLIGAEXPGAME",              # mex.2: 2/2 linked at up to 0.9
    "NWSL": "KXNWSLGAME",                           # usa.nwsl: 2/2 linked at up to 1.0
    "USL": "KXUSLGAME",                             # usa.usl.1: 2/2 linked at up to 0.95
    "USL_CUP": "KXUSLCUPGAME",                      # usa.usl.l1.cup: 2/2 linked at up to 0.7
    "US_OPEN_CUP": "KXUSOPENCUPGAME",               # usa.open: 2/2 linked at up to 0.875
    "NCAA_SOCCER": "KXNCAAMSOCCERGAME",             # usa.ncaa.m.1: 2/2 linked at up to 1.0
}

#: Series that are real per-match moneylines but cannot be forecast here, and why.
#:
#: Every one was found by the same sweep and carries live fixtures on Kalshi.
#: What is missing is the other half: a fixture feed that can say what the score
#: was at minute sixty. Kept rather than deleted, because this is the list
#: :mod:`scripts.check_leagues` re-tries — an ESPN competition that starts being
#: served promotes straight into the table above.
UNMAPPED: dict[str, tuple[str, str]] = {
    # ESPN carries the competition in its catalogue but served no fixtures on any
    # of the days Kalshi listed, probed across a year on 2026-09-27.
    "SWITZERLAND": ("KXSWISSLEAGUEGAME", "sui.1 answers 200 with an empty card on every probed day"),
    "CZECH": ("KXCZEFLGAME", "cze.1 answers 200 with an empty card"),
    "ISRAEL": ("KXISRPLGAME", "isr.1 answers 200 with an empty card"),
    "SWEDEN2": ("KXETTANGAME", "swe.2 answers 200 with an empty card"),
    "NED3": ("KXTWEEDEDIVGAME", "ned.3 serves some days but none Kalshi lists"),
    "BRASILEIRAO_C": ("KXBRASILEIROCGAME", "bra.3 serves four fixtures a year, none Kalshi lists"),
    "THAILAND": ("KXTHAIL1GAME", "tha.1 serves other days, none Kalshi lists"),
    "INDONESIA": ("KXIDNSLGAME", "idn.1 serves other days, none Kalshi lists"),
    "MALAYSIA": ("KXMYSLGAME", "mys.1 serves other days, none Kalshi lists"),
    "SINGAPORE": ("KXSGPPLGAME", "sgp.1 serves other days, none Kalshi lists"),
    "SERIEC": ("KXSERIECGAME", "ESPN has no Italian Serie C competition"),
    "SERIEC_CUP": ("KXSERIECCUPGAME", "ESPN has no Italian Serie C competition"),
    "SCOTTISH_CUP": ("KXSCOCUPGAME", "sco.tennents served nothing in a year of probes"),
    "TURKEY2": ("KXTFF1LIGGAME", "tur.2 answers 200 with an empty card"),
    # ESPN rejects the slug outright: the competition is not covered at all.
    "GER_3L": ("KXGER3LGAME", "ger.3 is a 400"),
    "SERIEA_W": ("KXSERIEAWGAME", "ita.w.1 is a 400"),
    "GREECE_CUP": ("KXGRECUPGAME", "ESPN has no Greek cup competition"),
    "POLAND": ("KXEKSTRAKLASAGAME", "pol.1 is a 400"),
    "CZECH2": ("KXCZEFNLGAME", "cze.2 is a 400"),
    "CROATIA": ("KXHNLGAME", "cro.1 is a 400"),
    "SERBIA": ("KXSRBSLGAME", "srb.1 is a 400"),
    "SLOVAKIA2": ("KXSVK2LGAME", "svk.2 is a 400"),
    "SLOVENIA": ("KXSVNPLGAME", "svn.1 is a 400"),
    "ISRAEL2": ("KXISRNLGAME", "isr.2 is a 400"),
    "LATVIA": ("KXLVAVIRGAME", "lva.1 is a 400"),
    "FINLAND2": ("KXFINYLGAME", "fin.2 is a 400"),
    "FAROE": ("KXFROPLGAME", "fro.1 is a 400"),
    "EGYPT": ("KXEGYPLGAME", "egy.1 is a 400"),
    "CANADA": ("KXCANPLGAME", "can.1 is a 400"),
    "JLEAGUE2": ("KXJ2LEAGUEGAME", "jpn.2 is a 400"),
    "KLEAGUE": ("KXKLEAGUEGAME", "kor.1 is a 400"),
    "KLEAGUE2": ("KXK2LEAGUEGAME", "kor.2 is a 400"),
    "CHINA2": ("KXCHNL1GAME", "chn.2 is a 400"),
    "UAE": ("KXUAEPLGAME", "uae.1 is a 400"),
    "VIETNAM": ("KXVLEAGUE1GAME", "vie.1 is a 400"),
    "SVK_CUP": ("KXSVKCUPGAME", "svk.cup is a 400"),
    "FINLAND_CUP": ("KXFINCUPGAME", "fin.cup is a 400"),
    "FAROE_CUP": ("KXFROCUPGAME", "fro.cup is a 400"),
    "QATAR": ("KXQSTARSGAME", "ESPN has no Qatari league competition"),
    "BALLER_LEAGUE": ("KXBALLERLEAGUEGAME", "ESPN has no Baller League competition"),
    "ISRAEL_PL_CUP": ("KXISRPLCUPGAME", "ESPN has no Israeli cup competition"),
    "SRB_CUP": ("KXSRBCUPGAME", "ESPN has no Serbian cup competition"),
    "SVN_CUP": ("KXSVNCUPGAME", "ESPN has no Slovenian cup competition"),
    "ISRAEL_NL_CUP": ("KXISRNLCUPGAME", "ESPN has no Israeli cup competition"),
    "ISRAEL_STATE_CUP": ("KXISRSCUPGAME", "ESPN has no Israeli cup competition"),
    "ISRAEL_SUPERCUP": ("KXISRSUPCUPGAME", "ESPN has no Israeli cup competition"),
    "SERIEC_SUPERCUP": ("KXSERIECSCUPGAME", "ESPN has no Italian Serie C competition"),
    # The feed has the fixture; the exchange's own label defeats the linker.
    # Kalshi prefixes these series' outcome names with "Reg Time: ", so
    # "Reg Time: Dortmund" is scored against "Borussia Dortmund" and the code
    # BVB against the same, and neither clears the 0.6 threshold. One fixture a
    # year each, so the linker is not worth changing for them.
    "GER_SUPERCUP": ("KXGERSCGAME", 'outcomes are labelled "Reg Time: <club>"'),
    "MLS_ALLSTAR": ("KXMLSASTGAME", 'outcomes are labelled "Reg Time: <team>"'),
}

#: Series whose ticker ends in GAME but which are not a match winner, or are not
#: anything at all. Recorded so ``check_leagues`` can tell a series nobody has
#: looked at from one that was looked at and dismissed — otherwise every run
#: reports the same two dozen names as new.
NOT_FIXTURES: dict[str, str] = {
    "KXFIFAGAME": "empty; no market has ever been listed under it",
    "KXUEFAGAME": "empty; no market has ever been listed under it",
    "KXFIFAUSPULLGAME": "empty; an administrative series",
    "KXKXECULPGAME": 'titled "delete"; a duplicate of KXECULPGAME',
    "KXPRYLIGA1GAME": 'titled "delete"; Paraguay trades as KXAPFDDHGAME',
    "KXDANISHSUPERLIGAGAME": "empty duplicate of KXDENSUPERLIGAGAME",
    "KXWCGOALEVERYGAME": "a tournament-wide prop, not one match's winner",
    "KXWCTEAMSINGAME": "a tournament-wide prop, not one match's winner",
    "KXINTERCONCUPGAME": "empty; the Intercontinental Cup is played in December",
    "KXFINALISSIMAGAME": "empty; played when a Copa America winner meets a Euro winner",
}

#: Series that exist, name a competition, and had no market listed on 2026-09-27.
#:
#: Almost all of these are simply out of season — a cup that starts in November,
#: a tournament held every other winter. The slug beside each is the ESPN
#: competition that should serve it, so ``check_leagues`` can verify and promote
#: them the week they list. Nothing here is swept: a series with no markets costs
#: a round trip and returns nothing.
#:
#: WORLD_CUP is the one entry with no ticker stem in ``_taxonomy.SOCCER_STEMS``.
#: ``WC`` is too short a stem to add safely — it would swallow the fifty World
#: Cup prop series — so its ESPN competition is recorded here and nowhere else.
SEASONAL: dict[str, tuple[str, str]] = {
    "CONCACAF_CL": ("KXCONCACAFCCUPGAME", "concacaf.champions"),
    "A_LEAGUE": ("KXALEAGUEGAME", "aus.1"),
    "COUPE_DE_FRANCE": ("KXCOUPEDEFRANCEGAME", "fra.coupe_de_france"),
    "AFC_ASIAN_CUP": ("KXAFCACGAME", "afc.asian.cup"),
    "CLUB_WC": ("KXCLUBWCGAME", "fifa.cwc"),
    "ESP_SUPERCUP": ("KXESPSUPERCUPGAME", "esp.super_cup"),
    "ITA_SUPERCUP": ("KXITASUPERCUPGAME", "ita.super_cup"),
    "WORLD_CUP": ("KXWCGAME", "fifa.world"),
}

#: Leagues the live sweep and the weekly roll walk, biggest first.
#:
#: Every verified pair, because the cost of a quiet league is one events call:
#: ``collect_live`` builds a league's market catalogue only once it has seen an
#: open event, so sixty-five extra competitions cost nothing on the nights they
#: are not playing.
SWEEP_LEAGUES: tuple[str, ...] = tuple(SERIES_BY_LEAGUE)

#: Every league whose series ticker is known, usable or not.
#:
#: ``series_for`` answers "which series prices this league", which has a right
#: answer even for a competition ESPN cannot grade. Only :data:`SWEEP_LEAGUES` is
#: about what can be forecast, so asking for the Ekstraklasa by hand gets the
#: ticker that exists rather than a guess that does not.
ALL_SERIES: dict[str, str] = {
    **{lg: series for lg, (series, _why) in UNMAPPED.items()},
    **{lg: series for lg, (series, _slug) in SEASONAL.items()},
    **SERIES_BY_LEAGUE,
}

#: Leagues whose ``series_for`` fell through to the rule, reported once each.
_FELL_BACK: set[str] = set()


def series_for(league: str, log=None) -> str:
    """The Kalshi match-winner series for a league.

    A lookup, with ``KX{LEAGUE}GAME`` as the fallback for a league nobody has
    verified yet. The fallback is announced the first time it is used for a
    given league: it is right for about half the competitions Kalshi lists and
    wrong for the rest, and a wrong series ticker is indistinguishable from a
    league with no matches on.
    """
    code = (league or "").strip().upper()
    hit = ALL_SERIES.get(code)
    if hit:
        return hit
    guess = f"KX{code.replace('_', '')}GAME"
    if code not in _FELL_BACK:
        _FELL_BACK.add(code)
        say = log or print
        say(f"  {code}: no verified series; guessing {guess} from the "
            f"KX<LEAGUE>GAME rule. Run scripts/check_leagues.py to settle it.")
    return guess


def check_table() -> list[str]:
    """Everything inconsistent about the tables above, as sentences.

    Cheap enough to be a test and a first line in ``check_leagues``: a league
    that is not routed to a fixture feed cannot be forecast whatever Kalshi
    lists, and two leagues sharing a series ticker means one of them is a typo.
    """
    problems: list[str] = []
    for league in SERIES_BY_LEAGUE:
        if league not in COMPETITIONS:
            problems.append(f"{league} has a series but no ESPN competition in _taxonomy")
    seen: dict[str, str] = {}
    for league, series in SERIES_BY_LEAGUE.items():
        first = seen.get(series)
        if first:
            problems.append(f"{series} is claimed by both {first} and {league}")
        seen[series] = league
    for league, (series, _why) in UNMAPPED.items():
        if league in SERIES_BY_LEAGUE:
            problems.append(f"{league} is both mapped and unmapped")
        if series in seen:
            problems.append(f"{series} is both mapped (as {seen[series]}) and unmapped")
    for league in SEASONAL:
        if league in SERIES_BY_LEAGUE:
            problems.append(f"{league} is both mapped and seasonal")
    for series in NOT_FIXTURES:
        if series in seen:
            problems.append(f"{series} is both mapped (as {seen[series]}) and dismissed")
    return problems
