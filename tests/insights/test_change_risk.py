"""Change risk (no database): the reviewed fixture, then synthetic snapshot pairs for
every factor, threshold, caveat, the exact unresolved-import rule, the test-file cap,
comparability, determinism and monotonicity."""

from __future__ import annotations

import json
import random
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from xraymyrepo.analyzer import analyze
from xraymyrepo.cim import (
    ChangeRiskAssessment,
    Confidence,
    NodeChangeKind,
    RepositoryRef,
    RiskCaveat,
    RiskFactor,
    RiskFactorRule,
    RiskItem,
    RiskLevel,
    Side,
    SnapshotComparison,
    SnapshotDocument,
    SnapshotStatus,
)
from xraymyrepo.insights.change_risk import (
    ALGORITHM,
    ALGORITHM_VERSION,
    ChangeRiskContractError,
    ChangeRiskParams,
    change_risk,
    python_unresolved_name,
    validate_against,
)
from xraymyrepo.insights.comparison import compare
from xraymyrepo.insights.health import HealthParams

from ..analyzer.helpers import write_repo
from ..conftest import GOLDEN_JSON, ROOT

FIXTURE = ROOT / "fixtures" / "comparison" / "v1"
FIXTURE_REPO = RepositoryRef(host="github.com", owner="xraymyrepo", name="comparison-fixture")
REPO = RepositoryRef(host="local", owner="tests", name="risk")
HIGH, MEDIUM, LOW = Confidence.HIGH, Confidence.MEDIUM, Confidence.LOW
R = RiskFactorRule
C = RiskCaveat

PKG = {"pkg/__init__.py": ""}
LEGACY = "def old():\n    return 1\n"
USER = "from .legacy import old\n\n\ndef run():\n    return old()\n"


def snapshot(root: Path, files: Mapping[str, str], sha: str) -> SnapshotDocument:
    return analyze(write_repo(root, dict(files)), repository=REPO, commit_sha=sha)


def pair(tmp_path: Path, before: Mapping[str, str],
         after: Mapping[str, str]) -> tuple[SnapshotDocument, SnapshotDocument]:  # fmt: skip
    return snapshot(tmp_path / "before", before, "1" * 40), snapshot(
        tmp_path / "after", after, "2" * 40
    )


def assess(before: SnapshotDocument, after: SnapshotDocument,
           params: ChangeRiskParams | None = None) -> ChangeRiskAssessment:  # fmt: skip
    return change_risk(compare(before, after), before, after, params)


def risk(tmp_path: Path, before: Mapping[str, str], after: Mapping[str, str],
         params: ChangeRiskParams | None = None) -> ChangeRiskAssessment:  # fmt: skip
    return assess(*pair(tmp_path, before, after), params)


def item(a: ChangeRiskAssessment, key: str) -> RiskItem:
    return next(i for i in a.items if i.subject_key == key)


def factors(i: RiskItem) -> dict[RiskFactorRule, RiskFactor]:
    return {f.rule: f for f in i.factors}


def points(i: RiskItem) -> dict[str, int]:
    return {f.rule.value: f.points for f in i.factors}


def with_identity(doc: SnapshotDocument, **identity: Any) -> SnapshotDocument:
    data = doc.model_dump(mode="json")
    data["snapshot"].update(identity)
    return SnapshotDocument.model_validate(data)


# ---------------------------------------------------------------------------
# The reviewed fixture


@pytest.fixture(scope="module")
def fixture_docs() -> tuple[SnapshotDocument, SnapshotDocument]:
    return (
        analyze(FIXTURE / "before", repository=FIXTURE_REPO, commit_sha="1" * 40),
        analyze(FIXTURE / "after", repository=FIXTURE_REPO, commit_sha="2" * 40),
    )


@pytest.fixture(scope="module")
def fixture_risk(fixture_docs: tuple[SnapshotDocument, SnapshotDocument]) -> ChangeRiskAssessment:
    return assess(*fixture_docs)


def test_fixture_matches_the_reviewed_expectation(fixture_risk: ChangeRiskAssessment) -> None:
    expected = ChangeRiskAssessment.model_validate_json(
        (FIXTURE / "expected_change_risk.json").read_text(encoding="utf-8")
    )
    assert fixture_risk == expected


