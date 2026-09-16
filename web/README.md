# web

A reader for what the harnesses actually did: the generations, the windows each
one was scored on, and the trace of any window — tools called, what came back,
the prompt, and the forecast with its driver and falsifier.

One file of HTML and a forty-line server. There is no build step and no
framework, because the page does three selects and rendering three tables is not
a reason to take on a toolchain.

## Running it

```bash
export SUPABASE_URL=https://<project>.supabase.co
export SUPABASE_ANON_KEY=<anon key>
python web/server.py            # http://localhost:8000
```

The anon key is injected at serve time rather than committed, so rotating it
does not mean editing HTML. It can only read: the tables carry row-level
security with a select-only policy, and the writer uses a different key.

## Where the data comes from

`scripts/publish_runs.py` pushes a run directory into Supabase. Run it after any
`optimize`, and after a `bench --trace` when the trajectories are worth keeping:

```bash
python scripts/publish_runs.py runs/gen1-floored
```

Tables live in an `rsi` schema and are read through views in `public`
(`rsi_runs`, `rsi_rollouts`, `rsi_traces`). The Supabase project is shared with
another arena, so exposing a second schema through the API would have changed
that arena's API too; views reach the same rows without touching anything anyone
else depends on.

## Why worst-first

The window list is ordered by skill ascending, always. A generation that scored
+0.04 tells you nothing you can act on. The windows it lost tell you what the
harness does when it is wrong, which is the only thing a rewrite can be aimed
at — and it is the same material GEPA reflects over.
