"""Health derivation (no database): golden expectations, one repository per rule,
confidence under hidden imports, determinism, and the observed/inferred split."""

from __future__ import annotations

import os
import random
from pathlib import Path
from typing import Any

import pytest

from xraymyrepo.analyzer import analyze
from xraymyrepo.cim import (
    Basis,
    Confidence,
    Derivation,
    DerivedEvidence,
    SnapshotDocument,
    SpanEvidence,
)
from xraymyrepo.graph import validate_against
from xraymyrepo.insights.health import (
    RULE_BLIND_SPOT,
    RULE_CYCLE,
    RULE_FAN_OUT,
    RULE_HUB,
    RULE_UNREFERENCED,
    RULE_UNUSED,
    HealthParams,
    health,
)

from ..analyzer.helpers import run
from ..conftest import GOLDEN_DIR, GOLDEN_REPO

EXPECTED = GOLDEN_DIR / "expected_health.json"
OBSERVED_RULES = {RULE_BLIND_SPOT}
INFERRED_RULES = {RULE_CYCLE, RULE_HUB, RULE_FAN_OUT, RULE_UNREFERENCED, RULE_UNUSED}


@pytest.fixture(scope="module")
def golden_health(golden: SnapshotDocument) -> Derivation:
    return health(golden)


def by_rule(d: Derivation) -> dict[str, list[tuple[tuple[str, ...], str]]]:
    found: dict[str, list[tuple[tuple[str, ...], str]]] = {}
    for f in d.findings:
        found.setdefault(f.rule, []).append((f.subject_keys, f.confidence.value))
    return found


def metric(d: Derivation, key: str, name: str) -> float | None:
    return next((m.value for m in d.metrics if m.node_key == key and m.metric == name), None)


# ---------------------------------------------------------------------------
# Golden


def test_golden_health_matches_fixture(golden_health: Derivation) -> None:
    """fixtures/golden/v1/expected_health.json is the reviewed expected health of the
    golden document. Regenerate deliberately with XRAY_UPDATE_GOLDEN=1."""
    text = golden_health.to_canonical_json()
    if os.environ.get("XRAY_UPDATE_GOLDEN"):
        EXPECTED.write_text(text, encoding="utf-8", newline="\n")
    assert EXPECTED.read_text(encoding="utf-8") == text
    assert Derivation.model_validate_json(text).canonical() == golden_health


def test_golden_findings(golden_health: Derivation) -> None:
    assert by_rule(golden_health) == {
        RULE_BLIND_SPOT: [
            (("backend/app/legacy.py",), "high"),
            (("backend/app/plugins.py",), "high"),
        ],
        # Low: plugins.py itself hides imports (dynamic), and legacy.py did not parse.
        RULE_UNREFERENCED: [(("backend/app/plugins.py",), "low")],
    }
    # No file-level cycle: the app <-> app.users cycle exists only between components
    # (reported by the architecture derivation), not between files.
    assert RULE_CYCLE not in by_rule(golden_health)
    # pytest is a dev requirement; fastapi, sqlalchemy and pydantic are imported.
    assert RULE_UNUSED not in by_rule(golden_health)


def test_golden_metrics(golden_health: Derivation) -> None:
    assert metric(golden_health, "backend/app/users/service.py", "fan_in") == 2  # routes, test
    assert metric(golden_health, "backend/app/users/routes.py", "fan_out") == 2  # schemas, service
    assert metric(golden_health, "backend/tests/test_users.py", "fan_in") is None  # not considered
    assert {m.metric: m.value for m in golden_health.metrics if m.node_key == "/"} == {
        "health_files_considered": 9.0,
        "health_blind_spot_files": 2.0,
        "health_hidden_import_files": 2.0,
    }