def test_fixture_cases(fixture_risk: ChangeRiskAssessment) -> None:
    """The cases of fixtures/comparison/v1/FIXTURE.md (change risk), spelled out."""
    a = fixture_risk
    run = a.run
    assert (run.algorithm, run.algorithm_version, run.status) == (
        ALGORITHM, ALGORITHM_VERSION, SnapshotStatus.COMPLETE)  # fmt: skip
    assert (run.medium_at, run.high_at, run.params_hash) == (2, 4, ChangeRiskParams().params_hash())
    assert len(a.items) == 12 and a.excluded_by_origin == 0 and a.caveats == ()
    # A used declaration removed: kind 2 + interface 2 + dependents 1 (pricing.py, and
    # report.py through it, both still there). pricing.py dropped the import, so no
    # import of it is left unresolved: the exact factor does not fire.
    legacy = item(a, "shop/legacy.py")
    assert points(legacy) == {"risk.change.kind": 2, "risk.change.interface": 2,
                              "risk.impact.dependents": 1}  # fmt: skip
    assert (legacy.level, legacy.confidence, legacy.conclusive) == (RiskLevel.HIGH, HIGH, True)
    assert factors(legacy)[R.IMPACT_DEPENDENTS].value == 2
    # The ambiguous move is not credited: removing a file is removing it.
    assert points(item(a, "shop/a/template.py")) == {"risk.change.kind": 2,
                                                     "risk.change.interface": 2}  # fmt: skip
    assert item(a, "shop/consts.py").level is RiskLevel.MEDIUM  # removed, no declarations
    # report.py stopped parsing: undetermined and incompletely extracted, so low
    # confidence; and in `to` it hides its imports, so every `to` subject has
    # hidden_dependencies and no absence is scored.
    report = item(a, "shop/report.py")
    assert report.caveats == (C.UNDETERMINED_CHANGES, C.INCOMPLETE_EXTRACTION,
                              C.HIDDEN_DEPENDENCIES)  # fmt: skip
    assert (report.confidence, report.conclusive) == (LOW, False)
    assert C.UNDETERMINED_CHANGES in item(a, "shop/pricing.py").caveats  # report -> pricing#Cart
    for i in a.items:
        if i.change is not NodeChangeKind.REMOVED:
            assert C.HIDDEN_DEPENDENCIES in i.caveats
            absent = factors(i)[R.NO_DEPENDENT_TEST]
            assert (absent.points, absent.caveats) == (0, (C.HIDDEN_DEPENDENCIES,))
        else:
            assert R.NO_DEPENDENT_TEST not in factors(i)
    moved = item(a, "shop/new/shipping.py")
    assert (moved.change, points(moved)["risk.change.kind"]) == (NodeChangeKind.MOVED, 1)
    assert [(e.side, e.node_keys) for e in factors(moved)[R.CHANGE_KIND].evidence] == [
        (Side.TO, ("shop/new/shipping.py",)), (Side.FROM, ("shop/old/shipping.py",))]  # fmt: skip
    assert points(item(a, "shop/catalog.py"))["risk.change.kind"] == 0  # added
    assert (a.level, a.confidence, a.conclusive) == (RiskLevel.HIGH, LOW, False)
    assert a.summary().levels == {RiskLevel.LOW: 7, RiskLevel.MEDIUM: 3, RiskLevel.HIGH: 2}


def test_input_order_does_not_matter(
    fixture_docs: tuple[SnapshotDocument, SnapshotDocument], fixture_risk: ChangeRiskAssessment
) -> None:
    before, after = fixture_docs
    c = compare(before, after)
    for seed in range(3):
        rng = random.Random(seed)

        def shuffled(doc: SnapshotDocument, rng: random.Random = rng) -> SnapshotDocument:
            nodes, edges = list(doc.nodes), list(doc.edges)
            rng.shuffle(nodes)
            rng.shuffle(edges)
            return doc.model_copy(update={"nodes": tuple(nodes), "edges": tuple(edges)})

        nodes, edges = list(c.nodes), list(c.edges)
        rng.shuffle(nodes)
        rng.shuffle(edges)
        mixed = c.model_copy(update={"nodes": tuple(nodes), "edges": tuple(edges)})
        result = change_risk(mixed, shuffled(before), shuffled(after))
        assert result == fixture_risk
        assert result.model_dump_json() == fixture_risk.model_dump_json()


# ---------------------------------------------------------------------------
# Each factor


def test_change_kind_points(tmp_path: Path) -> None:
    before = {**PKG, "pkg/a.py": "def f():\n    return 1\n", "pkg/b.py": LEGACY}
    after = {**PKG, "pkg/a.py": "def f():\n    return 2\n", "pkg/c.py": LEGACY + "X = 1\n"}
    a = risk(tmp_path, before, after)
    found = {i.subject_key: points(i)["risk.change.kind"] for i in a.items}
    assert found == {"pkg/a.py": 1, "pkg/b.py": 2, "pkg/c.py": 0}


