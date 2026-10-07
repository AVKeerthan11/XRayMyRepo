"""Stage 5: turn analyzer facts into a CIM v1 ``SnapshotDocument``.

This module only maps facts onto the contract models. Every invariant is checked
by constructing ``SnapshotDocument``; a violation is an analyzer bug and is raised
as ``AnalyzerContractError``, never repaired or hidden.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from typing import Any

from pydantic import ValidationError

from xraymyrepo.cim import (
    CIM_SCHEMA_VERSION,
    REPOSITORY_KEY,
    Basis,
    Classification,
    ClassificationFacet,
    Confidence,
    ConfigEvidence,
    ConventionEvidence,
    CoverageDeclaration,
    Edge,
    EdgeKind,
    ExtractionIssue,
    ExtractionStatus,
    ImportsAttributes,
    Node,
    NodeKind,
    Producer,
    Provenance,
    ReferenceKind,
    RepositoryRef,
    Role,
    SnapshotDocument,
    SnapshotIdentity,
    SourceSpan,
    SpanEvidence,
    UnresolvedReference,
    external_package_key,
    package_key,
)
from xraymyrepo.cim._base import MAX_EVIDENCE_SAMPLE as MAX_EVIDENCE
from xraymyrepo.cim.keys import decode_path, parent_path_key

from .facts import (
    MANIFEST_PRODUCER,
    PYTHON_PRODUCER,
    STRUCTURE_PRODUCER,
    IssueFact,
    OriginFact,
    RepositoryFacts,
    ResolvedImport,
    Span,
)
from .options import ANALYZER_VERSION, AnalysisOptions

PRODUCER_NAMES = (STRUCTURE_PRODUCER, PYTHON_PRODUCER, MANIFEST_PRODUCER)
EMPTY_CONFIG_HASH = "sha256:" + hashlib.sha256(b"{}").hexdigest()
"""Producers in this package take no configuration of their own."""
_TREE = Provenance(producer=STRUCTURE_PRODUCER, rule="fs.tree")


class AnalyzerContractError(RuntimeError):
    """The analyzer produced a document that violates the CIM v1 contract (a bug)."""


def build_document(
    facts: RepositoryFacts,
    *,
    repository: RepositoryRef,
    commit_sha: str,
    options: AnalysisOptions,
) -> SnapshotDocument:
    try:
        return _build(facts, repository, commit_sha, options)
    except ValidationError as exc:
        raise AnalyzerContractError(f"analyzer output violates CIM v1: {exc}") from exc


def _build(
    facts: RepositoryFacts, repository: RepositoryRef, commit_sha: str, options: AnalysisOptions
) -> SnapshotDocument:
    config = options.config()
    data: dict[str, Any] = {
        "schema_version": CIM_SCHEMA_VERSION,
        "snapshot": SnapshotIdentity(
            repository=repository,
            commit_sha=commit_sha,
            extractor_version=ANALYZER_VERSION,
            config_hash=config.config_hash(),
        ),
        "config": config,
        "producers": tuple(
            Producer(name=n, version=ANALYZER_VERSION, config_hash=EMPTY_CONFIG_HASH)
            for n in PRODUCER_NAMES
        ),
        "coverage": (CoverageDeclaration(language="python", edge_kinds=(EdgeKind.IMPORTS,)),),
        "nodes": (*_structure(facts, repository), *_symbols(facts), *_packages(facts)),
        "edges": _imports(facts.resolved),
        "classifications": _classifications(facts),
        "unresolved_references": _unresolved(facts),
        "extraction_issues": tuple(
            _issue(i)
            for i in (
                *facts.inventory.issues,
                *facts.manifests.issues,
                *(i for p in facts.python_files for i in p.issues),
            )
        ),
    }
    return SnapshotDocument.model_validate(data)


# ---------------------------------------------------------------------------
# Nodes


def _structure(facts: RepositoryFacts, repository: RepositoryRef) -> list[Node]:
    nodes = [
        _node(
            key=REPOSITORY_KEY,
            kind=NodeKind.REPOSITORY,
            name=repository.name,
            parent_key=None,
            provenance=_TREE,
            attributes={},
        )
    ]
    for directory in facts.inventory.directories:
        nodes.append(
            _node(
                key=directory.key,
                kind=NodeKind.DIRECTORY,
                name=_basename(directory.key),
                parent_key=parent_path_key(directory.key),
                provenance=_TREE,
                attributes={},
            )
        )
    python = {p.file_key: p for p in facts.python_files}
    for file in facts.inventory.files:
        attributes: dict[str, Any] = {}
        if not file.extracted:
            status = ExtractionStatus.EXCLUDED
        elif file.key in python:
            status = python[file.key].status
            if python[file.key].module_name is not None:
                attributes["module_name"] = python[file.key].module_name
        else:
            status = facts.manifests.statuses.get(file.key, ExtractionStatus.UNSUPPORTED)
        nodes.append(
            _node(
                key=file.key,
                kind=NodeKind.FILE,
                name=_basename(file.key),
                parent_key=parent_path_key(file.key),
                language=file.language,
                provenance=_TREE,
                extraction_status=status,
                blob_sha=file.blob_sha,
                attributes=attributes,
            )
        )
    return nodes


def _symbols(facts: RepositoryFacts) -> list[Node]:
    nodes = []
    for parsed in facts.python_files:
        for symbol in parsed.symbols:
            attributes: dict[str, Any] = {}
            if symbol.type_kind is not None:
                attributes["type_kind"] = symbol.type_kind
            if symbol.function_kind is not None:
                attributes["function_kind"] = symbol.function_kind
                if symbol.is_async:
                    attributes["is_async"] = True
            nodes.append(
                _node(
                    key=symbol.key,
                    kind=symbol.kind,
                    name=symbol.name,
                    parent_key=symbol.parent_key,
                    language="python",
                    span=_span(symbol.span),
                    provenance=Provenance(producer=PYTHON_PRODUCER, rule=symbol.rule),
                    attributes=attributes,
                )
            )
    return nodes


def _packages(facts: RepositoryFacts) -> list[Node]:
    nodes: list[Node] = []
    externals: dict[str, list[ConfigEvidence]] = {}
    for project in facts.manifests.projects:
        manifest = project.manifest_key
        if manifest is None:
            continue
        if project.package_name is not None:
            evidence = _config(manifest, "/project/name", "py.manifest.project")
            nodes.append(
                _node(
                    key=package_key("pypi", project.package_name),
                    kind=NodeKind.PACKAGE,
                    name=project.package_name,
                    parent_key=None,
                    provenance=evidence.provenance,
                    evidence=(evidence,),
                    attributes={
                        "ecosystem": "pypi",
                        "package_name": project.package_name,
                        "manifest_key": manifest,
                        "root_key": project.root_key,
                    },
                )
            )
        for dependency in project.dependencies:
            key = external_package_key("pypi", dependency.name)
            externals.setdefault(key, []).append(
                _config(manifest, dependency.json_pointer, dependency.rule)
            )
    for key, declared in externals.items():
        name = key.split(":", 2)[2]
        nodes.append(
            _node(
                key=key,
                kind=NodeKind.EXTERNAL_PACKAGE,
                name=name,
                parent_key=None,
                provenance=declared[0].provenance,
                evidence=tuple(declared[:MAX_EVIDENCE]),
                attributes={"ecosystem": "pypi", "package_name": name},
            )
        )

    stdlib: dict[str, list[SpanEvidence]] = {}
    for resolved in sorted(facts.resolved, key=_occurrence):
        if resolved.target_key.startswith("ext:python-stdlib:"):
            items = stdlib.setdefault(resolved.target_key, [])
            item = _span_evidence(resolved)
            if item not in items:
                items.append(item)
    for key, imported in sorted(stdlib.items()):
        name = key.split(":", 2)[2]
        nodes.append(
            _node(
                key=key,
                kind=NodeKind.EXTERNAL_PACKAGE,
                name=name,
                parent_key=None,
                basis=Basis.RESOLVED,
                provenance=imported[0].provenance,
                evidence=tuple(imported[:MAX_EVIDENCE]),
                attributes={"ecosystem": "python-stdlib", "package_name": name},
            )
        )
    return nodes


# ---------------------------------------------------------------------------
# Edges, classifications and diagnostics


def _imports(resolved: Iterable[ResolvedImport]) -> tuple[Edge, ...]:
    groups: dict[tuple[str, str], list[ResolvedImport]] = {}
    for item in sorted(resolved, key=_occurrence):
        groups.setdefault((item.fact.source_key, item.target_key), []).append(item)
    edges = []
    for (source, target), items in groups.items():
        evidence: list[SpanEvidence] = []
        for item in items:
            span_item = _span_evidence(item)
            if span_item not in evidence:
                evidence.append(span_item)
        attributes = ImportsAttributes(
            specifiers=tuple(sorted({i.fact.specifier for i in items})),
            imported_names=tuple(sorted({n for i in items for n in i.imported_names})),
            is_type_only=all(i.fact.is_type_only for i in items),
            is_dynamic=all(i.is_dynamic for i in items),
            via=items[0].via,
        )
        edges.append(
            Edge(
                kind=EdgeKind.IMPORTS,
                source_key=source,
                target_key=target,
                basis=Basis.RESOLVED,
                confidence=Confidence.HIGH,
                occurrence_count=len(evidence),
                evidence=tuple(evidence[:MAX_EVIDENCE]),
                attributes=attributes,
            )
        )
    return tuple(edges)


def _classifications(facts: RepositoryFacts) -> tuple[Classification, ...]:
    out = []
    origins = [(d.key, d.origin) for d in facts.inventory.directories if d.origin is not None]
    origins += [(f.key, f.origin) for f in facts.inventory.files]
    for key, origin in origins:
        assert origin is not None
        out.append(
            Classification(
                node_key=key,
                facet=ClassificationFacet.ORIGIN,
                value=origin.origin,
                basis=Basis.HEURISTIC,
                confidence=Confidence.HIGH,
                evidence=(_origin_evidence(key, origin),),
            )
        )
    for project in facts.manifests.projects:
        if project.manifest_key is not None and project.package_name is not None:
            out.append(
                Classification(
                    node_key=project.manifest_key,
                    facet=ClassificationFacet.ROLE,
                    value=Role.MANIFEST,
                    basis=Basis.OBSERVED,
                    confidence=Confidence.HIGH,
                    evidence=(_config(project.manifest_key, "/project", "py.manifest.project"),),
                )
            )
    return tuple(out)


def _unresolved(facts: RepositoryFacts) -> tuple[UnresolvedReference, ...]:
    out: dict[tuple[Any, ...], UnresolvedReference] = {}
    for item in facts.unresolved:
        fact, span = item.fact, item.fact.span
        record = UnresolvedReference(
            source_key=fact.source_key,
            file_key=fact.file_key,
            ref_kind=ReferenceKind.IMPORT,
            raw_text=_text(item.raw_text),
            start_line=span.start_line,
            start_col=span.start_col,
            end_line=span.end_line,
            end_col=span.end_col,
            reason=item.reason,
            provenance=Provenance(producer=PYTHON_PRODUCER, rule=item.rule),
        )
        out.setdefault((fact.source_key, span, record.raw_text, item.reason), record)
    return tuple(out.values())


def _issue(issue: IssueFact) -> ExtractionIssue:
    return ExtractionIssue(
        file_key=issue.file_key,
        severity=issue.severity,
        code=issue.code,
        message=_text(issue.message),
        span=_span(issue.span) if issue.span else None,
        provenance=Provenance(producer=issue.producer, rule=issue.rule),
    )


# ---------------------------------------------------------------------------
# Helpers


def _node(**fields: Any) -> Node:
    fields.setdefault("basis", Basis.OBSERVED)
    fields.setdefault("confidence", Confidence.HIGH)
    return Node.model_validate(fields)


def _basename(key: str) -> str:
    return decode_path(key).rsplit("/", 1)[-1]


def _span(span: Span) -> SourceSpan:
    return SourceSpan(
        start_line=span.start_line,
        start_col=span.start_col,
        end_line=span.end_line,
        end_col=span.end_col,
    )


def _occurrence(item: ResolvedImport) -> tuple[str, Span, str]:
    return item.fact.file_key, item.fact.span, item.target_key


def _span_evidence(item: ResolvedImport) -> SpanEvidence:
    span = item.fact.span
    return SpanEvidence(
        type="span",
        basis=Basis.RESOLVED,
        provenance=Provenance(producer=PYTHON_PRODUCER, rule=item.rule),
        file_key=item.fact.file_key,
        start_line=span.start_line,
        start_col=span.start_col,
        end_line=span.end_line,
        end_col=span.end_col,
        ast_type=item.fact.ast_type,
    )


def _config(file_key: str, pointer: str, rule: str) -> ConfigEvidence:
    return ConfigEvidence(
        type="config",
        basis=Basis.OBSERVED,
        provenance=Provenance(producer=MANIFEST_PRODUCER, rule=rule),
        file_key=file_key,
        json_pointer=pointer,
    )


def _origin_evidence(key: str, origin: OriginFact) -> ConventionEvidence | SpanEvidence:
    provenance = Provenance(producer=STRUCTURE_PRODUCER, rule=origin.rule)
    if origin.span is not None:
        span = origin.span
        return SpanEvidence(
            type="span",
            basis=Basis.HEURISTIC,
            provenance=provenance,
            file_key=key,
            start_line=span.start_line,
            start_col=span.start_col,
            end_line=span.end_line,
            end_col=span.end_col,
            ast_type=origin.ast_type,
        )
    assert origin.pattern is not None and origin.matched_value is not None
    return ConventionEvidence(
        type="convention",
        basis=Basis.HEURISTIC,
        provenance=provenance,
        pattern=origin.pattern,
        matched_value=origin.matched_value,
    )


def _text(value: str) -> str:
    """Single-line text as the contract requires (messages can contain newlines)."""
    return " ".join(value.split()) or "-"
