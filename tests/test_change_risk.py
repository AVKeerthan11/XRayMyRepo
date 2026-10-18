"""Change-risk models (cim/change_risk.py): run identity, evidence, factors, items,
level and confidence derivation, the lower-bound vocabulary, canonical form."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from xraymyrepo.cim import (
    CAVEAT_CONFIDENCE,
    ChangeRiskAssessment,
    ChangeRiskRun,
    Confidence,
    EdgeKind,
    EdgeRef,
    NodeChangeKind,
    RiskCaveat,
    RiskEvidence,
    RiskFactor,
    RiskFactorRule,
    RiskItem,
    RiskLevel,
    Side,
    SnapshotStatus,
    SpanRef,
    confidence_of,
    level_for,
)

from .test_comparison import FROM, TO, run
from .test_tiers import identity

HASH = "sha256:" + "1" * 64
HIGH, MEDIUM, LOW = Confidence.HIGH, Confidence.MEDIUM, Confidence.LOW
KIND, CAP = RiskFactorRule.CHANGE_KIND, RiskFactorRule.TEST_FILE_CAP


def evidence(key: str = "app.py", side: Side = Side.TO) -> RiskEvidence:
    return RiskEvidence(side=side, node_keys=(key,))


def factor(rule: RiskFactorRule = KIND, points: int = 1, **fields: Any) -> RiskFactor:
    data: dict[str, Any] = {"rule": rule, "points": points, "value": 1, "confidence": HIGH,
                            "message": "m", "evidence": (evidence(),)}  # fmt: skip
    return RiskFactor(**{**data, **fields})


def item(*factors: RiskFactor, key: str = "app.py", level: RiskLevel | None = None,
         caveats: tuple[RiskCaveat, ...] = (), **fields: Any) -> RiskItem:  # fmt: skip
    found = factors or (factor(),)
    points = sum(f.points for f in found)
    return RiskItem(
        subject_key=key,
        change=fields.pop("change", NodeChangeKind.MODIFIED),
        level=level or level_for(points, 2, 4),
        points=fields.pop("points", points),
        confidence=fields.pop("confidence", confidence_of(caveats)),
        caveats=caveats,
        factors=found,
        **fields,
    )


def risk_run(comparison_run: Any = None, **fields: Any) -> ChangeRiskRun:
    data: dict[str, Any] = {"comparison": comparison_run or run(), "algorithm": "change_risk",
                            "algorithm_version": "1.0.0", "params_hash": HASH, "medium_at": 2,
                            "high_at": 4, "status": SnapshotStatus.COMPLETE}  # fmt: skip
    return ChangeRiskRun(**{**data, **fields})


def assessment(*items: RiskItem, **fields: Any) -> ChangeRiskAssessment:
    return ChangeRiskAssessment(run=fields.pop("run", risk_run()), items=items, **fields)


# ---------------------------------------------------------------------------
# Vocabulary and derivations


def test_levels_have_no_safe_or_unknown_value() -> None:
    assert [level.value for level in RiskLevel] == ["low", "medium", "high"]
    assert [level.rank for level in RiskLevel] == [0, 1, 2]


@pytest.mark.parametrize(
    ("points", "expected"),
    [(0, "low"), (1, "low"), (2, "medium"), (3, "medium"), (4, "high"), (9, "high")],
)
def test_level_thresholds(points: int, expected: str) -> None:
    assert level_for(points, 2, 4) is RiskLevel(expected)


def test_every_caveat_lowers_confidence() -> None:
    assert set(CAVEAT_CONFIDENCE) == set(RiskCaveat)
    assert HIGH not in CAVEAT_CONFIDENCE.values()
    assert confidence_of(()) is HIGH
    assert confidence_of((RiskCaveat.HIDDEN_DEPENDENCIES,)) is MEDIUM
    assert confidence_of((RiskCaveat.IMPACT_BOUNDED, RiskCaveat.INCOMPLETE_EXTRACTION)) is LOW


# ---------------------------------------------------------------------------
# Run, evidence, factors


def test_a_run_is_of_a_complete_comparison_with_ordered_thresholds() -> None:
    risk_run()
    with pytest.raises(ValidationError, match="complete comparisons"):
        risk_run(run().model_copy(update={"status": SnapshotStatus.PENDING}))
    for medium_at, high_at in ((2, 2), (3, 2)):
        with pytest.raises(ValidationError, match="high_at must be greater"):
            risk_run(medium_at=medium_at, high_at=high_at)
    with pytest.raises(ValidationError):
        risk_run(medium_at=0)


def test_evidence_cites_something_on_a_named_side() -> None:
    RiskEvidence(side=Side.FROM, edge_refs=(EdgeRef.of(EdgeKind.IMPORTS, "a.py", "b.py"),))
    RiskEvidence(side=Side.TO, spans=(SpanRef(ref="span", file_key="a.py", start_line=1,
                                              end_line=1),))  # fmt: skip
    with pytest.raises(ValidationError, match="at least one node, edge or span"):
        RiskEvidence(side=Side.TO)


def test_factor_rules() -> None:
    with pytest.raises(ValidationError):
        factor(points=-1)
    with pytest.raises(ValidationError, match="cap adds no points"):
        factor(CAP, points=1)
    with pytest.raises(ValidationError, match="must say why"):
        factor(RiskFactorRule.NO_DEPENDENT_TEST, points=0)
    factor(RiskFactorRule.NO_DEPENDENT_TEST, points=0, caveats=(RiskCaveat.HIDDEN_DEPENDENCIES,))
    with pytest.raises(ValidationError):
        factor(evidence=())  # every factor cites evidence
    with pytest.raises(ValidationError):
        factor(evidence=(evidence(),) * 21)
    with pytest.raises(ValidationError, match="enum order"):
        factor(caveats=(RiskCaveat.HEURISTIC_PATHS, RiskCaveat.HIDDEN_DEPENDENCIES))


# ---------------------------------------------------------------------------
# Items


def test_item_points_are_the_sum_of_its_factors() -> None:
    item(factor(points=1), factor(RiskFactorRule.CHANGE_INTERFACE, points=2))
    with pytest.raises(ValidationError, match="sum of its factors"):
        item(factor(points=1), points=3)


def test_item_starts_with_its_change_and_lists_each_rule_once() -> None:
    with pytest.raises(ValidationError, match="starts with its change"):
        item(factor(RiskFactorRule.CHANGE_INTERFACE, points=2))
    with pytest.raises(ValidationError, match="at most once"):
        item(factor(points=1), factor(points=0))


def test_item_confidence_is_what_its_caveats_allow() -> None:
    hidden = (RiskCaveat.HIDDEN_DEPENDENCIES,)
    assert item(caveats=hidden).confidence is MEDIUM
    with pytest.raises(ValidationError, match="lowest its caveats allow"):
        item(caveats=hidden, confidence=HIGH)
    with pytest.raises(ValidationError, match="lowest its caveats allow"):
        item(confidence=LOW)  # no caveat may not lower confidence silently either


def test_factor_caveats_are_item_caveats() -> None:
    absent = factor(RiskFactorRule.NO_DEPENDENT_TEST, points=0,
                    caveats=(RiskCaveat.HIDDEN_DEPENDENCIES,))  # fmt: skip
    with pytest.raises(ValidationError, match="factor caveats must be item caveats"):
        item(factor(), absent)
    item(factor(), absent, caveats=(RiskCaveat.HIDDEN_DEPENDENCIES,))


def test_conclusive_needs_high_confidence_no_caveat_and_no_cap() -> None:
    assert item().conclusive and not item().capped
    low = item(caveats=(RiskCaveat.INCOMPLETE_EXTRACTION,))
    assert low.level is RiskLevel.LOW and not low.conclusive  # low, but not shown safe
    # A capped test file: high confidence and no caveat, but its low is a policy, not
    # what its points say, so it is never conclusive.
    capped = item(factor(points=2), factor(RiskFactorRule.CHANGE_INTERFACE, points=2),
                  factor(CAP, points=0), key="tests/test_a.py", level=RiskLevel.LOW)  # fmt: skip
    assert (capped.capped, capped.confidence, capped.caveats) == (True, HIGH, ())
    assert not capped.conclusive


def test_subject_is_a_file_key() -> None:
    with pytest.raises(ValidationError):
        item(key="app.py#f")


# ---------------------------------------------------------------------------
# Assessments


def test_levels_follow_the_run_thresholds() -> None:
    three = item(factor(points=1), factor(RiskFactorRule.CHANGE_INTERFACE, points=2))
    assert three.level is RiskLevel.MEDIUM
    assessment(three)
    with pytest.raises(ValidationError, match="3 points make high"):
        assessment(three, run=risk_run(medium_at=1, high_at=3))
    assessment(three.model_copy(update={"level": RiskLevel.HIGH}),
               run=risk_run(medium_at=1, high_at=3))  # fmt: skip


def test_the_test_file_cap_is_the_capped_items_own() -> None:
    capped = item(factor(points=2), factor(RiskFactorRule.CHANGE_INTERFACE, points=2),
                  factor(CAP, points=0), key="tests/test_a.py", level=RiskLevel.LOW)  # fmt: skip
    production = item(factor(points=2), factor(RiskFactorRule.CHANGE_INTERFACE, points=2))
    a = assessment(capped, production)
    assert (capped.points, capped.level) == (4, RiskLevel.LOW)
    assert production.level is RiskLevel.HIGH and a.level is RiskLevel.HIGH
    # Conclusive is every item's: the capped one is not, so neither is the assessment.
    assert production.conclusive and not capped.conclusive
    assert a.confidence is HIGH and not a.conclusive
    assert assessment(production).conclusive
    with pytest.raises(ValidationError, match="make low"):
        assessment(capped.model_copy(update={"level": RiskLevel.HIGH}))
    with pytest.raises(ValidationError, match="make high"):  # no cap factor: not capped
        assessment(production.model_copy(update={"level": RiskLevel.LOW}))


def test_subjects_are_unique() -> None:
    with pytest.raises(ValidationError, match="more than once"):
        assessment(item(), item())


def test_incomparable_comparisons_carry_the_caveat_everywhere() -> None:
    other = identity(commit="c" * 40).model_copy(update={"extractor_version": "9.0.0"})
    incomparable = risk_run(run(FROM, other))
    caveat = (RiskCaveat.INCOMPARABLE_SNAPSHOTS,)
    a = assessment(item(caveats=caveat), run=incomparable, caveats=caveat)
    assert a.confidence is MEDIUM and not a.conclusive
    with pytest.raises(ValidationError, match="exactly when the comparison is"):
        assessment(item(caveats=caveat), run=incomparable)
    with pytest.raises(ValidationError, match="every item of an incomparable"):
        assessment(item(), run=incomparable, caveats=caveat)
    with pytest.raises(ValidationError, match="exactly when the comparison is"):
        assessment(item(), caveats=caveat)  # comparable: no such caveat
    assert run(FROM, TO).comparable


def test_derived_level_confidence_and_summary() -> None:
    empty = assessment()
    assert (empty.level, empty.confidence, empty.conclusive) == (RiskLevel.LOW, HIGH, True)
    a = assessment(
        item(factor(points=1), key="a.py"),
        item(factor(points=2), factor(RiskFactorRule.CHANGE_INTERFACE, points=2), key="b.py"),
        item(key="c.py", caveats=(RiskCaveat.UNDETERMINED_CHANGES,)),
    )
    assert (a.level, a.confidence) == (RiskLevel.HIGH, LOW)
    summary = a.summary()
    assert summary.levels == {RiskLevel.LOW: 2, RiskLevel.MEDIUM: 0, RiskLevel.HIGH: 1}
    assert summary.confidences == {HIGH: 2, MEDIUM: 0, LOW: 1}


def test_canonical_orders_items_and_factors() -> None:
    a = assessment(
        item(factor(points=1), factor(RiskFactorRule.HEALTH_HUB, points=1),
             factor(RiskFactorRule.CHANGE_INTERFACE, points=2), key="z.py"),
        item(key="a.py"),
    )  # fmt: skip
    c = a.canonical()
    assert [i.subject_key for i in c.items] == ["a.py", "z.py"]
    assert [f.rule for f in c.items[1].factors] == [
        KIND, RiskFactorRule.CHANGE_INTERFACE, RiskFactorRule.HEALTH_HUB]  # fmt: skip
    assert c.canonical() == c
    assert ChangeRiskAssessment.model_validate_json(c.model_dump_json()) == c