@pytest.mark.parametrize(
    ("after", "interface"),
    [
        ("def f(x, y):\n    return x\n\n\ndef g():\n    pass\n", 1),  # a new parameter
        ("def f(x: int):\n    return x\n\n\ndef g():\n    pass\n", 1),  # an annotation
        ("def f(x):\n    return x + 1\n\n\ndef g():\n    pass\n", 0),  # the body only
        ("def f(x):\n    return x\n", 1),  # g removed, the file survives
        ("def f(x):\n    return x\n\n\ndef g():\n    pass\n\n\ndef h():\n    pass\n", 0),  # added
    ],
)
def test_interface_change(tmp_path: Path, after: str, interface: int) -> None:
    before = "def f(x):\n    return x\n\n\ndef g():\n    pass\n"
    i = item(risk(tmp_path, {**PKG, "pkg/a.py": before}, {**PKG, "pkg/a.py": after}), "pkg/a.py")
    found = factors(i).get(R.CHANGE_INTERFACE)
    if not interface:
        assert found is None
    else:
        assert found is not None and (found.points, found.value) == (2, interface)
        assert found.evidence[0].side is Side.FROM


def test_signatures_are_not_compared_across_extractor_versions(tmp_path: Path) -> None:
    before, after = pair(tmp_path, {**PKG, "pkg/a.py": "def f(x):\n    return x\n"},
                         {**PKG, "pkg/a.py": "def f(x, y):\n    return x\n"})  # fmt: skip
    i = item(assess(with_identity(before, extractor_version="0.1.9"), after), "pkg/a.py")
    assert R.CHANGE_INTERFACE not in factors(i)  # f is undetermined, not "changed"
    assert C.UNDETERMINED_CHANGES in i.caveats and i.confidence is LOW


def importers(n: int, test: bool = False) -> dict[str, str]:
    files = {f"pkg/u{k}.py": "from .hub import f\n\n\ndef g():\n    return f()\n" for k in range(n)}
    if test:
        files["tests/test_hub.py"] = "from pkg.hub import f\n\n\ndef test_f():\n    assert f()\n"
    return files


HUB_BEFORE = "def f():\n    return 1\n"
HUB_AFTER = "def f():\n    return 2\n"


@pytest.mark.parametrize(("n", "expected"), [(0, None), (1, 1), (2, 1), (3, 2), (4, 2)])
def test_dependents_threshold(tmp_path: Path, n: int, expected: int | None) -> None:
    """wide_dependents = 3: 2 below it, 3 at it, 4 above it."""
    files = {**PKG, **importers(n)}
    a = risk(tmp_path, {**files, "pkg/hub.py": HUB_BEFORE}, {**files, "pkg/hub.py": HUB_AFTER},
             ChangeRiskParams(wide_dependents=3))  # fmt: skip
    found = factors(item(a, "pkg/hub.py")).get(R.IMPACT_DEPENDENTS)
    assert (found.points if found else None) == expected
    if found:
        assert found.value == n
        assert {e.node_keys[0] for e in found.evidence} == {f"pkg/u{k}.py" for k in range(n)}
        assert all(e.edge_refs for e in found.evidence)  # each with its witness path


def test_tests_are_not_dependents(tmp_path: Path) -> None:
    files = {**PKG, **importers(1, test=True)}
    i = item(risk(tmp_path, {**files, "pkg/hub.py": HUB_BEFORE},
                  {**files, "pkg/hub.py": HUB_AFTER}), "pkg/hub.py")  # fmt: skip
    assert factors(i)[R.IMPACT_DEPENDENTS].value == 1
    assert R.NO_DEPENDENT_TEST not in factors(i)  # a test depends on it


def test_a_removed_file_counts_only_dependents_that_still_exist(tmp_path: Path) -> None:
    before = {**PKG, **importers(3), "pkg/hub.py": HUB_BEFORE}
    after = {k: v for k, v in before.items() if k not in ("pkg/hub.py", "pkg/u0.py")}
    i = item(risk(tmp_path, before, after), "pkg/hub.py")
    dependents = factors(i)[R.IMPACT_DEPENDENTS]
    assert dependents.value == 2 and all(e.side is Side.FROM for e in dependents.evidence)


@pytest.fixture(scope="module")
def golden() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(GOLDEN_JSON.read_text(encoding="utf-8"))
    return data


