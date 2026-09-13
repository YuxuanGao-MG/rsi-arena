# Notes on seantao97/rsi-arena

Read at commit `e333dc4` (2026-09-11), read-only. Nothing was pushed, forked,
starred or commented on.

## Who built what

| Author | Commits | Scope |
|---|---|---|
| seantao97 | 6 (Aug 14–18) | Runtime `rsi_arena/`, backend `server/`, web app `web/`, test suite, README |
| YuxuanGao-MG | 43 (Aug 16–Sep 11) | `topics/` catalogue, Kalshi data layer and 40 tools, the declared `Tool` class (#43), eval restructure (#46–49), replay benchmark (#33, #50) |
| Yu Yi Ling | 2 | agent-review and trade-review topics |

Sean's contribution is the three PRs #10, #11, #15. Nothing from him since Aug 18.

## Architecture, bottom to top

**`core/`** — cache (SHA-256 of canonical request, single-flight), costs
(per-call `Cost` with a source of `reported|estimated|fixed|free`, a ledger,
a ceiling that refuses rather than queues, a bail-out reserve), token-bucket
rate limit and `Retry-After`-aware retry, span-tree trace, `{{key}}` templating
with a restricted condition evaluator.

**`llm/`** — one async OpenRouter client: retries, limits, cache, `json_schema`
structured outputs (sets `provider.require_parameters`, so unsupported models 503),
OpenRouter web plugin, streaming, cost taken from `usage.cost`.

**`api/`** — declarative `APISpec` / `Endpoint` / `Param`; `APIClient` executes
any spec with the same retry, limit, cache and cost path. SearchApi is the only
spec shipped.

**`agent/`**
- `Tool`: a declared class with `name`, `version`, `description`, `parameters`,
  `output_schema`, and one sync `get_tool_output(input) -> ToolOutput`.
  `ToolOutput` carries three views: `response` (sentence for the model),
  `raw_output` (structure for code), `raw_api_data` (upstream payload).
  Failures return, never raise. `Toolbox` is a dict by name.
- Steps: `PromptStep` (optional `tools`, `output_schema`, `memory`, `stream`;
  with tools it runs a model-driven loop up to `max_tool_iterations`),
  `ToolStep` (fixed call, args templated from state), `LoopStep` (`max_loops`,
  `until` expression, `until_prompt` model check). All write into one flat
  state dict under `output_key`.
- `Agent` = context + plan + tools + config. `to_dict()` writes context, config,
  plan and tool *names*; `from_dict(data, toolbox)` rebinds. That is the harness
  contract: JSON in, JSON out, tools supplied by the host.
- `AgentResult`: output, state, trace, `error_kind`
  (`max_spend|budget|provider|api|plan|other`), `bailed_out`.
  `max_spend_mode` spends a small reserve on one final answer from state.

**`evals/`** — `Eval(agent, eval_function, input)` → `EvalOutput(score,
comments, metadata, output, ground_truth)`. `Eval.score()` grades a recorded
run without re-running it. A separate scorer registry (`contains`, `regex`,
`llm_judge`, `all_of`, …) returns `Score`; `scored_by` bridges the two shapes.
`InMemoryEvalStore` only.

**`server/`** — FastAPI on :3600. SSE `run` and `battle`, `vote`, `leaderboard`
(win/loss/tie counts only), agents catalogue, evals routes. Battles and votes in
append-only SQLite. Blinding is server-side: the agent name never reaches the
browser. One shared LLM client per process so both sides of a battle share a
cache and a rate limit.

**`web/`** — Next.js 16 on :8050. Playground and Battle tabs, live trace tree,
cost ledger, vote bar.

## The Kalshi layer (yours)

- 40 declared tools in `topics/kalshi/tools/`, data layer behind underscore modules.
- One harness config, `kalshi-horizon-5m.json`: three `ToolStep`s
  (`market_quote`, `candlesticks`, `previous_trades`) then one schema
  `PromptStep` returning `delta_cents`, `half_width_cents`, `confidence`,
  `driver`, `falsifier`. Code, not the model, applies the change to the
  exchange mid.
- Replay: tools frozen at a past instant, so the five-minute answer already
  exists in candle history. Only those three tools can be frozen; anything that
  reads news or live game state cannot be replayed honestly.
- Benchmark: five EPL fixtures in `eval/benchmark.json`, windows every five
  minutes, scored by skill against no-change, pooled (sum of errors, not mean
  of ratios), reported as `0.5 + skill/2`.
- Measured so far: base harness skill about zero, run to run between −5% and
  +1%; 70–80% of forecasts echo the current mid; after round-trip fees almost
  no decision clears.

## Design commitments worth keeping

1. A harness is JSON: context, config, tool names, plan. Same file runs live
   or replayed because binding is by tool name.
2. One flat, serialisable state dict; every step reads and writes it.
3. Every call is priced; the ceiling refuses, it does not queue; a partial
   trace is kept as evidence; `error_kind` separates harness fault from infra.
4. Both sides of a comparison share a client, so the comparison is fair.
5. Verifiable score beats preference vote where both exist.

## What is described and not built

- Ratings. Votes are tallied as counts; no Elo or Bradley–Terry.
- Matchmaking. Battles are whatever the UI picks.
- The optimizer loop: population, selection, mutation, promotion. This is the
  thesis of the README and there is no code for it.
- Persistence beyond SQLite votes: eval results die with the process; no
  record of generations.
- Only the web-research and fermi samples are in the server catalogue. The
  Kalshi harness runs from the CLI only.

## Friction I would design around

- OpenRouter is the only model path, and structured outputs depend on
  provider support.
- Two verdict shapes (`Score` and `EvalOutput`) with an adapter between them.
- Benchmark noise (±5% skill run to run) is larger than any improvement seen
  so far. An optimizer has to beat noise, not just the baseline, which means
  paired comparisons on identical windows and a held-out set.
- The README's clone URL points at `Helpigent/rsi-arena`, not `seantao97`.
  Worth confirming which is canonical before reconvening.
