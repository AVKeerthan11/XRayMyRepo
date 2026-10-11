"""``derive``: snapshot document -> architecture ``Derivation``. Pure and deterministic."""

from __future__ import annotations

from pydantic import ValidationError

from xraymyrepo.cim import Derivation, DerivationRun, SnapshotDocument, SnapshotStatus

from .findings import group_cycles, layer_violations
from .index import SnapshotIndex
from .layers import infer_layers
from .lenses import components_lens, directory_lens
from .params import ALGORITHM, ALGORITHM_VERSION, ArchitectureParams
from .rollups import rollup
from .validate import DerivationContractError, validate_against


def derive(doc: SnapshotDocument, params: ArchitectureParams | None = None) -> Derivation:
    """Derive groups, group edges, metrics, layers and findings from one snapshot.

    The result depends only on the document's content (not its collection order), the
    algorithm version and ``params``; it is returned in canonical order with status
    ``complete``. An invalid result is a derivation bug and raises
    ``DerivationContractError``.
    """
    params = params or ArchitectureParams()
    index = SnapshotIndex(doc, params)
    lenses = [directory_lens(index), components_lens(index)]

    group_edges, metrics, findings = [], [], []
    for lens in lenses:
        edges, lens_metrics = rollup(index, lens)
        group_edges += edges
        metrics += lens_metrics
        if lens.lens.name == "components":
            findings += group_cycles(index, lens, edges)
    layers = infer_layers(index)
    metrics += layers.metrics
    findings += layer_violations(index, layers.layers)

    try:
        derivation = Derivation(
            run=DerivationRun(
                snapshot=doc.snapshot,
                algorithm=ALGORITHM,
                algorithm_version=ALGORITHM_VERSION,
                params_hash=params.params_hash(),
                status=SnapshotStatus.COMPLETE,
            ),
            lenses=tuple(lens.lens for lens in lenses),
            groups=tuple(g for lens in lenses for g in lens.groups.values()),
            members=tuple(m for lens in lenses for m in lens.members.values()),
            group_edges=tuple(group_edges),
            metrics=tuple(metrics),
            findings=tuple(findings),
            layers=tuple(layers.layers.values()),
        ).canonical()
    except ValidationError as exc:
        raise DerivationContractError(str(exc)) from exc
    validate_against(doc, derivation)
    return derivation
