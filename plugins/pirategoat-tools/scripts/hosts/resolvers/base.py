"""Base class for host resolvers."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List, Dict, Any

from hosts.types import HostEntry


@dataclass
class ResolverResult:
    entries: List[HostEntry]
    unresolved: List[Dict[str, Any]]
    notes: Dict[str, Any]


class HostResolver(ABC):
    """A resolver discovers host codebases by one mechanism.

    Implementations must be side-effect-free: no network, no git, no mutation.
    Read-only filesystem introspection of repo_path and well-known dirs only.
    The one exception is ``EcosystemCacheResolver``: it reads each cache
    slot's git identity (read-only ``git`` in the slot, never the repository),
    and its ``resolve_for_names`` fulfilment mode also refreshes the slot,
    which the chain calls only for a host the repository signalled.
    """

    source: str  # ResolverSource literal; set on subclass

    @abstractmethod
    def resolve(self, repo_path: str, scan=None) -> ResolverResult:
        """Return what this resolver found. Empty result is fine.

        ``scan`` is the chain's one ``scan_roots(repo_path)`` result; a
        resolver that reads the scan roots uses it and scans itself only
        when called standalone.
        """
        raise NotImplementedError
