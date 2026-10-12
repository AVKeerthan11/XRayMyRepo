"""Pure graph building blocks shared by the derivation algorithms.

Imports only ``xraymyrepo.cim``; used by ``architecture`` and ``insights``, which do
not import each other. Holds the file-level view of a snapshot (``FileGraph``), a
compact dependency graph for traversals (``DependencyGraph``), strongly connected
components and the checks of a derivation against its snapshot.
"""

from .checks import DerivationContractError, validate_against
from .dependency import DependencyGraph, GraphEdge, GraphNode, GraphParts
from .files import FileEdge, FileGraph
from .scc import strongly_connected

__all__ = [
    "DependencyGraph",
    "DerivationContractError",
    "FileEdge",
    "FileGraph",
    "GraphEdge",
    "GraphNode",
    "GraphParts",
    "strongly_connected",
    "validate_against",
]