def golden_edit(golden: dict[str, Any], *files: str) -> tuple[SnapshotDocument, SnapshotDocument]:
    """The golden document and a copy whose ``files`` have another blob (modified)."""
    data = json.loads(json.dumps(golden))
    data["snapshot"]["commit_sha"] = "f" * 40
    for node in data["nodes"]:
        if node["key"] in files:
            node["blob_sha"] = "e" * 40
    return SnapshotDocument.model_validate(golden), SnapshotDocument.model_validate(data)


def test_endpoints_and_heuristic_paths(golden: dict[str, Any]) -> None:
    # The handler's own file: the endpoint is registered in it (high).
    routes = item(assess(*golden_edit(golden, "backend/app/users/routes.py")),
                  "backend/app/users/routes.py")  # fmt: skip
    endpoints = factors(routes)[R.IMPACT_ENDPOINTS]
    assert (endpoints.points, endpoints.value, endpoints.confidence) == (1, 1, HIGH)
    assert endpoints.evidence[0].node_keys == (
        "endpoint:http:GET /users/{user_id}@pkg:pypi:acme-backend",
    )
    # A dependency of the handler: the endpoint is reached through HANDLES (heuristic).
    service = item(assess(*golden_edit(golden, "backend/app/users/service.py")),
                   "backend/app/users/service.py")  # fmt: skip
    assert factors(service)[R.IMPACT_ENDPOINTS].confidence is MEDIUM
    assert C.HEURISTIC_PATHS in factors(service)[R.IMPACT_ENDPOINTS].caveats
    assert C.HEURISTIC_PATHS in service.caveats and service.confidence is MEDIUM


def test_sensitive_roles(tmp_path: Path, golden: dict[str, Any]) -> None:
    files = {**PKG, "pyproject.toml": '[project]\nname = "x"\n'}
    a = risk(tmp_path, files, {**files, "pyproject.toml": '[project]\nname = "x"\n# note\n'})
    role = factors(item(a, "pyproject.toml"))[R.SENSITIVE_ROLE]
    assert (role.points, role.message, role.evidence[0].node_keys) == (
        1, "role(s) manifest", ("pyproject.toml",))  # fmt: skip
    main = item(assess(*golden_edit(golden, "backend/app/main.py")), "backend/app/main.py")
    assert factors(main)[R.SENSITIVE_ROLE].confidence is MEDIUM  # a heuristic entrypoint
    # A role on a declaration counts for its file (orm_entity on models.py#User).
    models = item(assess(*golden_edit(golden, "backend/app/users/models.py")),
                  "backend/app/users/models.py")  # fmt: skip
    assert factors(models)[R.SENSITIVE_ROLE].evidence[0].node_keys == (
        "backend/app/users/models.py#User",)  # fmt: skip
    # Not sensitive by parameter: no factor.
    plain = assess(*golden_edit(golden, "backend/app/main.py"),
                   ChangeRiskParams(sensitive_roles=()))  # fmt: skip
    assert R.SENSITIVE_ROLE not in factors(item(plain, "backend/app/main.py"))


def test_health_hub_inline_and_in_the_params_hash(tmp_path: Path) -> None:
    files = {**PKG, **importers(3)}
    before, after = pair(tmp_path, {**files, "pkg/hub.py": HUB_BEFORE},
                         {**files, "pkg/hub.py": HUB_AFTER})  # fmt: skip
    assert R.HEALTH_HUB not in factors(item(assess(before, after), "pkg/hub.py"))  # min 20
    params = ChangeRiskParams(health=HealthParams(min_fan_in=3, top_percent=100))
    assert params.params_hash() != ChangeRiskParams().params_hash()
    a = assess(before, after, params)
    hub = factors(item(a, "pkg/hub.py"))[R.HEALTH_HUB]
    assert (hub.points, hub.value) == (1, 1) and "pkg/hub.py" in hub.message
    assert {r.source_key for e in hub.evidence for r in e.edge_refs} == {
        f"pkg/u{k}.py" for k in range(3)}  # fmt: skip
    assert a.run.params_hash == params.params_hash()


def test_health_cycle(tmp_path: Path) -> None:
    a_py = "from .b import g\n\n\ndef f():\n    return g()\n"
    b_py = "from .a import f\n\n\ndef g():\n    return f\n"
    i = item(risk(tmp_path, {**PKG, "pkg/a.py": a_py, "pkg/b.py": b_py},
                  {**PKG, "pkg/a.py": a_py.replace("g()", "g() + 1"), "pkg/b.py": b_py}),
             "pkg/a.py")  # fmt: skip
    cycle = factors(i)[R.HEALTH_CYCLE]
    assert (cycle.points, cycle.value) == (1, 2)
    assert {(r.source_key, r.target_key) for e in cycle.evidence for r in e.edge_refs} == {
        ("pkg/a.py", "pkg/b.py#g"), ("pkg/b.py", "pkg/a.py#f")}  # fmt: skip


