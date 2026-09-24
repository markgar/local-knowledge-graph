"""Optional local graph lifecycle and cited queries; SQLite remains authoritative."""

from kg.graph._session_types import GraphSessionError
from kg.graph.session import LocalGraphSession

__all__ = ["GraphSessionError", "LocalGraphSession"]
