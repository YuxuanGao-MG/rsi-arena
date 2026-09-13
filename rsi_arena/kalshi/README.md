# Kalshi data layer

The underscore modules here are copied verbatim from
`seantao97/rsi-arena/topics/kalshi/tools` (Apache-2.0, authored by YuxuanGao-MG).
They depend on the standard library only and talk to Kalshi's public read
endpoints and the fixture feeds.

| Module | Answers |
|---|---|
| `_client.py` | Rate-limited HTTP client for Kalshi's trade API |
| `_history.py` | Candlesticks, trades, settlement for any market, open or settled |
| `_quotes.py` | Live quotes and order books |
| `_gamestate.py` | Scores, situation and plays from official and ESPN feeds |
| `_timeline.py` | Game events and market candles on one clock |
| `_taxonomy.py` | Sport, league and market type for a series |
| `_linking.py` | Which fixture a Kalshi event is about |
| `_discovery.py` | What is bettable |
| `_coherence.py` | No-arbitrage checks across related markets |
| `_fees.py` | Fee schedule, breakeven, Kelly, CLV |
| `_credentials.py` | Optional credentials, only needed for portfolio endpoints |

`replay.py` is written here: three tools frozen at a past instant and the
match timeline that gives a replayed harness the score and clock as they stood.