# ---------------------------------------------------------------------------
# risk.dependency.unresolved_after_removal: exact recorded text only


def removal(tmp_path: Path, user_after: str | None = USER, extra: Mapping[str, str] = {},
            params: ChangeRiskParams | None = None) -> RiskItem:  # fmt: skip
    before = {**PKG, "pkg/user.py": USER, "pkg/legacy.py": LEGACY, **extra}
    after = {**PKG, **extra}
    if user_after is not None:
        after["pkg/user.py"] = user_after
    return item(risk(tmp_path, before, after, params), "pkg/legacy.py")


def test_an_import_left_dangling_by_a_removed_file(tmp_path: Path) -> None:
    """``from .legacy import old`` still in user.py, legacy.py gone: recorded as
    ``.legacy`` (the specifier as written)."""
    i = removal(tmp_path)
    found = factors(i)[R.UNRESOLVED_AFTER_REMOVAL]
    assert (found.points, found.value, found.confidence) == (2, 1, HIGH)
    edge, dependent = found.evidence
    assert edge.side is Side.FROM and [(r.source_key, r.target_key) for r in edge.edge_refs] == [
        ("pkg/user.py", "pkg/legacy.py#old")]  # fmt: skip
    assert dependent.side is Side.TO and dependent.node_keys == ("pkg/user.py",)
    assert [(s.file_key, s.start_line, s.end_line) for s in dependent.spans] == [
        ("pkg/user.py", 1, 1)]  # fmt: skip
    assert i.caveats == () and i.level is RiskLevel.HIGH


NS_USER = "from ns import helper\n\n\ndef run():\n    return helper\n"
NS = {"app/__init__.py": "", "app/user.py": NS_USER, "ns/other.py": "def o():\n    pass\n"}


def test_the_python_namespace_form(tmp_path: Path) -> None:
    """``from ns import helper`` with ns/helper.py gone and ``ns`` still a namespace
    package: recorded as ``ns.helper`` under py.import.namespace_package."""
    a = risk(tmp_path, {**NS, "ns/helper.py": "def h():\n    pass\n"}, NS)
    found = factors(item(a, "ns/helper.py"))[R.UNRESOLVED_AFTER_REMOVAL]
    assert found.evidence[1].spans[0].start_line == 1
    assert python_unresolved_name("ns", "helper") == "ns.helper"
    assert python_unresolved_name(".", "helper") == ".helper"


def test_the_namespace_form_needs_a_python_producer(tmp_path: Path) -> None:
    params = ChangeRiskParams(python_import_producers=())
    a = risk(tmp_path, {**NS, "ns/helper.py": "def h():\n    pass\n"}, NS, params)
    i = item(a, "ns/helper.py")
    assert R.UNRESOLVED_AFTER_REMOVAL not in factors(i)
    # The new not_found import is not explained by an exact match: a caveat, no points.
    assert C.DEPENDENT_RESOLUTION_CHANGED in i.caveats


def test_the_namespace_form_needs_the_namespace_rule(tmp_path: Path) -> None:
    """``ns.helper`` is the recorded form only under py.import.namespace_package; the
    same text under another rule is not that representation."""
    before, after = pair(tmp_path, {**NS, "ns/helper.py": "def h():\n    pass\n"}, NS)
    data = after.model_dump(mode="json")
    (ref,) = [r for r in data["unresolved_references"] if r["raw_text"] == "ns.helper"]
    assert ref["provenance"]["rule"] == "py.import.namespace_package"
    ref["provenance"]["rule"] = "py.import.absolute"
    i = item(assess(before, SnapshotDocument.model_validate(data)), "ns/helper.py")
    assert R.UNRESOLVED_AFTER_REMOVAL not in factors(i)
    assert C.DEPENDENT_RESOLUTION_CHANGED in i.caveats


def test_another_producer_does_not_match(tmp_path: Path) -> None:
    before, after = pair(tmp_path, {**PKG, "pkg/user.py": USER, "pkg/legacy.py": LEGACY},
                         {**PKG, "pkg/user.py": USER})  # fmt: skip
    data = after.model_dump(mode="json")
    for ref in data["unresolved_references"]:
        ref["provenance"]["producer"] = "structure-extractor"
    other = SnapshotDocument.model_validate(data)
    i = item(assess(before, other), "pkg/legacy.py")
    assert R.UNRESOLVED_AFTER_REMOVAL not in factors(i)
    assert C.DEPENDENT_RESOLUTION_CHANGED in i.caveats


