"""Architecture derivation (no database): golden expectations, determinism, invariants."""

from __future__ import annotations

import os
import random
from collections import Counter
from pathlib import Path

import pytest

from xraymyrepo.analyzer import analyze
from xraymyrepo.architecture import (
    ArchitectureParams,
    DerivationContractError,
    derive,
    validate_against,
)
from xraymyrepo.architecture.index import SnapshotIndex
from xraymyrepo.architecture.lenses import LensResult, components_lens, directory_lens
from xraymyrepo.cim import (
    Basis,
    Confidence,
    Derivation,
    DerivedEvidence,
    EdgeKind,
    Finding,
    Group,
    GroupMember,
    InferredLayer,
    LensKind,
    NodeKind,
    SnapshotDocument,
)
from xraymyrepo.graph import strongly_connected

from ..analyzer.helpers import run
from ..conftest import GOLDEN_DIR, GOLDEN_REPO

EXPECTED = GOLDEN_DIR / "expected_architecture.json"
APP = "group:components:pkg:pypi:acme-backend#app"
USERS = "group:components:pkg:pypi:acme-backend#app.users"


@pytest.fixture(scope="module")
def analyzed(golden: SnapshotDocument) -> SnapshotDocument:
    return analyze(GOLDEN_REPO, repository=golden.snapshot.repository,
                   commit_sha=golden.snapshot.commit_sha)  # fmt: skip


@pytest.fixture(scope="module")
def golden_derivation(golden: SnapshotDocument) -> Derivation:
    return derive(golden)


def layers_of(d: Derivation) -> dict[str, tuple[str, str]]:
    return {x.node_key: (x.layer, x.confidence.value) for x in d.layers}


# ---------------------------------------------------------------------------
# Golden expectations


def test_golden_derivation_matches_fixture(golden_derivation: Derivation) -> None:
    """fixtures/golden/v1/expected_architecture.json is the reviewed expected output for
    the golden document. Regenerate deliberately with XRAY_UPDATE_GOLDEN=1."""
    text = golden_derivation.to_canonical_json()
    if os.environ.get("XRAY_UPDATE_GOLDEN"):
        EXPECTED.write_text(text, encoding="utf-8", newline="\n")
    assert EXPECTED.read_text(encoding="utf-8") == text
    assert Derivation.model_validate_json(text).canonical() == golden_derivation


def test_golden_layers(golden_derivation: Derivation) -> None:
    assert layers_of(golden_derivation) == {
        "backend/app/db.py": ("infrastructure", "high"),        # db + sqlalchemy
        "backend/app/main.py": ("interface", "high"),           # fastapi + entrypoint
        "backend/app/users/models.py": ("infrastructure", "high"),  # sqlalchemy + orm_entity
        "backend/app/users/routes.py": ("interface", "high"),   # routes + fastapi
        "backend/app/users/schemas.py": ("interface", "high"),  # schemas + schema role
        "backend/app/users/service.py": ("application", "medium"),  # name + direction
    }  # fmt: skip
    coverage = {m.metric: m.value for m in golden_derivation.metrics if m.node_key == "/"}
    assert coverage == {"layer_files_considered": 9.0, "layer_files_classified": 6.0,
                        "layer_files_unclassified": 3.0}  # fmt: skip
    considered = {m.node_key for m in golden_derivation.metrics if m.metric == "layer_considered"}
    assert considered - set(layers_of(golden_derivation)) == {
        "backend/app/__init__.py",
        "backend/app/plugins.py",
        "backend/app/users/__init__.py",
    }
    # Out of scope: failed parse, generated, tests, other languages.
    for key in ("backend/app/legacy.py", "backend/app/generated/user_pb2.py",
                "backend/tests/test_users.py", "frontend/src/index.ts"):  # fmt: skip
        assert key not in considered


