"""RSI Arena: harnesses that compete, and a loop that rewrites the losers.

Four packages:

``harness``   the JSON contract a harness is written in, and the runner that
              executes it against a toolbox
``kalshi``    the Kalshi and fixture data layer, plus tools frozen at a past
              instant so a harness can be replayed
``bench``     fixed windows of finished matches, and the score a forecast earns
``optimize``  the GEPA adapter, the acceptance gate, and the CLI that runs them
"""

from .harness import Harness, HarnessConfig, HarnessError, Plan, Run, Runner, Toolbox

__all__ = ["Harness", "HarnessConfig", "HarnessError", "Plan", "Run", "Runner", "Toolbox"]
