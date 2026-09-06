"""Thin persistence wrapper around the pure `compute_quarter()`.

Loads opening state, calls the pure function, writes closing state, returns the result. No
business logic lives here -- every number in the result comes from
`app.engines.quarter.compute_quarter`; this module's only job is loading its inputs from the DB
and persisting its output. It also drives `app.engines.scoring.score_quarter`, (Phase 8)
`app.engines.evidence.extract_evidence`, and (Phase 11, Q4 only) `app.engines.endgame` in the same
transaction -- independently-pure engines, one persistence wrapper.

Distinct from `app.services.quarter_engine.run_quarter`, which orchestrates the legacy per-decision
Business Impact / Evidence / Cognitive Scoring pipeline -- a genuinely different system (CLAUDE.md:
"Two pipelines stay independent"), kept and tested, just no longer what `POST /lock` calls.
"""

import hashlib
import json
import types
import uuid
from dataclasses import fields, is_dataclass
from decimal import Decimal
from enum import Enum
from typing import Any, Union, get_args, get_origin, get_type_hints

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.loader import load_profile, load_scenario, load_seed
from app.config.schema import Scenario
from app.engines import endgame
from app.engines.evidence import extract_evidence
from app.engines.quarter import QuarterResult, compute_quarter
from app.engines.scoring import score_quarter
from app.engines.state import CompanyState, CrisisEvent, QuarterAllocations
from app.engines.survival import RunStatus, SurvivalOutcome, evaluate_survival, tier_assignment_quarter
from app.models.company import Company
from app.models.company_state_snapshot import CompanyStateSnapshot
from app.models.endgame_decision import EndgameDecision
from app.models.evidence import EvidenceRecord
from app.models.quarter import Quarter, QuarterStatus
from app.models.quarter_allocation import QuarterAllocation
from app.models.quarter_performance import QuarterPerformance
from app.services.company_service import assign_crisis_scenario

# Mirrors QuarterAllocations' fields exactly (see app/engines/state.py) so a QuarterAllocation
# row converts to the dataclass with no per-field mapping to keep in sync by hand.
_ALLOCATION_FIELDS = (
    "google_ads",
    "meta_ads",
    "social_influencer",
    "content_seo",
    "events_pr",
    "email_marketing",
    "referral",
    "prelaunch_buzz",
    "reps",
    "crm_tools",
    "onboarding",
    "quality_qa",
    "innovation",
    "manufacturing",
    "supplier_qc",
    "logistics",
    "culture_benefits",
    "training_development",
    "cx_team",
    "compliance_legal",
    "financial_planning",
    "audit_prep",
    "warranty_years",
    "crisis_choice",
    "price_match_fund",
    "comparison_ads",
    "retention_offers",
    "emergency_supply_fund",
    "crisis_choice_d_spend",
)


# --- Generic dataclass <-> JSON round-trip ---------------------------------------------------
# QuarterResult/Valuation/CompanyState together have 60+ fields, almost all Decimal, several
# nested. Hand-writing a converter per field would silently go stale the next time a field is
# added to the pure engine; walking `dataclasses.fields()` keeps this automatically in sync.