def test_golden_service_layer_rests_on_the_graph(golden_derivation: Derivation) -> None:
    service = next(x for x in golden_derivation.layers if x.node_key.endswith("service.py"))
    by_rule = {e.provenance.rule: e for e in service.evidence}
    assert set(by_rule) == {"arch.layer.name", "arch.layer.direction"}
    refs = {(r.source_key, r.target_key) for r in by_rule["arch.layer.direction"].edge_refs}
    assert refs == {
        ("backend/app/users/routes.py", "backend/app/users/service.py#UserService"),
        ("backend/app/users/service.py", "backend/app/users/models.py#User"),
    }


def test_golden_components(golden_derivation: Derivation) -> None:
    components = {g.key: (g.parent_key, g.confidence.value) for g in golden_derivation.groups
                  if g.lens == "components"}  # fmt: skip
    project = "group:components:pkg:pypi:acme-backend"
    assert components == {
        project: (None, "high"),
        APP: (project, "high"),
        USERS: (project, "high"),
        f"{project}#app.generated": (project, "medium"),  # namespace package, one file
        f"{project}#tests": (project, "medium"),
    }
    members = Counter(m.group_key for m in golden_derivation.members if "components" in m.group_key)
    assert members == {APP: 5, USERS: 5, f"{project}#app.generated": 1, f"{project}#tests": 1}


def test_golden_cycle(golden_derivation: Derivation) -> None:
    cycles = [f for f in golden_derivation.findings if f.rule == "arch.group.cycle"]
    assert [f.subject_keys for f in cycles] == [(APP, USERS)]
    cited = {(r.kind, r.source_key, r.target_key) for e in cycles[0].evidence
             for r in e.edge_refs}  # type: ignore[union-attr]  # fmt: skip
    assert cited == {
        (EdgeKind.IMPORTS, "backend/app/main.py", "backend/app/users/routes.py"),
        (EdgeKind.IMPORTS, "backend/app/users/models.py", "backend/app/db.py#Base"),
        (EdgeKind.INHERITS, "backend/app/users/models.py#User", "backend/app/db.py#Base"),
    }
    assert not [f for f in golden_derivation.findings if f.rule == "arch.layer.violation"]


def test_analyzed_golden_repository(analyzed: SnapshotDocument) -> None:
    """The analyzer emits fewer facts (no roles but manifest, IMPORTS only): fewer
    strong signals, lower confidence, never invented layers."""
    d = derive(analyzed)
    assert layers_of(d) == {
        "backend/app/db.py": ("infrastructure", "high"),
        "backend/app/main.py": ("interface", "medium"),
        "backend/app/users/models.py": ("infrastructure", "medium"),
        "backend/app/users/routes.py": ("interface", "high"),
        "backend/app/users/schemas.py": ("interface", "medium"),
        "backend/app/users/service.py": ("application", "medium"),
    }
    assert [f.subject_keys for f in d.findings if f.rule == "arch.group.cycle"] == [(APP, USERS)]


# ---------------------------------------------------------------------------
# Layer resolution on small repositories

# External imports resolve only to packages a manifest declares.
PYPROJECT = '[project]\nname = "demo"\ndependencies = ["fastapi", "sqlalchemy"]\n'


def test_name_only_stays_unclassified(tmp_path: Path) -> None:
    d = derive(run(tmp_path, {"services/billing.py": "def charge():\n    return 1\n"}))
    assert d.layers == ()
    assert {m.node_key for m in d.metrics if m.metric == "layer_considered"} == {
        "services/billing.py"}  # fmt: skip


def test_conflicting_strong_signals_stay_unclassified(tmp_path: Path) -> None:
    files: dict[str, str | bytes] = {
        "pyproject.toml": PYPROJECT,
        "app/mixed.py": "import fastapi\nimport sqlalchemy\n",
    }
    doc = run(tmp_path, files)
    assert {e.target_key for e in doc.edges} == {"ext:pypi:fastapi", "ext:pypi:sqlalchemy"}
    assert derive(doc).layers == ()


