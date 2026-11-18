"""procsafe: bounded, process-group-scoped subprocess execution."""

from .runner import Result, group_members, run

__version__ = "1.0.0"
__all__ = ["Result", "run", "group_members", "__version__"]