def test_blind_spots_cite_the_unseen_source(
    golden: SnapshotDocument, golden_health: Derivation
) -> None:
    finding = next(
        f
        for f in golden_health.findings
        if f.rule == RULE_BLIND_SPOT and f.subject_keys == ("backend/app/plugins.py",)
    )
    spans = {(e.file_key, e.start_line) for e in finding.evidence if isinstance(e, SpanEvidence)}
    expected = {
        (u.file_key, u.start_line)
        for u in golden.unresolved_references
        if u.file_key == "backend/app/plugins.py"
    }
    assert spans == expected and len(spans) == 2
    assert "dynamic: 1" in finding.message and "not_found: 1" in finding.message
    legacy = next(f for f in golden_health.findings if f.subject_keys == ("backend/app/legacy.py",))
    issue = next(i for i in golden.extraction_issues if i.file_key == "backend/app/legacy.py")
    assert issue.span is not None
    assert [
        (e.start_line, e.start_col) for e in legacy.evidence if isinstance(e, SpanEvidence)
    ] == [(issue.span.start_line, issue.span.start_col)]


def test_analyzer_output_gets_the_same_health(
    golden: SnapshotDocument, golden_health: Derivation
) -> None:
    analyzed = analyze(
        GOLDEN_REPO, repository=golden.snapshot.repository, commit_sha=golden.snapshot.commit_sha
    )
    assert by_rule(health(analyzed)) == by_rule(golden_health)


# ---------------------------------------------------------------------------
# One repository per rule (and the negative)


PKG = {"pkg/__init__.py": ""}


def files(**contents: str) -> dict[str, str | bytes]:
    return {**PKG, **{f"pkg/{name}.py": text for name, text in contents.items()}}


def test_file_cycle(tmp_path: Path) -> None:
    d = health(
        run(
            tmp_path,
            files(
                a="from pkg import b\n",
                b="from pkg import c\n",
                c="from pkg import a\n",
                d="from pkg import a\n",
            ),
        )
    )
    assert by_rule(d)[RULE_CYCLE] == [(("pkg/a.py", "pkg/b.py", "pkg/c.py"), "high")]
    cycle = next(f for f in d.findings if f.rule == RULE_CYCLE)
    assert {(r.source_key, r.target_key) for r in cycle.evidence[0].edge_refs} == {  # type: ignore[union-attr]
        ("pkg/a.py", "pkg/b.py"),
        ("pkg/b.py", "pkg/c.py"),
        ("pkg/c.py", "pkg/a.py"),
    }
    acyclic = health(run(tmp_path / "x", files(a="from pkg import b\n", b="x = 1\n")))
    assert RULE_CYCLE not in by_rule(acyclic)


def test_hub_and_fan_out(tmp_path: Path) -> None:
    importers = {f"m{i:02}": "from pkg.hub import shared\n" for i in range(25)}
    fan_out = "".join(f"import pkg.m{i:02}\n" for i in range(21))
    d = health(run(tmp_path, files(hub="def shared():\n    return 1\n", wide=fan_out, **importers)))
    rules = by_rule(d)
    assert rules[RULE_HUB] == [(("pkg/hub.py",), "high")]
    assert rules[RULE_FAN_OUT] == [(("pkg/wide.py",), "high")]
    assert metric(d, "pkg/hub.py", "fan_in") == 25
    # Below the absolute minimum: no hub, whatever the percentile.
    small = health(
        run(
            tmp_path / "s",
            files(hub="def shared():\n    return 1\n", **dict(list(importers.items())[:5])),
        )
    )
    assert RULE_HUB not in by_rule(small)
    # Thresholds are parameters (and part of the run identity).
    lowered = HealthParams(min_fan_in=5)
    assert by_rule(
        health(
            run(
                tmp_path / "t",
                files(hub="def shared():\n    return 1\n", **dict(list(importers.items())[:5])),
            ),
            lowered,
        )
    )[RULE_HUB] == [(("pkg/hub.py",), "high")]
    assert lowered.params_hash() != HealthParams().params_hash()