def test_upward_dependency_is_a_violation_and_lowers_confidence(tmp_path: Path) -> None:
    d = derive(run(tmp_path, {
        "pyproject.toml": PYPROJECT,
        "app/__init__.py": "",
        "app/routes.py": "import fastapi\n",
        "app/db.py": "import sqlalchemy\nfrom app import routes\n",
    }))  # fmt: skip
    assert layers_of(d) == {"app/db.py": ("infrastructure", "low"),
                            "app/routes.py": ("interface", "low")}  # fmt: skip
    violations = [f for f in d.findings if f.rule == "arch.layer.violation"]
    assert [f.subject_keys for f in violations] == [("app/db.py", "app/routes.py")]
    assert violations[0].confidence is Confidence.LOW
    assert violations[0].basis is Basis.INFERRED


def test_name_disagreeing_with_framework_is_low(tmp_path: Path) -> None:
    d = derive(run(tmp_path, {"pyproject.toml": PYPROJECT, "app/services.py": "import fastapi\n"}))
    assert layers_of(d) == {"app/services.py": ("interface", "low")}


def test_tests_generated_and_vendored_are_out_of_layer_scope(tmp_path: Path) -> None:
    d = derive(run(tmp_path / "out", {
        "pyproject.toml": PYPROJECT,
        "tests/test_routes.py": "import fastapi\n",
        "pkg/routes_test.py": "import fastapi\n",
        "pkg/conftest.py": "import fastapi\n",
        "vendor/lib/routes.py": "import fastapi\n",
    }))  # fmt: skip
    assert d.layers == ()
    assert not [m for m in d.metrics if m.metric == "layer_considered"]
    # The same file elsewhere is classified, so the exclusions above are what matter.
    inside = derive(run(tmp_path / "in", {"pyproject.toml": PYPROJECT,
                                          "pkg/routes.py": "import fastapi\n"}))  # fmt: skip
    assert layers_of(inside) == {"pkg/routes.py": ("interface", "high")}


# ---------------------------------------------------------------------------
# Determinism


def shuffled(doc: SnapshotDocument, seed: int) -> SnapshotDocument:
    rng = random.Random(seed)
    fields = ("producers", "coverage", "nodes", "edges", "classifications",
              "unresolved_references", "extraction_issues")  # fmt: skip
    update = {}
    for name in fields:
        items = list(getattr(doc, name))
        rng.shuffle(items)
        update[name] = tuple(items)
    return SnapshotDocument.model_validate(doc.model_copy(update=update).model_dump())


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_order_of_facts_does_not_matter(
    golden: SnapshotDocument, golden_derivation: Derivation, seed: int
) -> None:
    assert derive(shuffled(golden, seed)) == golden_derivation


def test_params_are_part_of_the_identity(golden: SnapshotDocument,
                                         golden_derivation: Derivation) -> None:  # fmt: skip
    assert derive(golden) == golden_derivation
    custom = ArchitectureParams(name_signals={"service": "domain"})
    other = derive(golden, custom)
    assert other.run.params_hash != golden_derivation.run.params_hash
    assert other.run.params_hash == custom.params_hash()
    assert ArchitectureParams().params_hash() == golden_derivation.run.params_hash
    with pytest.raises(ValueError, match="unknown layers"):
        ArchitectureParams(name_signals={"x": "presentation"})


# ---------------------------------------------------------------------------
# Invariants on every derivation


