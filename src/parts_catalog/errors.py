from __future__ import annotations


class PublicationError(Exception):
    """Base class for collection-run and publication semantics violations."""


class RunStateError(PublicationError):
    """The run lifecycle transition is not allowed, or the run is unknown."""


class RunAlreadyPublishedError(RunStateError):
    """The run_id already names a published (immutable) run."""


class IncompleteRunError(PublicationError):
    """A run was asked to publish before its pinned query scope was covered.

    Carries the still-pending scope entries so operators can see exactly
    which queries must be retried before the view may be replaced.
    """

    def __init__(self, run_id: str, pending) -> None:
        self.run_id = run_id
        self.pending = list(pending)
        super().__init__(
            f"run {run_id!r} has {len(self.pending)} pending queries; "
            "the previous complete publication stays visible"
        )


class OutOfScopeError(PublicationError):
    """A delivery carries cells outside the run's pinned query scope."""


class DeliveryError(PublicationError):
    """A delivery batch failed validation; nothing from the batch was staged."""

    def __init__(self, problems) -> None:
        self.problems = list(problems)
        super().__init__("invalid delivery: " + "; ".join(self.problems))
