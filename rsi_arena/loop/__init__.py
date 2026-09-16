"""The loop, independent of any topic.

A :class:`Task` says what an instance is, which tools a harness may use on it,
and what a run of the harness is worth. Everything here is written against
that protocol: :func:`evaluate` runs a harness over instances, the
:class:`TaskAdapter` presents the task to GEPA, the gate decides whether a
rewrite replaces the incumbent, and a :class:`Generation` records one turn of
the loop so the next one can start from it.
"""

from .adapter import TaskAdapter, reflection_templates
from .archive import ARCHIVE, Archive, Entry, from_gepa_state
from .gate import Decision, accept, paired_bootstrap
from .generation import Generation, lineage, render_lineage
from .settings import Settings
from .task import (Instance, Outcome, Rollout, Task, evaluate, probe_sample,
                   split_by_group, summarise, three_way_split)

__all__ = ["TaskAdapter", "reflection_templates", "ARCHIVE", "Archive", "Entry",
           "from_gepa_state", "Decision", "accept", "paired_bootstrap",
           "Generation", "lineage", "render_lineage", "Settings", "Instance", "Outcome", "Rollout", "Task",
           "evaluate", "probe_sample", "split_by_group", "summarise", "three_way_split"]
