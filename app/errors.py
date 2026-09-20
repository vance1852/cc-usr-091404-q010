"""Domain errors mapped to HTTP responses in the API layer."""
from __future__ import annotations


class LifecycleError(Exception):
    """Base class for business-rule violations (HTTP 422)."""


class ChainIntegrityError(LifecycleError):
    """Hash chain verification failed (HTTP 409)."""


class DestructionBlocked(LifecycleError):
    """Destruction request failed eligibility checks; carries blocker list."""

    def __init__(self, blockers: list[dict]):
        self.blockers = blockers
        super().__init__("; ".join(b["reason"] for b in blockers))
