"""XRayMyRepo analyzer: source repository -> analyzer facts -> CIM v1 snapshot document.

The CIM contract lives in ``xraymyrepo.cim``; this package only produces it. It has
no dependency on databases, web frameworks or the frontend. See
docs/analyzer/python-v1.md for the pipeline, rules and limitations.
"""

from .build import AnalyzerContractError, build_document
from .options import ANALYZER_VERSION, DEFAULT_IGNORE_GLOBS, AnalysisOptions
from .pipeline import analyze, collect_facts

__all__ = [
    "ANALYZER_VERSION",
    "DEFAULT_IGNORE_GLOBS",
    "AnalysisOptions",
    "AnalyzerContractError",
    "analyze",
    "build_document",
    "collect_facts",
]