def _to_jsonable(value: Any) -> Any:
    """Decimal -> str (exact, not float-lossy), dataclasses -> dict, tuples/lists -> list,
    recursively. The tuple case exists for QuarterScore -- its `traits`/`modifiers`/`criteria`
    fields are tuples, unlike anything on QuarterResult, which only ever nests dicts."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: _to_jsonable(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, dict):
        return {k: _to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(v) for v in value]
    return value


def _from_jsonable(annotation: Any, value: Any) -> Any:
    if value is None:
        return None
    origin = get_origin(annotation)
    if origin in (Union, types.UnionType):
        non_none = next(a for a in get_args(annotation) if a is not type(None))
        return _from_jsonable(non_none, value)
    if is_dataclass(annotation):
        hints = get_type_hints(annotation)
        return annotation(**{f.name: _from_jsonable(hints[f.name], value[f.name]) for f in fields(annotation)})
    if annotation is Decimal:
        return Decimal(value)
    if origin is dict:
        _, value_type = get_args(annotation)
        return {k: _from_jsonable(value_type, v) for k, v in value.items()}
    if origin in (tuple, list):
        # Every tuple/list-typed field in this codebase's dataclasses is variable-length and
        # homogeneous (`tuple[CriterionScore, ...]`, `tuple[str, ...]`, ...), so the first type
        # argument is always the element type. Reconstructs QuarterScore's `traits`/`modifiers`/
        # `criteria` -- `_to_jsonable` already flattens these to plain lists; without this branch
        # they silently came back as lists of dicts instead of dataclass instances.
        element_type = get_args(annotation)[0]
        elements = (_from_jsonable(element_type, v) for v in value)
        return tuple(elements) if origin is tuple else list(elements)
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        return annotation(value)
    return value  # int, str, bool round-trip through JSON as-is


def _result_hash(result_json: dict[str, Any], run_status: RunStatus, score_hash_input: dict[str, Any]) -> str:
    """sha256 over the canonical (sorted-key) serialised result -- not Python's `hash()`, which
    is salted per process and would never compare equal across two runs, let alone two processes.

    Run status is hashed alongside the result because it is part of what locking this quarter
    decided: the same numbers can leave a company ACTIVE or DISTRESSED depending on the history
    behind them, and a hash that ignored that would call two different outcomes identical.

    The score is folded in for the same reason, per the Phase 7 spec -- but only its
    `trait_points`/`modifiers_applied` breakdown, not the whole `QuarterScore`. Those two fully
    determine `ceo_score` and `score_band` (pure functions of them), and they round-trip through
    JSONB byte-exact the same way `engine_result` does. `ceo_score` itself does not: it is stored
    in a `Numeric(6, 2)` column for display, which rounds on write, so hashing its full-precision
    Decimal would make the hash impossible to reproduce from what is actually persisted.
    """
    canonical = json.dumps(
        {"result": result_json, "run_status": run_status.value, "score": score_hash_input}, sort_keys=True
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def to_allocations(row: QuarterAllocation | None) -> QuarterAllocations:
    if row is None:
        return QuarterAllocations()
    return QuarterAllocations(**{field: getattr(row, field) for field in _ALLOCATION_FIELDS})


async def _prior_allocations(session: AsyncSession, quarter: Quarter) -> QuarterAllocations | None:
    """The immediately preceding quarter's allocation row, or `None` for Q1 or if that quarter
    never had one submitted.

    Distinct from `_to_allocations(None)`, which returns an all-zero `QuarterAllocations` for
    *this* quarter's still-unsubmitted row. Here, `None` matters: `score_quarter`'s
    compounding-asset-cut modifier treats "no prior allocations to compare against" as "the
    modifier sits out", not as "every compounding line was cut from zero".
    """
    if quarter.number == 1:
        return None
    row = (
        await session.execute(
            select(QuarterAllocation)
            .join(Quarter, Quarter.id == QuarterAllocation.quarter_id)
            .where(Quarter.company_id == quarter.company_id, Quarter.number == quarter.number - 1)
        )
    ).scalar_one_or_none()
    return to_allocations(row) if row is not None else None


async def load_opening_state(session: AsyncSession, quarter: Quarter, seed: Any) -> CompanyState:
    if quarter.number == 1:
        return CompanyState.opening(seed)

    prior = (
        await session.execute(
            select(Quarter).where(Quarter.company_id == quarter.company_id, Quarter.number == quarter.number - 1)
        )
    ).scalar_one_or_none()
    if prior is None:
        raise ValueError(
            f"quarter {quarter.id} is quarter {quarter.number} for its company, but no quarter "
            f"{quarter.number - 1} exists to carry state forward from"
        )

    snapshot = (
        await session.execute(select(CompanyStateSnapshot).where(CompanyStateSnapshot.quarter_id == prior.id))
    ).scalar_one_or_none()
    if snapshot is None:
        raise ValueError(f"prior quarter {prior.id} has no closing-state snapshot -- lock it before this one")

    return _from_jsonable(CompanyState, snapshot.state)


async def _prior_results(session: AsyncSession, quarter: Quarter) -> list[QuarterResult]:
    """Every already-locked quarter's result for this company, oldest first.

    Survival needs the whole run, not just the quarter being locked: `buffer_breached` is "at any
    point", and `sustained_decline` counts a streak across quarters.
    """
    rows = (
        (
            await session.execute(
                select(QuarterPerformance.engine_result)
                .join(Quarter, Quarter.id == QuarterPerformance.quarter_id)
                .where(
                    Quarter.company_id == quarter.company_id,
                    Quarter.number < quarter.number,
                    QuarterPerformance.engine_result.isnot(None),
                )
                .order_by(Quarter.number)
            )
        )
        .scalars()
        .all()
    )
    return [_from_jsonable(QuarterResult, row) for row in rows]


def _result_by_number(results: list[QuarterResult], number: int) -> QuarterResult:
    """`results` is oldest-first with no gaps (the immutability guard on quarter creation only
    ever allows sequential locking), so quarter `number`'s result sits at index `number - 1`."""
    if number < 1 or number > len(results):
        raise ValueError(f"no locked quarter {number} in a history of length {len(results)}")
    return results[number - 1]


