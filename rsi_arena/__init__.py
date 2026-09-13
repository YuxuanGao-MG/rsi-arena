"""RSI Arena: harnesses that compete, and a loop that rewrites the losers.

``harness``  the JSON contract a harness is written in, and the runner
``kalshi``   the Kalshi and fixture data layer, and tools frozen at an instant
``loop``     the task protocol, the GEPA adapter, the gate, generation records
``topics``   one package per task; ``kalshi_horizon`` is the first
``cli``      rsi-arena bench | optimize | show
"""

from .harness import Harness, HarnessConfig, HarnessError, Plan, Run, Runner, Toolbox

__all__ = ["Harness", "HarnessConfig", "HarnessError", "Plan", "Run", "Runner", "Toolbox"]
