# Open-source frameworks for the self-improvement loop

Surveyed 2026-09-13. The question: which verified, maintained codebase should the
loop be built on, so that we write the Kalshi evaluator and the harness contract
and not the optimizer. Stars and licenses read off GitHub the same day.

## What our problem looks like to an optimizer

- **Candidate**: a harness. Today that is JSON: a context string, a plan of
  prompt/tool/loop steps, a tool list, a config. It could also be a Python file.
- **Evaluator**: replay a harness over fixed `(ticker, instant)` windows with
  tools frozen at that instant; score each window by skill against no-change;
  cost is known per window. Expensive (model calls), noisy (±5% skill run to
  run), and per-instance scores carry real information about *where* a harness
  fails.
- **Budget**: hundreds of evaluations, not tens of thousands.
- **Hazards**: the optimizer must not be able to reach a tool that sees the
  future, and it must not be promoted on training windows alone.

That rules out RL-style optimizers and favours reflective, trace-reading,
Pareto-aware search with a strict held-out gate.

## Candidates

| Framework | Shape | License · stars | Fit |
|---|---|---|---|
| [GEPA](https://github.com/gepa-ai/gepa) | Reflective text evolution over a dict of text components; per-instance Pareto frontier; reads traces | MIT · 6.5k | **Best fit.** Candidate = our harness JSON as components. Evaluator returns score + feedback per window. Budget 100–500 evals is the design point. Eight built-in adapters; `optimize_anything()` for a minimal start, `GEPAAdapter` for the real thing |
| [ShinkaEvolve](https://github.com/SakanaAI/ShinkaEvolve) | Evolves a Python program between `EVOLVE-BLOCK` markers; LLM-ensemble bandit; islands; novelty rejection; SQLite archive; WebUI | Apache-2.0 · 1.4k | **Second engine.** Right if the harness becomes a Python file rather than JSON. Evaluator contract is `combined_score` + `public`/`private` metrics + text feedback. Claude and headless Claude Code supported |
| [OpenEvolve](https://github.com/codelion/openevolve) | AlphaEvolve reimplementation; MAP-Elites + islands; cascade evaluation; artifacts fed back to the prompt | Apache-2.0 · 7.4k | Solid alternative to ShinkaEvolve. Cascade evaluation (cheap filter, then full benchmark) is the one feature the others lack and that our cost profile wants. Any OpenAI-compatible endpoint, so OpenRouter works |
| [Meta-Harness](https://github.com/stanford-iris-lab/meta-harness) | Outer loop over harness *code*: seed incumbent → coding agent proposes with filesystem access to all prior candidates, traces and scores → validate → held-out-protected score → Pareto-merge on quality and cost | MIT · 1.6k | **The loop discipline to copy**, even if not the code. Proposer is Claude Code; heavy per step (up to 10M tokens). Two reference tasks only; new task needs a `domain_spec.md` and proposer wrapper |
| [Self-Harness](https://github.com/qzzqzzb/Self-Harness) | Weakness mining from traces → bounded harness edits → accept only with **no regression on held-in and held-out** | 103 stars, single commit | Too early to depend on. The acceptance rule is the one we should adopt verbatim |
| [DSPy](https://github.com/stanfordnlp/dspy) | Programs of modules with signatures; optimizers MIPROv2, SIMBA, `dspy.GEPA` | MIT · mature | Mature and well-trodden, but it wants the harness as a DSPy program. Our harness is tool steps and a frozen toolbox; wrapping that in DSPy adds a layer we would fight. Worth it only if we drop the JSON contract |
| [EvoAgentX](https://github.com/EvoAgentX/EvoAgentX) | Whole agent framework with TextGrad, AFlow, MIPRO, EvoPrompt optimizers | MIT · 3.3k | Replaces the runtime wholesale. More framework than we need |
| [Trace](https://github.com/microsoft/Trace) | Computation-graph primitives; OptoPrime, TextGrad, OPRO; joint prompt + code optimisation | MIT · beta | Interesting, less active. Not a first choice |
| [DGM](https://github.com/jennyzzt/dgm) · [HGM](https://github.com/metauto-ai/HGM) | Self-modifying coding agents scored on SWE-bench in Docker | Apache-2.0 | Wrong shape: the agent rewrites its own codebase against a coding benchmark. HGM's clade-metaproductivity idea (score a candidate by its descendants) is worth remembering for selection |
| [AEL](https://github.com/WujiangXu/AEL) | Bandit over memory-retrieval policies plus slow reflection; sequential portfolio benchmark | MIT · 7 | Closest domain to ours, far too early. One finding transfers: the simplest variant (reflection + memory) won, every extra component hurt |
| [Agent Lightning](https://github.com/microsoft/agent-lightning) | RL and fine-tuning of agents | MIT | Trains weights. Not this phase |

Reading list that organises all of this:
[Awesome-Harness-Self-Improvement](https://github.com/leezythu/Awesome-Harness-Self-Improvement).

## Verified structures on the forecasting side

These evaluate event-level probabilities, not five-minute price moves, but the
scoring discipline transfers: Brier plus calibration plus realised returns,
held out by time, never by random split.

- [Prophet Arena](https://www.prophetarena.co/) (UChicago SIGMA Lab): continuous live benchmark on Kalshi events, multi-horizon, modular pipeline with a fixed shared context per event. Site blocked from this session; paper is arXiv 2510.17638.
- Prediction Arena (arXiv 2604.07355): models trade real capital on Kalshi and Polymarket.
- PolyBench (arXiv 2604.14199) and KalshiBench: point-in-time snapshots of binary markets; KalshiBench reports systematic miscalibration.
- [Metaculus forecasting-tools](https://github.com/Metaculus/forecasting-tools): bot template with research, reasoning, aggregation, and a cost manager. Useful as a reference shape for a research-driven harness, not for the horizon task.
- Recent harness-evolution papers with a trading flavour: AEL (above), Harness-1, HarnessX, HarnessForge, "Harness Updating Is Not Harness Benefit" (arXiv 2605.30621), which argues for measuring benefit rather than change. Keep that last one in mind when reporting.

## Recommendation

**Build the loop on GEPA, with a Meta-Harness/Self-Harness acceptance gate.**

1. Candidate = the harness JSON split into components: `context`, `plan`
   (JSON text), `tools` (comma-separated names). GEPA mutates components
   individually, which is what we want: a rewrite of the prompt should not
   silently rewrite the plan.
2. Implement a `GEPAAdapter`, not `optimize_anything()`. `evaluate()` takes a
   batch of windows and a candidate, runs the replay, and returns per-window
   scores plus trajectories. `make_reflective_dataset()` turns each window into
   inputs (book, path, tape, game state), the harness's output, and feedback
   (what the market printed, error versus no-change, cost). That is exactly the
   text a rewriter needs and exactly what GEPA's reflection reads.
3. Pass `objective_scores` with `skill` and `cost`, so the Pareto frontier
   keeps cheap-and-decent harnesses alongside expensive-and-better ones.
4. Acceptance gate outside GEPA: split fixtures into held-in and held-out;
   promote a candidate only if it does not regress on either and its held-out
   skill improvement exceeds the measured noise band. This is Self-Harness's
   rule with Meta-Harness's held-out protection.
5. Binding is by tool name against the frozen replay toolbox. A candidate that
   names a tool the box does not have fails at load, so the optimizer cannot
   reach for live game state or news and read the future.
6. Keep ShinkaEvolve as the second engine if the harness moves to Python. Both
   engines share the same evaluator, so switching is a wrapper.

Why not the others: DSPy would mean re-expressing the harness in its module
system; EvoAgentX would replace the runtime; DGM and HGM optimise coding agents
against coding benchmarks; Meta-Harness is the right loop but at 10M tokens a
step it is a coding-agent proposer, not a population optimizer.

## What this left for us to write (now written)

- The replay evaluator as a GEPA adapter (the benchmark exists in Sean's repo
  and is yours; it needs porting or importing).
- A runner that executes harness JSON. Decision A in `design.md` still stands:
  reuse `rsi_arena` as a dependency, or write a slim runner that keeps the
  contract.
- The acceptance gate and the generation record.
- Nothing else. The optimizer, the archive and the reflection prompt come from
  the framework.
