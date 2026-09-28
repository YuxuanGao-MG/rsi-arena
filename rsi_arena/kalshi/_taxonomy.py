"""Classify Kalshi series into sport, league and market type.

Kalshi encodes almost everything in the series ticker: ``KXNFLGAME`` is a pro
football game winner, ``KXNBA3PT`` is a player threes prop. There is no API
field for market type, so it is derived from the ticker and title. **Sport is
an API field** — ``series.tags`` carries it for 96% of sports series, so tags
are used first and the ticker regex is only a fallback. ``series.frequency``
separates fixtures (``custom``) from season futures (``annual``, ``one_off``).

Coverage on the 2026-08-17 sweep: 73% of sports series resolve to a named
league, 75% to a market type. The residue is the long tail of world leagues
and one-off event series; ``league == "UNKNOWN"`` is honest rather than wrong.

Derived from a sweep of all 13,029 series on 2026-08-17, of which 3,403 are
category ``Sports``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class Sport(str, Enum):
    FOOTBALL = "football"          # American
    BASKETBALL = "basketball"
    BASEBALL = "baseball"
    HOCKEY = "hockey"
    SOCCER = "soccer"
    TENNIS = "tennis"
    GOLF = "golf"
    COMBAT = "combat"              # boxing, MMA
    MOTORSPORT = "motorsport"
    ESPORTS = "esports"
    CRICKET = "cricket"
    DARTS = "darts"
    CHESS = "chess"
    OLYMPICS = "olympics"
    OTHER = "other"


class MarketType(str, Enum):
    GAME_WINNER = "game_winner"
    SPREAD = "spread"
    TOTAL = "total"
    TEAM_TOTAL = "team_total"
    BTTS = "btts"                  # both teams to score
    PERIOD = "period"              # 1st half / quarter / period markets
    EXACT_SCORE = "exact_score"
    PLAYER_PROP = "player_prop"
    SEASON_WINS = "season_wins"
    CHAMPIONSHIP = "championship"
    QUALIFY = "qualify"            # advance / make playoffs
    AWARD = "award"
    DRAFT = "draft"
    TRANSFER = "transfer"          # players, managers, coaches
    OTHER = "other"


# Kalshi's own ``tags`` field, which covers 96% of sports series. Far more
# reliable than pattern-matching a ticker, so it is consulted first.
TAG_TO_SPORT = {
    "soccer": Sport.SOCCER, "basketball": Sport.BASKETBALL,
    "football": Sport.FOOTBALL, "cfb": Sport.FOOTBALL,
    "baseball": Sport.BASEBALL, "hockey": Sport.HOCKEY,
    "tennis": Sport.TENNIS, "table tennis": Sport.TENNIS,
    "golf": Sport.GOLF, "esports": Sport.ESPORTS, "video games": Sport.ESPORTS,
    "mma": Sport.COMBAT, "ufc": Sport.COMBAT, "boxing": Sport.COMBAT,
    "motorsport": Sport.MOTORSPORT, "cycling": Sport.MOTORSPORT,
    "cricket": Sport.CRICKET, "darts": Sport.DARTS, "chess": Sport.CHESS,
    "olympics": Sport.OLYMPICS,
    "rugby": Sport.OTHER, "lacrosse": Sport.OTHER, "squash": Sport.OTHER,
    "aussie rules": Sport.OTHER,
}

# Kalshi ``frequency`` values that mean "one fixture" rather than "a season".
FIXTURE_FREQUENCIES = {"custom", "daily", "weekly"}



# Soccer competitions keyed by ticker stem, each with the ESPN slug that serves
# it. Every slug here was validated against the live ESPN scoreboard endpoint on
# 2026-08-17; four candidates (Korean, Egyptian, Polish and Canadian top flights)
# were dropped because ESPN genuinely does not carry them.
#
# Matched by prefix, not regex. Ticker stems are concatenated words, so a
# pattern like ``\bMLS\b`` never fires on ``MLSGAME`` — the trailing word
# boundary has nothing to match against.
SOCCER_STEMS: dict[str, tuple[str, str]] = {
    # England
    "EPL": ("EPL", "eng.1"), "FACUP": ("FA_CUP", "eng.fa"),
    "EFLCUP": ("EFL_CUP", "eng.league_cup"), "ENGCS": ("ENG_SHIELD", "eng.charity"),
    "EFLL1": ("EFL_L1", "eng.3"), "ENGNL": ("ENG_NL", "eng.5"),
    "EWSL": ("EWSL", "eng.w.1"), "EFL": ("EFL", "eng.2"),
    # Spain, Italy, Germany, France
    "LALIGA2": ("LALIGA2", "esp.2"), "LALIGA": ("LALIGA", "esp.1"),
    "COPADELREY": ("COPA_DEL_REY", "esp.copa_del_rey"),
    "SERIEAW": ("SERIEA_W", "ita.w.1"), "SERIEA": ("SERIEA", "ita.1"),
    "SERIEB": ("SERIEB", "ita.2"),
    "SERIECSCUP": ("SERIEC_SUPERCUP", "ita.3"),
    "SERIECCUP": ("SERIEC_CUP", "ita.3"), "SERIEC": ("SERIEC", "ita.3"),
    "COPPAITALIA": ("COPPA_ITALIA", "ita.coppa_italia"),
    "ITASUPERCUP": ("ITA_SUPERCUP", "ita.super_cup"),
    "ESPSUPERCUP": ("ESP_SUPERCUP", "esp.super_cup"),
    "BUNDESLIGA2": ("BUNDESLIGA2", "ger.2"), "BUNDESLIGA": ("BUNDESLIGA", "ger.1"),
    "GER3L": ("GER_3L", "ger.3"), "DFBPOKAL": ("DFB_POKAL", "ger.dfb_pokal"),
    "GERSC": ("GER_SUPERCUP", "ger.super_cup"),
    "LIGUE2": ("LIGUE2", "fra.2"), "LIGUE1": ("LIGUE1", "fra.1"),
    "COUPEDEFRANCE": ("COUPE_DE_FRANCE", "fra.coupe_de_france"),
    "FRASUPERCUP": ("FRA_SUPERCUP", "fra.super_cup"),
    # UEFA
    "UCLW": ("UCL_W", "uefa.wchampions"), "UCL": ("UCL", "uefa.champions"),
    "UEL": ("UEL", "uefa.europa"), "UECL": ("UECL", "uefa.europa.conf"),
    "UEFASC": ("UEFA_SUPERCUP", "uefa.super_cup"), "UEFANL": ("UEFA_NATIONS", "uefa.nations"),
    # Americas
    "MLSAST": ("MLS_ALLSTAR", "usa.1"), "MLS": ("MLS", "usa.1"),
    "NWSL": ("NWSL", "usa.nwsl"),
    "USOPENCUP": ("US_OPEN_CUP", "usa.open"), "USLCUP": ("USL_CUP", "usa.usl.l1.cup"),
    "USL": ("USL", "usa.usl.1"), "NCAAMSOCCER": ("NCAA_SOCCER", "usa.ncaa.m.1"),
    "LIGAEXP": ("LIGA_EXPANSION", "mex.2"), "LIGAMX": ("LIGAMX", "mex.1"),
    "BRASILEIROB": ("BRASILEIRAO_B", "bra.2"), "BRASILEIROC": ("BRASILEIRAO_C", "bra.3"),
    "BRASILEIRAOB": ("BRASILEIRAO_B", "bra.2"), "BRASILEIRO": ("BRASILEIRAO", "bra.1"),
    "COPADOBRASIL": ("COPA_DO_BRASIL", "bra.copa_do_brazil"),
    "ARGNACB": ("ARGENTINA2", "arg.2"), "ARGPREMDIV": ("ARGENTINA", "arg.1"),
    "URYPD": ("URUGUAY", "uru.1"),
    "CHILEAN": ("CHILE", "chi.1"), "CHLLDP": ("CHILE", "chi.1"),
    "COLOMBIAN": ("COLOMBIA", "col.1"),
    "DIMAYOR": ("COLOMBIA", "col.1"), "PERLIGA1": ("PERU", "per.1"),
    "ECULP": ("ECUADOR", "ecu.1"), "VENFUTVE": ("VENEZUELA", "ven.1"),
    "BOLPDIV": ("BOLIVIA", "bol.1"), "APFDDH": ("PARAGUAY", "par.1"),
    "CANPL": ("CANADA", "can.1"),
    "CONMEBOLLIB": ("LIBERTADORES", "conmebol.libertadores"),
    "CONMEBOLSUD": ("SUDAMERICANA", "conmebol.sudamericana"),
    "CONCACAFCCUP": ("CONCACAF_CL", "concacaf.champions"),
    "CONCACAFCL": ("CONCACAF_CL", "concacaf.champions"),
    "CONCACAFNL": ("CONCACAF_NL", "concacaf.nations.league"),
    "LEAGUESCUP": ("LEAGUES_CUP", "concacaf.leagues.cup"),
    # Rest of Europe
    "EREDIVISIEW": ("EREDIVISIE_W", "ned.w.1"), "EREDIVISIE": ("EREDIVISIE", "ned.1"),
    "EERSTEDIV": ("NED2", "ned.2"), "TWEEDEDIV": ("NED3", "ned.3"),
    "KNVBCUP": ("KNVB_CUP", "ned.cup"),
    "LIGAPORTUGAL": ("PRIMEIRA", "por.1"), "TACAPORT": ("TACA_PORTUGAL", "por.taca.portugal"),
    "SCOTTISHPREM": ("SCOTTISH", "sco.1"), "SCOCUP": ("SCOTTISH_CUP", "sco.tennents"),
    "BELGIANPL": ("BELGIUM", "bel.1"),
    "SUPERLIG": ("TURKEY", "tur.1"), "TFF1LIG": ("TURKEY2", "tur.2"),
    "SLGREECE": ("GREECE", "gre.1"), "SWISSLEAGUE": ("SWITZERLAND", "sui.1"),
    "AUSTRIANBL": ("AUSTRIA", "aut.1"), "DENSUPERLIGA": ("DENMARK", "den.1"),
    "ALLSVENSKAN": ("SWEDEN", "swe.1"), "ETTAN": ("SWEDEN2", "swe.2"),
    "ELITESERIEN": ("NORWAY", "nor.1"), "EKSTRAKLASA": ("POLAND", "pol.1"),
    "CZEFL": ("CZECH", "cze.1"), "CZEFNL": ("CZECH2", "cze.2"),
    "HNL": ("CROATIA", "cro.1"), "SRBSL": ("SERBIA", "srb.1"),
    "SVK2L": ("SLOVAKIA2", "svk.2"), "SVNPL": ("SLOVENIA", "svn.1"),
    "ISRPLCUP": ("ISRAEL_PL_CUP", "isr.1"), "ISRNLCUP": ("ISRAEL_NL_CUP", "isr.2"),
    "ISRSCUP": ("ISRAEL_STATE_CUP", "isr.1"), "ISRSUPCUP": ("ISRAEL_SUPERCUP", "isr.1"),
    "ISRPL": ("ISRAEL", "isr.1"), "ISRNL": ("ISRAEL2", "isr.2"),
    "FINCUP": ("FINLAND_CUP", "fin.cup"), "FINYL": ("FINLAND2", "fin.2"),
    "FROCUP": ("FAROE_CUP", "fro.cup"), "FROPL": ("FAROE", "fro.1"),
    "GRECUP": ("GREECE_CUP", "gre.cup"), "SRBCUP": ("SRB_CUP", "srb.1"),
    "SVKCUP": ("SVK_CUP", "svk.cup"), "SVNCUP": ("SVN_CUP", "svn.1"),
    "LVAVIR": ("LATVIA", "lva.1"),
    # Asia, Africa, Oceania
    "JLEAGUE": ("JLEAGUE", "jpn.1"), "J2LEAGUE": ("JLEAGUE2", "jpn.2"),
    "KLEAGUE": ("KLEAGUE", "kor.1"), "K2LEAGUE": ("KLEAGUE2", "kor.2"),
    "CHNSL": ("CHINA", "chn.1"), "CHNL1": ("CHINA2", "chn.2"),
    "SAUDIPL": ("SAUDI", "ksa.1"), "UAEPL": ("UAE", "uae.1"),
    "QSTARS": ("QATAR", "qat.1"),
    "THAIL1": ("THAILAND", "tha.1"), "IDNSL": ("INDONESIA", "idn.1"),
    "MYSL": ("MALAYSIA", "mys.1"), "SGPPL": ("SINGAPORE", "sgp.1"),
    "VLEAGUE1": ("VIETNAM", "vie.1"), "EGYPL": ("EGYPT", "egy.1"),
    "INDIANSL": ("INDIA", "ind.1"), "ALEAGUE": ("A_LEAGUE", "aus.1"),
    "ASEAN": ("ASEAN", "aff.championship"),
    # AFCAC is the AFC *Asian Cup*, a national-team tournament, and used to be
    # mapped onto the AFC Champions League — a club competition on a different
    # continent's calendar. They are two series and two ESPN competitions.
    "AFCCL": ("AFC_CL", "afc.champions"), "AFCAC": ("AFC_ASIAN_CUP", "afc.asian.cup"),
    "AFCON": ("AFCON", "caf.nations"), "FIFAW": ("FIFA_WWC", "fifa.wwc"),
    "CLUBWC": ("CLUB_WC", "fifa.cwc"),
    "BALLERLEAGUE": ("BALLER_LEAGUE", "nonfifa"),
    # Friendlies. Clubs and national teams are different ESPN competitions and
    # different Kalshi series, so they are different leagues here.
    "INTLFRIENDLY": ("INTL_FRIENDLY", "fifa.friendly"),
    "CLUBF": ("CLUB_FRIENDLY", "club.friendly"),
}

# Competitions whose fixtures ESPN splits over more than one competition feed.
#
# A cup's qualifying rounds are a different ESPN competition from the cup:
# ``KXFACUPGAME`` trades the FA Cup all season, but the September rounds are
# served by ``eng.fa_qual`` and only the November ones by ``eng.fa``. One slug
# per league therefore loses months of a competition, silently — the Conference
# League's whole August was invisible for exactly this reason.
#
# The scoreboard is the only lookup that needs the right competition. ESPN's
# ``summary?event=<id>`` endpoint is slug-agnostic — probed on 2026-09-27, a
# Conference League qualifier's summary came back identically through
# ``uefa.europa.conf_qual``, ``uefa.europa.conf``, ``uefa.champions`` and even
# ``eng.1`` — so a timeline needs no entry here, and one canonical slug per
# league in SOCCER_STEMS remains correct.
EXTRA_SCOREBOARD_SLUGS: dict[str, tuple[str, ...]] = {
    "UCL": ("uefa.champions_qual",),
    "UEL": ("uefa.europa_qual",),
    "UECL": ("uefa.europa.conf_qual",),
    "UCL_W": ("uefa.wchampions_qual",),
    "FA_CUP": ("eng.fa_qual",),
    "AFCON": ("caf.nations_qual",),
    # "FIFA Women's Game" is currently the 2027 Women's World Cup's European
    # qualifying group stage, which ESPN serves as its own competition.
    "FIFA_WWC": ("fifa.wworldq.uefa", "fifa.wwcq.ply"),
}

# Longest stem first, so LIGAMX is not shadowed by LIGA-prefixed neighbours.
_COMPETITION_ORDER = sorted(SOCCER_STEMS, key=len, reverse=True)


def match_competition(stem: str) -> tuple[str, str] | None:
    """Resolve a ticker stem to ``(league, espn_slug)`` by longest prefix.

    A digit immediately after the stem means a different division, not another
    market type on the same one: ``KXLALIGA2GAME`` is the Spanish second tier,
    and reading it as La Liga swept 36 second-division markets into the top
    flight's sweep. That was enough to break linking for the whole league —
    the reserve side listed as "Real Sociedad B" claimed the ``RSO`` code, so
    Real Madrid against Real Sociedad scored too low to link and was never
    adopted, silently, for a whole evening.

    A digit followed by ``H`` is a half, not a division — ``1HTOTAL`` and
    ``2HSPREAD`` are ordinary market types on the same competition. It is the
    bare digit that marks a tier: ``2GAME``, ``2SPREAD``, ``2TOTAL``.
    """
    for key in _COMPETITION_ORDER:
        if not stem.startswith(key):
            continue
        tail = stem[len(key):]
        if tail[:1].isdigit() and tail[1:2] != "H":
            continue
        return SOCCER_STEMS[key]
    return None


# Non-soccer leagues, each one ESPN endpoint. ESPN keys most sports by league —
# ``basketball/nba`` is the whole NBA — but keys soccer by competition, which is
# why soccer needs one entry per tournament above and everything else needs one
# entry here.
NON_SOCCER_LEAGUES: dict[str, tuple[Sport, str]] = {
    "NFL": (Sport.FOOTBALL, "football/nfl"),
    "NCAAF": (Sport.FOOTBALL, "football/college-football"),
    "NBA": (Sport.BASKETBALL, "basketball/nba"),
    "WNBA": (Sport.BASKETBALL, "basketball/wnba"),
    "NCAAB": (Sport.BASKETBALL, "basketball/mens-college-basketball"),
    "INTLBASKET": (Sport.BASKETBALL, "basketball/nba"),
    "MLB": (Sport.BASEBALL, "baseball/mlb"),
    "NHL": (Sport.HOCKEY, "hockey/nhl"),
    "TENNIS": (Sport.TENNIS, "tennis/atp"),
    "GOLF": (Sport.GOLF, "golf/pga"),
    "COMBAT": (Sport.COMBAT, "mma/ufc"),
    "MOTORSPORT": (Sport.MOTORSPORT, "racing/f1"),
    "CRICKET": (Sport.CRICKET, "cricket/league"),
}


def _build_registry() -> dict[str, tuple[Sport, str]]:
    """One league -> (sport, ESPN path) map, soccer and everything else.

    Previously soccer routing lived in two places — a competition table here and
    a league table in gamestate — which agreed by luck and nothing else. This is
    the single source of truth; gamestate reads it rather than keeping a copy.
    """
    registry: dict[str, tuple[Sport, str]] = dict(NON_SOCCER_LEAGUES)
    for league, slug in SOCCER_STEMS.values():
        registry.setdefault(league, (Sport.SOCCER, f"soccer/{slug}"))
    return registry


COMPETITIONS: dict[str, tuple[Sport, str]] = _build_registry()


def resolve_league(value: str) -> str | None:
    """Map anything league-shaped onto a canonical league code.

    Callers guess, and reasonably: a Kalshi ticker reads ``KXSAUDIPLGAME``, so
    ``SAUDIPL`` is the obvious code to try — but the canonical one is ``SAUDI``.
    Rejecting that is a usability bug, not a correct validation, so this accepts
    the ticker stem, the ESPN slug and ordinary case variants.
    """
    if not value:
        return None
    # The canonical code exactly as written, before anything is stripped out of
    # it. Stripping first meant a code containing an underscore could fail to
    # resolve to itself: ``UEFA_NATIONS`` became ``UEFANATIONS``, which is not a
    # key and is not the ``UEFANL`` ticker stem either, so it came back None —
    # and ``espn_scoreboard`` raises on None. The Nations League was therefore
    # unreachable twice over: the series ticker was guessed wrong, and the one
    # league code that names the competition could not be routed to its feed.
    raw = value.strip().upper()
    if raw in COMPETITIONS:
        return raw
    probe = raw.replace("-", "").replace("_", "")
    if probe in COMPETITIONS:
        return probe

    # A Kalshi series stem, e.g. SAUDIPL -> SAUDI, LIGAPORTUGAL -> PRIMEIRA.
    for stem, (league, _slug) in SOCCER_STEMS.items():
        if probe.startswith(stem) or stem.startswith(probe):
            return league

    # An ESPN slug, e.g. "eng.1" or "soccer/eng.1".
    tail = value.strip().lower().split("/")[-1]
    for league, (_sport, path) in COMPETITIONS.items():
        if path.split("/")[-1] == tail:
            return league

    for league in COMPETITIONS:
        flat = league.replace("_", "")
        if probe.startswith(flat) or flat.startswith(probe):
            return league
    return None


def espn_path(league: str) -> str | None:
    """ESPN ``sport/league`` path for a league code, or None if unwired."""
    hit = COMPETITIONS.get(league)
    return hit[1] if hit else None


# League detection. Order matters — longer, more specific stems first.
_LEAGUE_PATTERNS: list[tuple[str, str, Sport]] = [
    # American football
    (r"\bNCAAF|CFB|COLLEGEFOOT", "NCAAF", Sport.FOOTBALL),
    (r"\bNFL", "NFL", Sport.FOOTBALL),
    # Basketball
    (r"\bWNBA", "WNBA", Sport.BASKETBALL),
    (r"\bNCAAM?B|NCAAW?B|MARMAD|CBB", "NCAAB", Sport.BASKETBALL),
    (r"\bNBA", "NBA", Sport.BASKETBALL),
    (r"\bEUROLEAGUE|\bDBB|\bVBA", "INTLBASKET", Sport.BASKETBALL),
    # Baseball
    (r"\bMLB|\bNLGAME|\bALGAME|\bNL[A-Z]*WEST|\bAL[A-Z]*WEST|WORLDSERIES", "MLB", Sport.BASEBALL),
    (r"\bKBO|\bNPB", "INTLBASEBALL", Sport.BASEBALL),
    # Hockey
    (r"\bNHL|STANLEYCUP", "NHL", Sport.HOCKEY),
    # Soccer — domestic
    (r"\bEPL|PREMIERLEAGUE", "EPL", Sport.SOCCER),
    (r"\bLALIGA", "LALIGA", Sport.SOCCER),
    (r"\bSERIEA", "SERIEA", Sport.SOCCER),
    (r"\bBUNDESLIGA", "BUNDESLIGA", Sport.SOCCER),
    (r"\bLIGUE1|FRALIGUE", "LIGUE1", Sport.SOCCER),
    (r"\bMLS", "MLS", Sport.SOCCER),
    (r"\bNWSL", "NWSL", Sport.SOCCER),
    (r"\bEFL|CHAMPIONSHIP1H", "EFL", Sport.SOCCER),
    (r"\bBRASILEIRO|COPADOBRAS", "BRASILEIRAO", Sport.SOCCER),
    (r"\bEREDIVISIE|LIGAPORTUG|SCOTTISHPR|EKSTRAKLAS|ALLSVENSKA|ELITESERIE"
     r"|DENSUPERLI|SWISSLEAGU|ARGPREMDIV|DIMAYOR|USL\b|EGYPL|URYPD|CANPL"
     r"|EERSTEDIV|ASEAN|VENFUTVE|\bJ1LEAGUE|\bJ2LEAGUE|KLEAGUE|ALEAGUE"
     r"|SUPERLIG|BELPRO|AUTBUND|GREEKSL|TURKSL|CHISUPER|MEXLIGA|LIGAMX",
     "OTHERLEAGUE", Sport.SOCCER),
    # Soccer — cups and international
    (r"\bUEFACL|CHAMPIONSLEAGUE", "UCL", Sport.SOCCER),
    (r"\bUEL\d*|EUROPALEAGUE|\bUECL|CONFERENCELEAGUE|UEFANL|NATIONSLEAGUE",
     "UEFA_OTHER", Sport.SOCCER),
    (r"\bAFCON|ASIANCUP|GOLDCUP|COPAAMERICA|\bWC[A-Z]*|WORLDCUP|EURO20|EURO24",
     "INTERNATIONAL", Sport.SOCCER),
    (r"\bCONMEBOL|CONCACAF|LEAGUESCUP|CLUBWC|FINALISSIM|INTLFRIEND"
     r"|COPPAITALI|COUPEDEFRA|COPADELREY|[A-Z]{3}SUPERCU|FINCUP|TACAPORT"
     r"|CLUBF|SOCCERTRANSFER", "CUP", Sport.SOCCER),
    # Individual
    (r"\bATP|\bWTA|TENNIS|EXHIBITIONMEN|\bFO(MEN|WOMEN)|USOPEN|WIMBLEDON"
     r"|AUSOPEN|ROLANDGARROS", "TENNIS", Sport.TENNIS),
    (r"\bPGA|LIVGOLF|DPWORLDTOU|RYDERCUP|MASTERS", "GOLF", Sport.GOLF),
    (r"\bUFC|\bBOXING|\bMMA", "COMBAT", Sport.COMBAT),
    (r"\bF1|FORMULA|NASCAR|MOTOGP|INDYCAR|LEMANS|WRC\b", "MOTORSPORT", Sport.MOTORSPORT),
    (r"VALORANT|\bLOL\b|\bCSGO|\bCS2\b|\bDOTA|ESPORT|\bESL[A-Z]*|OVERWATCH"
     r"|ROCKETLEAGUE|APEX", "ESPORTS", Sport.ESPORTS),
    (r"\bIPL|CRICKET|\bT20|\bBBL|TESTMATCH", "CRICKET", Sport.CRICKET),
    (r"\bPDC|DARTS", "DARTS", Sport.DARTS),
    (r"\bCHESS", "CHESS", Sport.CHESS),
    (r"OLYMPIC|WINTERGAMES|SUMMERGAMES|PARALYMP", "OLYMPICS", Sport.OLYMPICS),
    (r"SQUASH|WRESTL|VOLLEY|RUGBY|\bNRL\b|\bAFL\b|HANDBALL|CYCLING|SNOOKER",
     "OTHER", Sport.OTHER),
]

# Market type detection, matched against ticker and title together.
_TYPE_PATTERNS: list[tuple[str, MarketType]] = [
    (r"1H|2H|FIRSTHALF|QUARTER|PERIOD|1STQ|1ST HALF|2ND HALF|FIRST HALF"
     r"|1ST QUARTER|FIRST PERIOD", MarketType.PERIOD),
    (r"EXACT(WINS|SCORE)?|FINALSEXACT|CORRECT SCORE|EXACT SCORE", MarketType.EXACT_SCORE),
    (r"TEAMTOTAL|TEAM TOTAL", MarketType.TEAM_TOTAL),
    (r"TOTAL|OVERUNDER|ROUNDS|MAPS|POINT TOTAL|OVER/UNDER", MarketType.TOTAL),
    (r"SPREAD|HANDICAP|MARGIN", MarketType.SPREAD),
    (r"BTTS|BOTH TEAMS", MarketType.BTTS),
    (r"WINS$|SEASONR|REGSEASON|WINTOTAL", MarketType.SEASON_WINS),
    (r"ADVANCE|QUALIF|MAKEPLAYOFF|SEED|TO REACH|KNOCKOUT", MarketType.QUALIFY),
    (r"CHAMP|TITLE|CUP|TOP ?\d*|LEADER|FINALS|DIVISION|CONFERENCE"
     r"|\b(AL|NL)(EAST|WEST|CENTRAL)\b|SEASON WINNER|WIN THE", MarketType.CHAMPIONSHIP),
    (r"MVP|MOTY|COMEBACK|ALLSTAR|ROTY|AWARD|GOLDGLOVE", MarketType.AWARD),
    (r"DRAFT", MarketType.DRAFT),
    (r"TRANSFER|NEXTMANAGE|NEXTTEAM|COACHON|NEXT TEAM|NEXT MANAGER", MarketType.TRANSFER),
    (r"3PT|GOAL|FIRSTTD|RSHYDS|PASSYDS|RECYDS|STRIKEOUT|\bHR\b|\bSB\b"
     r"|POINTS|ASSISTS|REBOUND|SAVES|PLAYER|SCORER|HRDERBY", MarketType.PLAYER_PROP),
    (r"GAME|MONEYLINE|MATCH|WINNER", MarketType.GAME_WINNER),
]


@dataclass(frozen=True)
class SeriesClass:
    """Classification of one Kalshi series."""

    ticker: str
    sport: Sport
    league: str
    market_type: MarketType
    title: str = ""
    frequency: str = ""
    sport_source: str = "regex"   # "tag" when it came from Kalshi's own field
    espn_slug: str = ""           # set when a game-state feed is known

    @property
    def is_fixture(self) -> bool:
        """A *hint* that the series covers single fixtures rather than a season.

        **Not authoritative, and must not be used as a filter on its own.**
        Kalshi's ``frequency`` is the only signal available on a series object,
        and ``custom`` is a catch-all: it covers per-game series and season
        futures alike, so the World Series winner market reads as a fixture
        here. It is right far more often than not, which is exactly what makes
        it dangerous alone.

        The definitive test needs an event ticker, since only a fixture encodes
        a date and two team codes — see :func:`linking.is_fixture_event`, which
        :meth:`discovery.Discovery.whats_bettable` applies for free on markets
        it has already fetched.
        """
        if self.frequency:
            return self.frequency in FIXTURE_FREQUENCIES
        return self.is_game_level

    @property
    def is_game_level(self) -> bool:
        """True for market types tied to a single fixture."""
        return self.market_type in {
            MarketType.GAME_WINNER,
            MarketType.SPREAD,
            MarketType.TOTAL,
            MarketType.TEAM_TOTAL,
            MarketType.BTTS,
            MarketType.PERIOD,
            MarketType.EXACT_SCORE,
            MarketType.PLAYER_PROP,
        }


def _strip_prefix(ticker: str) -> str:
    return ticker[2:] if ticker.startswith("KX") else ticker


def classify_series(ticker: str, title: str = "", category: str = "",
                    tags: list[str] | None = None,
                    frequency: str = "") -> SeriesClass:
    """Classify a series ticker into sport, league and market type.

    ``category`` is Kalshi's own field; when it is present and not ``Sports``
    the series is returned as OTHER/OTHER so non-sports series can be filtered
    cheaply without a second pass.
    """
    if category and category != "Sports":
        return SeriesClass(ticker, Sport.OTHER, "NONSPORT", MarketType.OTHER,
                           title, frequency, "category")

    stem = _strip_prefix(ticker).upper()
    hay = f"{stem} {title.upper()}".strip()

    # Kalshi's tag is authoritative for sport when present.
    tag_sport = None
    for tag in tags or []:
        hit = TAG_TO_SPORT.get(tag.strip().lower())
        if hit:
            tag_sport = hit
            break

    sport, league, slug = Sport.OTHER, "UNKNOWN", ""

    competition = match_competition(stem)
    if competition:
        league, slug = competition
        sport = Sport.SOCCER

    for pattern, lg, sp in ([] if competition else _LEAGUE_PATTERNS):
        if re.search(pattern, hay):
            sport, league = sp, lg
            break

    sport_source = "regex"
    if tag_sport:
        sport_source = "tag"
        if sport is Sport.OTHER or sport is not tag_sport:
            # The tag wins on sport. Keep a league only if it agrees.
            if sport is not tag_sport:
                league = league if sport is tag_sport else _generic_league(tag_sport, league)
            sport = tag_sport

    if sport is Sport.OTHER and re.search(
            r"\bBTTS\b|GOALSCORER|CLEAN ?SHEET|\bCORNERS?\b|\bGOALS?\b|BOTH TEAMS", hay):
        sport, league = Sport.SOCCER, "OTHERLEAGUE"

    market_type = MarketType.OTHER
    # A stem that *ends* in GAME names the fixture's winner, whatever else the
    # words in it say. The generic patterns test CHAMPIONSHIP before
    # GAME_WINNER, so every cup's match-winner series came out a championship —
    # KXFACUPGAME, KXEFLCUPGAME, KXCOPADELREYGAME, KXUSLCUPGAME — and
    # ``is_game_level`` was then False for all of them, which is the wrong answer
    # for a market that settles on one afternoon's result. Checked first because
    # the suffix is the exchange's own statement about what the series is.
    if stem.endswith("GAME"):
        market_type = MarketType.GAME_WINNER
    for pattern, mt in ([] if market_type is MarketType.GAME_WINNER else _TYPE_PATTERNS):
        # Match the ticker stem and the title separately: the stem is
        # concatenated words, so word-boundary anchors only work on the title.
        if re.search(pattern, stem) or re.search(pattern, title.upper()):
            market_type = mt
            break

    return SeriesClass(ticker, sport, league, market_type, title,
                       frequency, sport_source, slug)


def _generic_league(sport: Sport, current: str) -> str:
    """A per-sport catch-all so an unmatched league is still usable.

    Better than UNKNOWN: an agent filtering on ``sport`` still gets everything,
    and the label says which sport rather than nothing.
    """
    if current not in ("UNKNOWN", "NONSPORT"):
        return current
    return {
        Sport.SOCCER: "SOCCER_OTHER", Sport.BASKETBALL: "BASKET_OTHER",
        Sport.FOOTBALL: "FOOTBALL_OTHER", Sport.BASEBALL: "BASEBALL_OTHER",
        Sport.HOCKEY: "HOCKEY_OTHER", Sport.TENNIS: "TENNIS",
        Sport.GOLF: "GOLF", Sport.COMBAT: "COMBAT", Sport.ESPORTS: "ESPORTS",
        Sport.MOTORSPORT: "MOTORSPORT", Sport.CRICKET: "CRICKET",
        Sport.DARTS: "DARTS", Sport.CHESS: "CHESS", Sport.OLYMPICS: "OLYMPICS",
    }.get(sport, "UNKNOWN")


def classify_many(series: list[dict]) -> list[SeriesClass]:
    """Classify a list of series objects as returned by ``/series``."""
    return [
        classify_series(s.get("ticker", ""), s.get("title", ""),
                        s.get("category", ""), s.get("tags"), s.get("frequency", ""))
        for s in series
    ]


# Leagues for which a live game-state feed exists in gamestate.py.
#
# Every key of COMPETITIONS, by construction: gamestate derives ESPN_PATHS from
# it, so a league in the registry is a league with a feed. This used to be a
# hand-written list of fourteen, which said the Champions League was the only
# continental competition supported and the Nations League was not supported at
# all — neither of which was ever true of the code, and both of which were the
# kind of thing someone reads before deciding what to sweep.
#
# Whether a feed actually *answers* for a competition is a different question,
# and an empirical one: see ``_series.UNMAPPED`` for the ones ESPN lists and does
# not serve.
LIVE_STATE_SUPPORTED = frozenset(COMPETITIONS)