@pytest.mark.parametrize(
    ("user_after", "caveat"),
    [
        ("def run():\n    return 1\n", None),  # the import was removed: nothing dangles
        (USER.replace(".legacy", ".legacyy"), C.DEPENDENT_RESOLUTION_CHANGED),  # not exact
        (None, None),  # the dependent is gone too
    ],
)
def test_no_exact_match_no_points(tmp_path: Path, user_after: str | None,
                                  caveat: RiskCaveat | None) -> None:  # fmt: skip
    i = removal(tmp_path, user_after)
    assert R.UNRESOLVED_AFTER_REMOVAL not in factors(i)
    assert (C.DEPENDENT_RESOLUTION_CHANGED in i.caveats) == (caveat is not None)


def test_a_moved_dependent_is_not_checked(tmp_path: Path) -> None:
    """Relative specifiers of a moved file mean something else: no claim either way."""
    a = risk(tmp_path, {**PKG, "pkg/user.py": USER, "pkg/legacy.py": LEGACY},
             {**PKG, "pkg/sub/__init__.py": "", "pkg/sub/user.py": USER})  # fmt: skip
    assert item(a, "pkg/sub/user.py").change is NodeChangeKind.MOVED
    i = item(a, "pkg/legacy.py")
    assert R.UNRESOLVED_AFTER_REMOVAL not in factors(i) and i.caveats == ()


def test_a_removed_declaration_of_a_surviving_file_leaves_no_record(tmp_path: Path) -> None:
    """``from .legacy import old`` with old() gone but legacy.py kept resolves to the
    module: the snapshot records nothing to match, so nothing is claimed."""
    keep = "def keep():\n    pass\n"
    before = {**PKG, "pkg/user.py": USER, "pkg/legacy.py": LEGACY + "\n" + keep}
    a = risk(tmp_path, before, {**PKG, "pkg/user.py": USER, "pkg/legacy.py": keep})
    i = item(a, "pkg/legacy.py")
    assert R.UNRESOLVED_AFTER_REMOVAL not in factors(i)
    assert factors(i)[R.CHANGE_INTERFACE].value == 1  # the removal itself still counts


def test_not_comparable_resolution_is_a_caveat(tmp_path: Path) -> None:
    before, after = pair(tmp_path, {**PKG, "pkg/user.py": USER, "pkg/legacy.py": LEGACY},
                         {**PKG, "pkg/user.py": USER})  # fmt: skip
    i = item(assess(before, with_identity(after, extractor_version="0.3.0")), "pkg/legacy.py")
    assert R.UNRESOLVED_AFTER_REMOVAL not in factors(i)
    assert {C.INCOMPARABLE_SNAPSHOTS, C.RESOLUTION_NOT_COMPARABLE} <= set(i.caveats)


# ---------------------------------------------------------------------------
# The absence factor and hidden evidence


CALC = "def add(a, b):\n    return a + b\n"
CALC_TEST = "from pkg.calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"


def test_no_dependent_test_is_scored_only_when_reliable(tmp_path: Path) -> None:
    clean = item(risk(tmp_path / "1", {**PKG, "pkg/calc.py": CALC},
                      {**PKG, "pkg/calc.py": CALC.replace("+", "-")}), "pkg/calc.py")  # fmt: skip
    absent = factors(clean)[R.NO_DEPENDENT_TEST]
    assert (absent.points, absent.confidence, clean.conclusive) == (1, HIGH, True)
    # A file hiding its imports elsewhere: a test might depend on calc.py unseen.
    plugins = "import importlib\n\n\ndef load(n):\n    return importlib.import_module(n)\n"
    dynamic = {"pkg/plugins.py": plugins}
    hidden = item(risk(tmp_path / "2", {**PKG, **dynamic, "pkg/calc.py": CALC},
                       {**PKG, **dynamic, "pkg/calc.py": CALC.replace("+", "-")}),
                  "pkg/calc.py")  # fmt: skip
    absent = factors(hidden)[R.NO_DEPENDENT_TEST]
    assert (absent.points, absent.confidence, absent.caveats) == (0, LOW, (C.HIDDEN_DEPENDENCIES,))
    assert (hidden.confidence, hidden.conclusive) == (MEDIUM, False)
    assert hidden.points == clean.points - 1  # withheld, never guessed


