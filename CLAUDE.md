# rsi-arena

Harnesses that compete on Kalshi soccer forecasts, and a GEPA-driven loop that
rewrites the losers. Start with `HANDOFF.md`, then `README.md`, then
`docs/design.md`.

## Commands

```bash
pip install -e ".[dev]" && pytest          # offline; must stay green
export OPENROUTER_API_KEY=sk-or-...
rsi-arena windows                          # build the question set (no key)
rsi-arena bench --split holdout            # score one harness
rsi-arena optimize --run-dir runs/gen1     # one generation: baseline, search, gate
rsi-arena optimize --harness runs/gen1 --run-dir runs/gen2
rsi-arena show runs/gen2
```

## Layout

`rsi_arena/harness` (JSON contract, runner, model client) · `rsi_arena/kalshi`
(data layer, frozen tools) · `rsi_arena/loop` (Task protocol, GEPA adapter,
gate, generation records; topic-agnostic) · `rsi_arena/topics/kalshi_horizon`
(windows, score, feedback) · `rsi_arena/topics/_common` (the move metric with
its unit as a parameter, thinning, live grading; shared by every five-minute
topic) · `rsi_arena/topics/__init__.py` (`TopicSpec`: what each topic runs on)
· `rsi_arena/cli.py`.

## Rules

- Tests are offline: no key, no network. Fakes live in `tests/conftest.py`.
- Frozen tools never see past the instant. Split by fixture, never by window.
- A failed run scores as silence. Promotion goes through `loop/gate.py` only.
- `loop/` must not import from `topics/`.
- No model call has been made yet as of the handoff; `HANDOFF.md` lists what to check first.