def _run_status(outcome: SurvivalOutcome, quarter: Quarter, scenario: Scenario) -> RunStatus:
    """Fold the survival outcome and the scenario's length into one persisted status.

    FAILED wins outright -- a run that ran out of cash on its last quarter did not complete.
    Otherwise reaching `total_quarters` means COMPLETED, which shadows DISTRESSED: both are true,
    but "the run is over" is what gates quarter creation. The distress signal is not lost, because
    `survival_condition` records what fired regardless of the status it ends up under, which is
    what Q4 tiering reads.
    """
    if outcome.status is RunStatus.FAILED:
        return RunStatus.FAILED
    if quarter.number >= scenario.total_quarters:
        return RunStatus.COMPLETED
    return outcome.status


async def run_quarter(session: AsyncSession, quarter_id: uuid.UUID) -> QuarterResult:
    """Run one quarter end to end and persist the result, or return the already-persisted result
    unchanged if this quarter is already locked.

    Idempotent: calling this twice never recomputes, never rewrites, and never duplicates a row --
    every write upserts on `quarter_id` (each of QuarterPerformance and CompanyStateSnapshot is
    already unique on it), and the lock transition plus both writes commit in one transaction.
    """
    quarter = await session.get(Quarter, quarter_id)
    if quarter is None:
        raise ValueError(f"quarter {quarter_id} not found")

    performance = (
        await session.execute(select(QuarterPerformance).where(QuarterPerformance.quarter_id == quarter_id))
    ).scalar_one_or_none()

    if quarter.status == QuarterStatus.CLOSED:
        if performance is None or performance.engine_result is None:
            raise ValueError(
                f"quarter {quarter_id} is locked but has no persisted engine result -- it was "
                "closed by a different pipeline, or before this wrapper computed anything"
            )
        return _from_jsonable(QuarterResult, performance.engine_result)

    company = await session.get(Company, quarter.company_id)
    if company is None:
        raise ValueError(f"quarter {quarter_id} references company {quarter.company_id}, which does not exist")

    seed = load_seed(company.seed_name)
    profile = load_profile(company.profile_name)

    allocation_row = (
        await session.execute(select(QuarterAllocation).where(QuarterAllocation.quarter_id == quarter_id))
    ).scalar_one_or_none()
    allocations = to_allocations(allocation_row)

    opening_state = await load_opening_state(session, quarter, seed)

    # Crisis (Phase 10): fires only on the scenario's own crisis_quarter. `crisis_scenario` in
    # config picks the branch when the scenario pins one; `None` means assign deterministically
    # per company, same SHA-256-seeded-random.Random pattern `assign_scenario_id` already uses,
    # so replaying a run lands on the same scenario every time.
    scenario = load_scenario(company.scenario_id)
    crisis_event = None
    if quarter.number == scenario.crisis_quarter:
        letter = scenario.crisis_scenario or assign_crisis_scenario(company.id)
        crisis_event = CrisisEvent(scenario=letter)

    result = compute_quarter(opening_state, allocations, profile, seed, crisis_event)

    # Survival is evaluated inside this same transaction, over the whole run including the
    # quarter being locked -- so the lock, the result and the status it produced all commit
    # together or not at all.
    prior_results = await _prior_results(session, quarter)
    history = [*prior_results, result]
    outcome = evaluate_survival(
        history, profile.survival, tier_assignment_quarter(scenario.total_quarters)
    )
    run_status = _run_status(outcome, quarter, scenario)

    company.run_status = run_status
    company.survival_condition = outcome.triggered_by
    company.survival_detail = outcome.detail

    # The CEO score is computed in this same transaction, alongside survival -- it is derived
    # from `result` and this quarter's allocations exactly like survival is derived from the
    # result history, so it locks or doesn't lock together with everything else.
    prior_result = prior_results[-1] if prior_results else None
    prior_alloc = await _prior_allocations(session, quarter)
    modifier_sets = ["crisis"] if crisis_event is not None else []
    rubric = profile.scoring
    endgame_facts = None

    # Q4 endgame (Phase 11, docs/16/17): only when this is the scenario's last quarter AND the
    # company submitted an EndgameDecision -- reuses `outcome` (this same transaction's survival
    # evaluation) for Tier Assignment's Distressed check, per docs/19's "reuse it, don't
    # reimplement it". No decision submitted scores normally with just the standard set, per the
    # approved Phase 11 plan.
    if quarter.number == scenario.total_quarters:
        decision = (
            await session.execute(select(EndgameDecision).where(EndgameDecision.company_id == quarter.company_id))
        ).scalar_one_or_none()
        if decision is not None:
            tier_quarter = tier_assignment_quarter(scenario.total_quarters)
            q1_result = _result_by_number(prior_results, 1)
            q2_result = _result_by_number(prior_results, 2)
            q3_result = _result_by_number(prior_results, tier_quarter)
            tier_outcome = endgame.assign_tier(q1_result, q2_result, q3_result, outcome)
            endgame_facts = endgame.build_endgame_facts(
                decision.path,
                decision.term_sheet_name,
                tier_outcome,
                q1_result,
                q3_result,
                result.units_sold,
                profile.endgame,
            )
            rubric = endgame.build_q4_rubric(profile.scoring)
            modifier_sets.append("q4")

    modifier_sets = ("standard", *modifier_sets)
    score = score_quarter(
        result, prior_result, allocations, rubric, prior_allocations=prior_alloc,
        modifier_sets=modifier_sets, endgame_facts=endgame_facts,
    )

    result_json = _to_jsonable(result)
    state_json = result_json["closing_state"]
    score_json = _to_jsonable(score)
    trait_points_json = score_json["traits"]
    modifiers_applied_json = score_json["modifiers"]
    score_hash_input = {"trait_points": trait_points_json, "modifiers_applied": modifiers_applied_json}

    # Evidence is generated in this same transaction, from the same `allocations`/`opening_state`
    # that fed `result` -- but it is computed independently by `extract_evidence`, which never
    # reads `result` itself (see engines/evidence.py's module docstring on pipeline independence).
    # It does NOT enter `_result_hash`: nothing scores it yet (Phase 8 builds only the producer),
    # so folding it in would add hash surface with no corresponding score to protect.
    # Idempotent re-lock: delete this quarter's producer-owned rows (decision_id IS NULL leaves
    # the legacy per-decision pipeline's rows alone) and reinsert the freshly computed set --
    # content-identical every time, since `extract_evidence` is a pure function of the same inputs.
    facts = extract_evidence(allocations, opening_state, profile, seed, prior_allocations=prior_alloc)
    await session.execute(
        delete(EvidenceRecord).where(EvidenceRecord.quarter_id == quarter_id, EvidenceRecord.decision_id.is_(None))
    )
    for fact in facts:
        session.add(
            EvidenceRecord(
                company_id=quarter.company_id,
                quarter_id=quarter_id,
                decision_id=None,
                workspace=None,
                department=fact.department,
                evidence_key=fact.evidence_key,
                evidence_value=_to_jsonable(fact.value),
                categories=list(fact.categories),
                weight=fact.weight,
                weight_status=fact.weight_status,
                detail=fact.detail,
            )
        )

    if performance is None:
        performance = QuarterPerformance(company_id=quarter.company_id, quarter_id=quarter_id)
        session.add(performance)
    performance.result_hash = _result_hash(result_json, run_status, score_hash_input)
    performance.engine_result = result_json
    performance.ceo_score = score.normalised_score
    performance.score_band = score.band
    performance.trait_points = trait_points_json
    performance.modifiers_applied = modifiers_applied_json
    performance.unscored_criteria = [
        criterion
        for trait in trait_points_json
        for criterion in trait["criteria"]
        if criterion["result"] == "unscored"
    ]

    snapshot = (
        await session.execute(select(CompanyStateSnapshot).where(CompanyStateSnapshot.quarter_id == quarter_id))
    ).scalar_one_or_none()
    if snapshot is None:
        snapshot = CompanyStateSnapshot(company_id=quarter.company_id, quarter_id=quarter_id, state=state_json)
        session.add(snapshot)
    else:
        snapshot.state = state_json

    quarter.status = QuarterStatus.CLOSED

    await session.commit()
    return result