def check_invariants(doc: SnapshotDocument, d: Derivation) -> None:
    validate_against(doc, d)
    lenses = {lens.name: lens.kind for lens in d.lenses}
    assert lenses == {"directory": LensKind.DIRECTORY, "components": LensKind.INFERRED}
    files = {n.key for n in doc.nodes if n.kind is NodeKind.FILE}
    directory_members = [
        m.node_key for m in d.members if m.group_key.startswith("group:directory:")
    ]
    assert sorted(directory_members) == sorted(files)  # every file, once

    inferred: list[Group | InferredLayer | Finding] = [
        *(g for g in d.groups if g.lens == "components"),
        *d.layers,
        *d.findings,
    ]
    for record in inferred:
        assert record.basis is Basis.INFERRED
        assert record.evidence
        for item in record.evidence:
            assert isinstance(item, DerivedEvidence) and item.basis is Basis.INFERRED
            assert item.provenance.rule is not None
    for layer in d.layers:
        assert layer.provenance.rule == "arch.layer.resolve"
        # A name alone never classifies (decision D2).
        assert {e.provenance.rule for e in layer.evidence} - {"arch.layer.name"}

    index = SnapshotIndex(doc, ArchitectureParams())
    for result in (directory_lens(index), components_lens(index)):
        expected = _expected_rollup_total(index, result)
        actual = sum(e.count for e in d.group_edges if e.source_group_key.startswith(
            f"group:{result.lens.name}:"))  # fmt: skip
        assert actual == expected, result.lens.name

    coverage = {m.metric: m.value for m in d.metrics if m.node_key == "/"}
    considered = [m.node_key for m in d.metrics if m.metric == "layer_considered"]
    assert coverage["layer_files_considered"] == len(considered)
    assert coverage["layer_files_classified"] == len(d.layers)
    assert {x.node_key for x in d.layers} <= set(considered)


def _expected_rollup_total(index: SnapshotIndex, lens: LensResult) -> int:
    """Each leaf edge contributes once per level below the two chains' common prefix."""
    total = 0
    for fe in index.file_edges:
        source, target = lens.chain(fe.source_file), lens.chain(fe.target_file)
        if source is None or target is None:
            continue
        shared = 0
        while shared < min(len(source), len(target)) and source[shared] == target[shared]:
            shared += 1
        total += min(len(source), len(target)) - shared
    return total


def test_invariants_golden(golden: SnapshotDocument, golden_derivation: Derivation) -> None:
    check_invariants(golden, golden_derivation)


def test_invariants_analyzed(analyzed: SnapshotDocument) -> None:
    check_invariants(analyzed, derive(analyzed))


def test_group_metrics(golden_derivation: Derivation) -> None:
    metrics = {(m.node_key, m.metric): m.value for m in golden_derivation.metrics}
    assert metrics[("group:directory:/", "files")] == 22
    # app -> app.users: main -> routes; app.users -> app: models -> db (IMPORTS, INHERITS)
    assert metrics[(APP, "efferent")] == 1 and metrics[(APP, "afferent")] == 2
    assert metrics[(USERS, "efferent")] == 2 and metrics[(USERS, "afferent")] == 2  # + tests
    assert metrics[(APP, "instability")] == pytest.approx(1 / 3)
    assert (f"{APP[:-4]}#app.generated", "instability") not in metrics  # no dependencies


# ---------------------------------------------------------------------------
# Snapshot-level checks and cycle detection


def test_validate_against_rejects_dangling_keys(golden: SnapshotDocument,
                                                golden_derivation: Derivation) -> None:  # fmt: skip
    member = GroupMember(group_key=APP, node_key="backend/app/missing.py",
                         basis=Basis.INFERRED, confidence=Confidence.HIGH)  # fmt: skip
    broken = golden_derivation.model_copy(
        update={"members": (*golden_derivation.members[:-1], member)})  # fmt: skip
    with pytest.raises(DerivationContractError, match="unknown node"):
        validate_against(golden, broken)
    layer = golden_derivation.layers[0]
    stranger = layer.model_copy(update={"provenance": layer.provenance.model_copy(
        update={"producer": "someone-else"})})  # fmt: skip
    with pytest.raises(DerivationContractError, match="produced by"):
        validate_against(golden, golden_derivation.model_copy(
            update={"layers": (stranger, *golden_derivation.layers[1:])}))  # fmt: skip


def test_strongly_connected_components() -> None:
    adjacency = {"a": ["b"], "b": ["c"], "c": ["a", "d"], "d": ["e"], "e": ["d"], "f": ["f"]}
    found = sorted(strongly_connected(sorted("abcdef"), adjacency))
    assert found == [["a", "b", "c"], ["d", "e"], ["f"]]