def test_a_test_beyond_the_depth_bound_makes_the_absence_unreliable(tmp_path: Path) -> None:
    chain = {
        "pkg/c1.py": "from .calc import add\n\n\ndef f1():\n    return add\n",
        "pkg/c2.py": "from .c1 import f1\n\n\ndef f2():\n    return f1\n",
        "tests/test_c.py": "from pkg.c2 import f2\n\n\ndef test_f():\n    assert f2\n",
    }
    a = risk(tmp_path, {**PKG, **chain, "pkg/calc.py": CALC},
             {**PKG, **chain, "pkg/calc.py": CALC.replace("+", "-")},
             ChangeRiskParams(max_depth=2))  # fmt: skip
    i = item(a, "pkg/calc.py")
    assert C.IMPACT_BOUNDED in i.caveats
    assert factors(i)[R.NO_DEPENDENT_TEST].points == 0
    assert C.IMPACT_BOUNDED in factors(i)[R.IMPACT_DEPENDENTS].caveats
    assert R.NO_DEPENDENT_TEST not in factors(item(risk(
        tmp_path / "deep", {**PKG, **chain, "pkg/calc.py": CALC},
        {**PKG, **chain, "pkg/calc.py": CALC.replace("+", "-")}), "pkg/calc.py"))  # fmt: skip


def test_a_truncated_impact_is_bounded(tmp_path: Path) -> None:
    files = {**PKG, **importers(3)}
    a = risk(tmp_path, {**files, "pkg/hub.py": HUB_BEFORE}, {**files, "pkg/hub.py": HUB_AFTER},
             ChangeRiskParams(max_nodes=2))  # fmt: skip
    assert C.IMPACT_BOUNDED in item(a, "pkg/hub.py").caveats


def test_a_removed_file_has_no_absence_factor(tmp_path: Path) -> None:
    i = item(risk(tmp_path, {**PKG, "pkg/calc.py": CALC}, PKG), "pkg/calc.py")
    assert R.NO_DEPENDENT_TEST not in factors(i)


# ---------------------------------------------------------------------------
# The test-file cap is the test subject's own


def test_the_test_file_cap_never_lowers_production_subjects(tmp_path: Path) -> None:
    """Both change their signature (kind 1 + interface 2 = 3). With high_at = 3 the
    production file is high; the test file is capped at low; the run stays high."""
    before = {**PKG, "pkg/calc.py": CALC, "tests/test_calc.py": CALC_TEST}
    after = {**PKG, "pkg/calc.py": CALC.replace("a, b", "a, b, c=0"),
             "tests/test_calc.py": CALC_TEST.replace("test_add()", "test_add(x=1)")}  # fmt: skip
    a = risk(tmp_path, before, after, ChangeRiskParams(medium_at=2, high_at=3))
    test, production = item(a, "tests/test_calc.py"), item(a, "pkg/calc.py")
    assert (test.points, test.level, test.capped) == (3, RiskLevel.LOW, True)
    assert R.TEST_FILE_CAP in factors(test) and R.NO_DEPENDENT_TEST not in factors(test)
    assert (production.points, production.level, production.capped) == (3, RiskLevel.HIGH, False)
    assert a.level is RiskLevel.HIGH
    # Nothing hides risk here (high confidence, no caveat), yet the capped test file's
    # low is a policy, not its points: never conclusive, and so neither is the run.
    assert (test.confidence, test.caveats, test.conclusive) == (HIGH, (), False)
    assert production.conclusive and a.confidence is HIGH and not a.conclusive
    # Without the test file changing, the production subject is the same.
    alone = risk(tmp_path / "alone", before, {**after, "tests/test_calc.py": CALC_TEST},
                 ChangeRiskParams(medium_at=2, high_at=3))  # fmt: skip
    assert item(alone, "pkg/calc.py") == production
    assert alone.conclusive  # no capped item: conclusive again


# ---------------------------------------------------------------------------
# Thresholds, origins, comparability, contract


@pytest.mark.parametrize(
    ("medium_at", "high_at", "level"),
    [(2, 4, "high"), (2, 5, "high"), (2, 6, "medium"), (5, 6, "medium"), (6, 7, "low")],
)
def test_thresholds_are_parameters(fixture_docs: tuple[SnapshotDocument, SnapshotDocument],
                                   medium_at: int, high_at: int, level: str) -> None:  # fmt: skip
    """shop/legacy.py has 5 points: at, below and above each threshold."""
    a = assess(*fixture_docs, ChangeRiskParams(medium_at=medium_at, high_at=high_at))
    assert item(a, "shop/legacy.py").level is RiskLevel(level)
    assert (a.run.medium_at, a.run.high_at) == (medium_at, high_at)