def test_unreferenced_confidence_depends_on_hidden_imports(tmp_path: Path) -> None:
    clean = health(
        run(
            tmp_path / "clean",
            files(orphan="x = 1\n", used="y = 2\n", user="from pkg import used\n"),
        )
    )
    assert sorted(by_rule(clean)[RULE_UNREFERENCED]) == [
        (("pkg/orphan.py",), "high"),
        (("pkg/user.py",), "high"),
    ]
    # One unresolvable import anywhere: any absence claim becomes low confidence.
    hidden = health(
        run(
            tmp_path / "hidden",
            files(
                orphan="x = 1\n",
                used="y = 2\n",
                user="from pkg import used\n",
                loader="import not_installed_anywhere\n",
            ),
        )
    )
    assert {c for _, c in by_rule(hidden)[RULE_UNREFERENCED]} == {"low"}
    assert by_rule(hidden)[RULE_BLIND_SPOT] == [(("pkg/loader.py",), "high")]
    # Entry points, package roots and tests are never reported.
    roots = health(run(tmp_path / "roots", {**files(main="x = 1\n"), "tests/test_x.py": "y = 1\n"}))
    assert RULE_UNREFERENCED not in by_rule(roots)


def test_unused_declared_requirement(golden_data: dict[str, Any]) -> None:
    """The analyzer emits no REQUIRES yet, so the golden document is extended with one
    more declared production requirement that nothing imports."""
    nodes, edges = golden_data["nodes"], golden_data["edges"]
    template = next(n for n in nodes if n["key"] == "ext:pypi:sqlalchemy")
    nodes.append(
        {
            **template,
            "key": "ext:pypi:requests",
            "name": "requests",
            "attributes": {**template["attributes"], "package_name": "requests"},
        }
    )
    requires = next(
        e for e in edges if e["kind"] == "REQUIRES" and e["target_key"] == "ext:pypi:sqlalchemy"
    )
    edges.append({**requires, "target_key": "ext:pypi:requests"})
    d = health(SnapshotDocument.model_validate(golden_data))
    assert by_rule(d)[RULE_UNUSED] == [(("pkg:pypi:acme-backend", "ext:pypi:requests"), "low")]
    finding = next(f for f in d.findings if f.rule == RULE_UNUSED)
    assert finding.basis is Basis.INFERRED and "dependent may be hidden" in finding.message


# ---------------------------------------------------------------------------
# Invariants and determinism


def check_invariants(doc: SnapshotDocument, d: Derivation) -> None:
    validate_against(doc, d)
    assert (d.lenses, d.groups, d.members, d.group_edges, d.layers) == ((), (), (), (), ())
    for f in d.findings:
        assert f.rule in OBSERVED_RULES | INFERRED_RULES
        if f.rule in OBSERVED_RULES:  # coverage gaps restate what was seen
            assert (f.basis, f.confidence) == (Basis.OBSERVED, Confidence.HIGH)
            assert all(e.basis is Basis.OBSERVED for e in f.evidence)
        else:  # structural findings are computed over the graph
            assert f.basis is Basis.INFERRED
            assert all(
                isinstance(e, DerivedEvidence) and e.basis is Basis.INFERRED for e in f.evidence
            )
        assert all(e.provenance.rule == f.rule for e in f.evidence)


def test_invariants(golden: SnapshotDocument, golden_health: Derivation, tmp_path: Path) -> None:
    check_invariants(golden, golden_health)
    doc = run(
        tmp_path,
        files(a="from pkg import b\n", b="from pkg import a\n", c="import not_installed\n"),
    )
    check_invariants(doc, health(doc))


@pytest.mark.parametrize("seed", [1, 2])
def test_order_of_facts_does_not_matter(
    golden: SnapshotDocument, golden_health: Derivation, seed: int
) -> None:
    rng = random.Random(seed)
    update = {}
    for name in ("nodes", "edges", "classifications", "unresolved_references", "extraction_issues"):
        items = list(getattr(golden, name))
        rng.shuffle(items)
        update[name] = tuple(items)
    shuffled = SnapshotDocument.model_validate(golden.model_copy(update=update).model_dump())
    assert health(shuffled) == golden_health


def test_run_identity(golden_health: Derivation) -> None:
    run_ = golden_health.run
    assert (run_.algorithm, run_.algorithm_version) == ("health", "1.0.0")
    assert run_.params_hash == HealthParams().params_hash()