def test_threshold_params_are_validated() -> None:
    with pytest.raises(ValueError, match="greater than medium_at"):
        ChangeRiskParams(medium_at=4, high_at=4)


def test_non_source_files_are_excluded_by_default(tmp_path: Path) -> None:
    before = {**PKG, "README.md": "# a\n", "pkg/a.py": CALC}
    after = {**PKG, "README.md": "# b\n", "pkg/a.py": CALC}
    a = risk(tmp_path, before, after)
    assert (a.items, a.excluded_by_origin) == ((), 1)
    from xraymyrepo.cim import Origin

    docs = risk(tmp_path / "docs", before, after,
                ChangeRiskParams(subject_origins=(Origin.SOURCE, Origin.DOCS)))  # fmt: skip
    readme = item(docs, "README.md")
    assert docs.excluded_by_origin == 0
    assert C.INCOMPLETE_EXTRACTION in readme.caveats  # excluded from extraction: not seen


def test_an_incomparable_comparison(
    fixture_docs: tuple[SnapshotDocument, SnapshotDocument],
) -> None:
    before, after = fixture_docs
    a = assess(before, with_identity(after, commit_sha="3" * 40, extractor_version="0.3.0"))
    assert a.caveats == (C.INCOMPARABLE_SNAPSHOTS,)
    assert all(C.INCOMPARABLE_SNAPSHOTS in i.caveats for i in a.items)
    assert all(i.confidence is not HIGH for i in a.items) and not a.conclusive
    # Points are not changed by comparability: still the observed lower bound.
    assert item(a, "shop/legacy.py").points == 5


def test_contract_errors(fixture_docs: tuple[SnapshotDocument, SnapshotDocument]) -> None:
    before, after = fixture_docs
    c = compare(before, after)
    pending = c.model_copy(
        update={"run": c.run.model_copy(update={"status": SnapshotStatus.PENDING})}
    )
    with pytest.raises(ChangeRiskContractError, match="complete comparisons"):
        change_risk(pending, before, after)
    with pytest.raises(ChangeRiskContractError, match="does not fit the snapshots"):
        change_risk(c, after, before)
    a = change_risk(c, before, after)
    validate_against(c, before, after, a)
    reversed_c = compare(after, before)
    with pytest.raises(ChangeRiskContractError, match="another comparison"):
        validate_against(reversed_c, after, before, a)


def test_every_factor_cites_evidence_of_its_side(fixture_risk: ChangeRiskAssessment) -> None:
    for i in fixture_risk.items:
        for f in i.factors:
            assert f.evidence and all(e.node_keys or e.edge_refs or e.spans for e in f.evidence)


# ---------------------------------------------------------------------------
# Monotonicity: more observed dependence never lowers a level


def test_more_dependents_never_lower_points_or_level(tmp_path: Path) -> None:
    previous = (-1, -1)
    params = ChangeRiskParams(wide_dependents=3)
    for n in range(6):
        files = {**PKG, **importers(n)}
        i = item(risk(tmp_path / str(n), {**files, "pkg/hub.py": HUB_BEFORE},
                      {**files, "pkg/hub.py": HUB_AFTER}, params), "pkg/hub.py")  # fmt: skip
        current = (i.points, i.level.rank)
        assert current >= previous, (n, current, previous)
        previous = current


def test_adding_an_interface_change_never_lowers_a_level(tmp_path: Path) -> None:
    files = {**PKG, **importers(2)}
    body = item(risk(tmp_path / "body", {**files, "pkg/hub.py": HUB_BEFORE},
                     {**files, "pkg/hub.py": HUB_AFTER}), "pkg/hub.py")  # fmt: skip
    signature = item(risk(tmp_path / "sig", {**files, "pkg/hub.py": HUB_BEFORE},
                          {**files, "pkg/hub.py": "def f(x=0):\n    return 2\n"}),
                     "pkg/hub.py")  # fmt: skip
    assert signature.points == body.points + 2
    assert signature.level.rank >= body.level.rank


def test_assessment_round_trips_through_json(fixture_risk: ChangeRiskAssessment) -> None:
    again = ChangeRiskAssessment.model_validate_json(fixture_risk.model_dump_json())
    assert again == fixture_risk and again.canonical() == fixture_risk


def test_comparison_is_used_as_given(
    fixture_docs: tuple[SnapshotDocument, SnapshotDocument],
) -> None:
    before, after = fixture_docs
    c: SnapshotComparison = compare(before, after)
    assert change_risk(c, before, after).run.comparison == c.run
