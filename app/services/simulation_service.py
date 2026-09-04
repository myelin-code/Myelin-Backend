"""Orchestration for the Nadi Wear scenario: assign the crisis, replay the run, preview, lock.

The engine in `app/engines/simulation/` is pure and knows nothing about a database. Everything that
touches one lives here.

The central design point is **replay**. `SimulationQuarter.decisions` is the authoritative record;
state is derived by running the engine over those rows in order, never by trusting a stored
snapshot. That makes the run reproducible, makes a mid-run engine fix apply retroactively, and
means the cached `result`/`opening_state` columns can be rebuilt from scratch at any time.
"""

import uuid
from dataclasses import asdict, is_dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.engines.simulation import (
    SimulationAllocations,
    SimulationCompanyState,
    SimulationQuarterResult,
    SimulationScore,
    ProductState,
    build_term_sheet,
    compute_simulation_quarter,
    normalise_lines,
    opening_state,
    score_quarter,
    settle,
)
from app.engines.simulation.catalog import (
    ARCHETYPE_IDS,
    BUFFER,
    CRISIS_QUARTER,
    DEPT_LOAD,
    INNOVATION_BY_ID,
    PRODUCT_IDS,
    TOTAL_QUARTERS,
)
from app.engines.simulation.crisis import assess, available_strategies, commit_reading
from app.engines.simulation.crisis import evidence as crisis_evidence
from app.engines.simulation.state import CrisisResponse, headcount, salary_bill
from app.engines.survival import RunStatus
from app.models.company import Company
from app.models.simulation_quarter import SimulationQuarter, SimulationRun

_LAKH = Decimal(100_000)


class SimulationError(Exception):
    """A move that is well-formed but not legal in the run's current state."""

    def __init__(self, reason: str, allowed: tuple[str, ...] = ()):
        super().__init__(reason)
        self.reason = reason
        self.allowed = allowed


# ── serialisation ────────────────────────────────────────────────────


def _plain(value):
    """JSON-safe, losslessly: Decimals become strings, exactly as the rest of the API does."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if is_dataclass(value) and not isinstance(value, type):
        return {k: _plain(v) for k, v in asdict(value).items()}
    return value


def _create_checkpoint(timer_remaining: int | None, opening_state: SimulationCompanyState, 
                       prior_result: SimulationQuarterResult | None, allocations: SimulationAllocations) -> dict | None:
    """Create a checkpoint for quarter-start restoration during rewind.
    
    Returns checkpoint dict with timer, cash, and budget ceiling at quarter start, or None if timer not provided.
    """
    if timer_remaining is None:
        return None
    
    # Calculate budget ceiling for THIS quarter's opening
    # drawn is 0 for Q1, or from prior quarter's result for Q2+
    drawn = prior_result.drawn if prior_result else Decimal(0)
    fixed = salary_bill(opening_state.staff) + opening_state.overhead
    budget_ceiling = max(Decimal(0), opening_state.cash + opening_state.pending_investment + drawn - fixed - BUFFER)
    
    return {
        "timer_remaining": timer_remaining,
        "cash_balance": str(opening_state.cash),  # Convert Decimal to string for JSON
        "budget_ceiling": str(budget_ceiling),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def state_to_dict(s: SimulationCompanyState, checkpoint: dict | None = None) -> dict:
    """Serialize state to JSONB, optionally including a checkpoint."""
    state_dict = _plain(s)
    if checkpoint is not None:
        state_dict["checkpoint"] = checkpoint
    return state_dict


def state_from_dict(raw: dict) -> SimulationCompanyState:
    """Rebuild opening state from a stored snapshot. Only used to re-run a single quarter in
    isolation -- the normal path replays from `decisions` instead."""
    products = {
        pid: ProductState(
            live=bool(p["live"]), status=p["status"], price=Decimal(p["price"]),
            share=Decimal(p["share"]), inv=Decimal(p["inv"]), inv_cost=Decimal(p["inv_cost"]),
        )
        for pid, p in raw["products"].items()
    }
    decimals = {
        k: Decimal(raw[k]) for k in (
            "cash", "ar", "ap", "debt", "equipment", "ip", "retained_earnings", "installed_capacity",
            "customers", "prior_units", "brand", "seo", "quality",
            "innovation", "npd", "supplier_rel", "logistics_eff", "emp_sat", "emp_eng", "compliance",
            "forecast", "audit", "satisfaction", "repeat_rate", "attrition", "ar_days", "overhead",
            "market_share", "fill_rate", "prior_demand", "last_gm", "last_net_cf",
        )
    }
    return SimulationCompanyState(
        quarter=int(raw["quarter"]),
        staff={k: Decimal(v) for k, v in raw["staff"].items()},
        products=products,
        innovations=tuple(raw["innovations"]),
        pipeline={k: int(v) for k, v in raw["pipeline"].items()},
        buzz_hist={int(k): Decimal(v) for k, v in raw["buzz_hist"].items()},
        rev_history=tuple(Decimal(v) for v in raw["rev_history"]),
        last_mix={k: Decimal(v) for k, v in raw["last_mix"].items()},
        aftermath={k: (Decimal(v) if k != "note" else v) for k, v in raw["aftermath"].items()},
        crisis_log=tuple(raw["crisis_log"]),
        pay_terms=raw["pay_terms"],
        wc_breached=bool(raw["wc_breached"]),
        ever_insolvent=bool(raw["ever_insolvent"]),
        **decimals,
    )


def result_to_dict(r: SimulationQuarterResult) -> dict:
    """The full result, plus the binding gate spelled out so clients never re-derive it."""
    out = _plain(r)
    out["gate"] = r.gate()
    return out


def score_to_dict(s: SimulationScore) -> dict:
    return _plain(s)


def allocations_from_payload(payload: dict) -> SimulationAllocations:
    """Build an allocation from a submitted JSON body, flooring every line at zero."""
    products = None
    if payload.get("products"):
        products = {
            pid: ProductState(
                live=bool(p.get("live", pid == "pulse")),
                status=p.get("status", "active"),
                price=Decimal(str(p.get("price", 0))),
                share=Decimal(str(p.get("share", 0))),
                inv=Decimal(str(p.get("inv", 0))),
                inv_cost=Decimal(str(p.get("inv_cost", 0))),
            )
            for pid, p in payload["products"].items()
        }

    c = payload.get("crisis") or {}
    return SimulationAllocations(
        lines=normalise_lines(payload.get("lines")),
        warranty=payload.get("warranty", "6mo"),
        pay_terms=payload.get("pay_terms", "net30"),
        start_inno=tuple(i for i in (payload.get("start_inno") or []) if i in INNOVATION_BY_ID),
        products=products,
        crisis=CrisisResponse(
            variant=c.get("variant"),
            diagnosis=c.get("diagnosis"),
            reasoning=c.get("reasoning", "") or "",
            strategy=c.get("strategy"),
            commit=Decimal(str(c.get("commit") or 0)),
        ),
        priority=payload.get("priority"),
        reflection=payload.get("reflection") or {},
    )


def allocations_to_dict(a: SimulationAllocations) -> dict:
    return _plain(a)


# ── run assembly ─────────────────────────────────────────────────────


def assign_archetype(company_id: uuid.UUID) -> str:
    """Which market event this company draws, fixed by its id.

    Deterministic for the same reason `company_service.assign_crisis_scenario` is: a student who
    reloads must get the same crisis, and a cohort must be split across events rather than all
    facing the same one.
    """
    return ARCHETYPE_IDS[company_id.int % len(ARCHETYPE_IDS)]


async def get_or_create_run(session: AsyncSession, company: Company) -> SimulationRun:
    run = (await session.execute(select(SimulationRun).where(SimulationRun.company_id == company.id))).scalar_one_or_none()
    if run is None:
        run = SimulationRun(company_id=company.id, archetype=assign_archetype(company.id))
        session.add(run)
        await session.flush()
    return run


async def locked_quarters(session: AsyncSession, company_id: uuid.UUID) -> list[SimulationQuarter]:
    result = await session.execute(
        select(SimulationQuarter).where(SimulationQuarter.company_id == company_id).order_by(SimulationQuarter.number)
    )
    return list(result.scalars())


def replay(quarters: list[SimulationQuarter]) -> tuple[SimulationCompanyState, list[SimulationQuarterResult]]:
    """Rebuild the company by re-running every locked quarter's decisions in order.

    The engine is pure, so this is exact and cheap -- four quarters is four function calls.
    """
    state = opening_state()
    history: list[SimulationQuarterResult] = []
    for row in quarters:
        result = compute_simulation_quarter(state, allocations_from_payload(row.decisions))
        history.append(result)
        state = result.next_state
    return state, history


def budget(state: SimulationCompanyState, result: SimulationQuarterResult | None, allocations: SimulationAllocations) -> dict:
    """What the quarter can afford, and what has been committed against it.

    The ceiling is cash plus whatever credit is actually drawn and whatever investment has been
    signed but not yet banked (pending_investment), less the fixed costs that land whatever happens
    and the working-capital buffer the board set. Committing past it is allowed -- the buffer absorbs
    it and the record shows it -- which is why this is reported rather than enforced.

    `investment` is reported alongside `drawn` for the same reason `drawn` is: a ceiling that
    moved because a term sheet was signed has to be explainable on the screen that shows it,
    without the client re-deriving it from the opening state.
    
    Path A (Q4 external financing): When Path A is selected, pending_investment is set on Q4's opening
    state and IS included in ceiling calculation, making it available for allocation through the normal
    decision flow. The 'left' amount (ceiling - committed) will reflect this investment.
    """
    opex = allocations.opex_lakhs * _LAKH
    capex = allocations.capex_lakhs * _LAKH
    inno = sum((INNOVATION_BY_ID[i].cost for i in allocations.start_inno if i in INNOVATION_BY_ID), Decimal(0))
    people = result.people_cost if result else Decimal(0)
    repay = allocations.get("repay") * _LAKH
    drawn = result.drawn if result else Decimal(0)
    fixed = (result.salaries + result.overhead) if result else (salary_bill(state.staff) + state.overhead)
    
    # For budget breakdown display
    cash_available = state.cash
    investment_available = state.pending_investment
    credit_available = drawn
    total_funds = cash_available + investment_available + credit_available
    ceiling = max(Decimal(0), total_funds - fixed - BUFFER)

    return {
        "opex": opex, 
        "capex": capex, 
        "inno": inno, 
        "people": people, 
        "repay": repay, 
        "drawn": drawn,
        "investment": state.pending_investment,
        "committed": opex + capex + inno + people + repay,
        "ceiling": ceiling,
        # Breakdown for UI display
        "cash_available": cash_available,
        "investment_available": investment_available,
        "credit_available": credit_available,
        "total_funds": total_funds,
        "fixed_costs": fixed,
        "buffer": BUFFER,
    }


def legal_moves(state: SimulationCompanyState, run: SimulationRun, locked: int) -> tuple[str, ...]:
    """What this run can legally do right now. The client renders from this, never from the
    quarter number alone."""
    if locked >= TOTAL_QUARTERS:
        return ("read_quarter_report", "read_endgame_preview")
    moves = ["preview_quarter", "lock_quarter"]
    if locked >= 1:
        moves.append("read_quarter_report")
    if locked == CRISIS_QUARTER:
        moves.append("read_endgame_preview")
        if run.endgame_path is None:
            moves.append("submit_endgame_decision")
    if state.quarter == CRISIS_QUARTER:
        moves.append("submit_crisis_response")
    return tuple(moves)


def crisis_briefing(state: SimulationCompanyState, run: SimulationRun, history: list | None = None) -> dict | None:
    """What the student is told when the event fires, and never a coefficient.

    The narrative, the evidence each function is seeing and the postures on the table --
    nothing that would let them back out the damage number they are meant to diagnose.
    """
    if state.quarter != CRISIS_QUARTER:
        return None
    from app.engines.simulation.catalog import ARCHETYPES, DIAGNOSIS_LABELS, SCENARIO_LETTER_FOR_ARCHETYPE, STRATEGY_BY_ID

    arch = ARCHETYPES[run.archetype]
    situation = assess(run.archetype, state)
    hist = history or []
    strategies = available_strategies(run.archetype, state, situation.factors)

    return {
        "archetype": run.archetype,
        # The 22-line engine's letter for the same event, where one exists.
        "scenario_code": SCENARIO_LETTER_FOR_ARCHETYPE.get(run.archetype),
        "name": arch.name,
        "signal": arch.signal,
        "body": arch.body,
        "diagnoses": [{"id": d, "label": DIAGNOSIS_LABELS[d]} for d in arch.diagnoses],
        "strategies": [
            {"id": s, "name": STRATEGY_BY_ID[s].name, "thesis": STRATEGY_BY_ID[s].thesis,
             "gain": STRATEGY_BY_ID[s].gain, "risk": STRATEGY_BY_ID[s].risk}
            for s in strategies
        ],
        # Severity only -- never `vuln`, which is the number they are diagnosing.
        "level": situation.level,
        # What each function is seeing. Symptoms only -- `vuln` never leaves the server.
        "evidence": [
            {"fn": e.fn, "line": e.line, "detail": e.detail, "tone": e.tone}
            for e in crisis_evidence(run.archetype, state, hist[-1] if hist else None,
                                     hist[-2] if len(hist) > 1 else None)
        ],
        "ignoring_is_legal": True,
    }


async def preview(session: AsyncSession, company: Company, payload: dict) -> dict:
    """Run the engine on a draft plan without persisting anything.

    This is what makes the in-quarter screens honest: the CEO sees where the plan is tight and
    where it has room, computed by the same engine that will grade them, without ever being
    shown the revenue before they commit.
    """
    run = await get_or_create_run(session, company)
    quarters = await locked_quarters(session, company.id)
    if len(quarters) >= TOTAL_QUARTERS:
        raise SimulationError("the run is complete; no further quarters can be previewed",
                        ("read_quarter_report", "read_endgame_preview"))

    state, history = replay(quarters)
    state = _apply_endgame_investment(state, run, history)
    allocations = _with_assigned_crisis(allocations_from_payload(payload), state, run)
    result = compute_simulation_quarter(state, allocations)

    return {
        "quarter": state.quarter,
        "opening_state": state_to_dict(state),
        "projection": result_to_dict(result),
        "budget": _plain(budget(state, result, allocations)),
        "crisis": crisis_briefing(state, run, history),
        "commit_reading": (
            commit_reading(allocations.crisis.strategy, allocations.crisis.commit, state)
            if state.quarter == CRISIS_QUARTER else None
        ),
        "legal_moves": list(legal_moves(state, run, len(quarters))),
    }


def _with_assigned_crisis(a: SimulationAllocations, state: SimulationCompanyState, run: SimulationRun) -> SimulationAllocations:
    """Force the archetype to the one this company was assigned.

    A client cannot choose its own crisis, and cannot avoid one by omitting the field -- the
    event fires in the crisis quarter whether or not a response was submitted.
    """
    from dataclasses import replace

    if state.quarter != CRISIS_QUARTER:
        return replace(a, crisis=CrisisResponse())
    return replace(a, crisis=replace(a.crisis, variant=run.archetype))


def _apply_endgame_investment(
    state: SimulationCompanyState, run: SimulationRun, history: list[SimulationQuarterResult]
) -> SimulationCompanyState:
    """Q4's opening state carries a signed "Path A" rescue cheque as `pending_investment`, so it
    raises the budget ceiling the moment it's accepted and shows as "pending" everywhere Q4 is
    read -- not just once the quarter locks. `compute_simulation_quarter` sweeps it into
    financing cash flow and clears it back to zero on `next_state`.

    A no-op unless Q4 is actually open with a signed Path A deal behind it -- everywhere else
    `state.pending_investment` stays the dataclass default of zero.
    """
    # Only apply to Q4 and only if enough history exists to build the term sheet
    if state.quarter != TOTAL_QUARTERS or len(history) < CRISIS_QUARTER:
        return state
    
    # If Path A is already officially selected, apply it
    if run.endgame_path == "A":
        ts = build_term_sheet(history[:CRISIS_QUARTER], history[CRISIS_QUARTER - 1].next_state)
        return replace(state, pending_investment=ts.offer("A").investment)
    
    # Path not selected yet, but return state unchanged (investment will be 0)
    return state


async def lock(session: AsyncSession, company: Company, payload: dict) -> dict:
    """Commit the quarter: run it, score it, persist it."""
    run = await get_or_create_run(session, company)
    quarters = await locked_quarters(session, company.id)
    if len(quarters) >= TOTAL_QUARTERS:
        raise SimulationError("the run is complete; a terminated run accepts no further locks",
                        ("read_quarter_report", "read_endgame_preview"))

    state, history = replay(quarters)
    state = _apply_endgame_investment(state, run, history)
    
    # Extract timer_remaining from payload for checkpoint creation
    timer_remaining = payload.get("timer_remaining")
    
    # Create checkpoint for THIS quarter's opening state
    # For Q1: timer_remaining should be 3000 (50 minutes) at simulation start
    # For Q2-Q4: timer_remaining is passed from frontend when previous quarter locked
    checkpoint = _create_checkpoint(timer_remaining, state, history[-1] if history else None, allocations_from_payload(payload))
    
    allocations = _with_assigned_crisis(allocations_from_payload(payload), state, run)
    result = compute_simulation_quarter(state, allocations)

    # Check for cash exhaustion (company failure)
    if result.next_state.cash <= 0:
        company.run_status = RunStatus.FAILED
        session.add(company)
    
    # Check for budget exhaustion: if closing with exactly zero budget left, mark as distressed
    # This prevents advancing to the next quarter - the run ends here with whatever was accomplished
    closing_budget = budget(state, result, allocations)
    left = closing_budget["ceiling"] - closing_budget["committed"]
    if left <= 0 and state.quarter < TOTAL_QUARTERS:
        # Zero budget left before Q4 means company ran out of money to allocate
        company.run_status = RunStatus.DISTRESSED
        session.add(company)

    prior = history[-1] if history else None
    constraint_id, all_ids = _constraint_ids(result)
    extra = ()

    # Q4 only: the term sheet settles against the quarter that actually happened.
    settlement = None
    if state.quarter == TOTAL_QUARTERS and run.endgame_path:
        ts = build_term_sheet(history[:3], history[2].next_state)
        settlement = settle(ts, run.endgame_path, result)
        extra = settlement.modifiers

    score = score_quarter(
        result, prior, allocations.reflection, allocations.priority,
        constraint_id, all_ids, closing_budget["ceiling"], extra,
    )

    # Save quarter with checkpoint in opening_state
    row = SimulationQuarter(
        company_id=company.id,
        number=state.quarter,
        decisions=allocations_to_dict(allocations),
        opening_state=state_to_dict(state, checkpoint),  # Include checkpoint
        result=result_to_dict(result),
        score=score_to_dict(score),
        ceo_score=str(score.final),
        band=score.band,
    )
    session.add(row)
    await session.flush()

    # Q4 only: freeze the settled term-sheet outcome so a reopened run shows the same report.
    if settlement is not None:
        run.settlement = _plain(settlement)
        company.run_status = RunStatus.COMPLETED
        session.add(run)
        session.add(company)
        await session.flush()

    return {
        "quarter": state.quarter,
        "result": result_to_dict(result),
        "score": score_to_dict(score),
        "settlement": _plain(settlement) if settlement else None,
        "next_state": state_to_dict(result.next_state),
        "legal_moves": list(legal_moves(result.next_state, run, len(quarters) + 1)),
    }


def _constraint_ids(r: SimulationQuarterResult) -> tuple[str | None, tuple[str, ...]]:
    """Which stage actually bound, and the other real pressures, for grading the CEO's reading."""
    found: list[str] = []
    if r.cash < BUFFER:
        found.append("cash")
    if r.leads_wasted > max(Decimal(60), r.eff_leads * Decimal("0.08")):
        found.append("sales")
    if r.supply_binding:
        found.append("production")
    if r.ceiling_binding:
        found.append("ceiling")
    if r.position_binding:
        found.append("position")
    if r.short_roles:
        found.append("staffing")
    if r.inv_units_out > max(Decimal(150), r.units_sold * Decimal("0.4")):
        found.append("wc")
    if r.supplier_rel < 76:
        found.append("supplier")
    if not found:
        found.append("demand")
    return found[0], tuple(found[:4])


async def endgame_preview(session: AsyncSession, company: Company) -> dict:
    """The Q4 menu. Only defined once three quarters have locked."""
    quarters = await locked_quarters(session, company.id)
    if len(quarters) < CRISIS_QUARTER:
        raise SimulationError(f"the term sheet is only readable once {CRISIS_QUARTER} quarters have locked",
                        ("preview_quarter", "lock_quarter"))
    _, history = replay(quarters)
    ts = build_term_sheet(history[:3], history[2].next_state)
    run = await get_or_create_run(session, company)
    return {
        "tier": ts.tier,
        "q3_valuation_inr": str(ts.v),
        "momentum": str(ts.momentum),
        "true_continuation_value_inr": str(ts.true_continuation),
        "term_sheet_menu": ts.menu(),
        "offers": [_plain(o) for o in ts.offers],
        "chosen_path": run.endgame_path,
        "chosen_term_sheet": run.endgame_term_sheet,
    }


async def submit_endgame(session: AsyncSession, company: Company, path: str,
                         term_sheet_name: str, reasoning: str | None) -> dict:
    quarters = await locked_quarters(session, company.id)
    if len(quarters) != CRISIS_QUARTER:
        raise SimulationError("the endgame decision is only legal between Q3 closing and Q4 locking")
    if path not in ("A", "B", "C"):
        raise SimulationError(f"path must be A, B or C (got {path!r})")

    _, history = replay(quarters)
    ts = build_term_sheet(history[:3], history[2].next_state)
    if term_sheet_name not in ts.menu().values():
        raise SimulationError(f"{term_sheet_name!r} is not on this tier's menu")

    run = await get_or_create_run(session, company)
    run.endgame_path = path
    run.endgame_term_sheet = term_sheet_name
    run.endgame_reasoning = reasoning
    
    # Path B (acquisition) and Path C (some independent options) end the simulation immediately.
    # Check if the chosen offer ends early by inspecting its kind and terms.
    chosen_offer = ts.offer(path)
    ends_early = False
    
    if chosen_offer:
        # Path B acquisitions always end early (kind='acquire')
        if chosen_offer.kind == "acquire":
            ends_early = True
        # Some Path C options also end early - check the terms for the signal
        elif path == "C":
            # Path C can be either "runs normally" or in rare edge cases might end early
            # For now, no Path C options end early in the current implementation
            pass
    
    if ends_early:
        company.run_status = RunStatus.COMPLETED
        session.add(company)
    
    await session.flush()
    return {"path": path, "term_sheet_name": term_sheet_name, "reasoning": reasoning, "tier": ts.tier, "ends_early": ends_early}


MAX_REWINDS = 2


async def rewind(session: AsyncSession, company: Company, target_quarter: int) -> dict:
    """Rewind the simulation to a previously completed quarter.

    Deletes all SimulationQuarter rows with number >= target_quarter, increments the
    rewind counter, and clears the endgame decision if rewinding past Q3. The replay
    architecture means no other state needs updating -- the next loadRun() will replay
    the remaining quarters and reconstruct the correct state.
    """
    if target_quarter < 1 or target_quarter >= TOTAL_QUARTERS:
        raise SimulationError(f"target quarter must be between 1 and {TOTAL_QUARTERS - 1} (got {target_quarter})")

    run = await get_or_create_run(session, company)
    if run.rewinds_used >= MAX_REWINDS:
        raise SimulationError("no rewind opportunities remaining")

    quarters = await locked_quarters(session, company.id)
    if not quarters:
        raise SimulationError("no quarters have been locked yet")

    target_numbers = [q.number for q in quarters if q.number >= target_quarter]
    if not target_numbers:
        raise SimulationError(f"quarter {target_quarter} has not been completed yet")

    # Retrieve checkpoint from target quarter's opening_state BEFORE deletion
    # This checkpoint will be used by the frontend to restore timer, cash, and budget ceiling
    checkpoint = None
    target_quarter_obj = next((q for q in quarters if q.number == target_quarter), None)
    if target_quarter_obj and target_quarter_obj.opening_state:
        checkpoint = target_quarter_obj.opening_state.get("checkpoint")

    # Delete all quarters at or after the target
    await session.execute(
        select(SimulationQuarter).where(
            SimulationQuarter.company_id == company.id,
            SimulationQuarter.number >= target_quarter,
        ).with_for_update()
    )
    for q in quarters:
        if q.number >= target_quarter:
            await session.delete(q)

    # Increment rewind counter
    run.rewinds_used += 1

    # If rewinding past Q3, clear the endgame decision so the player re-chooses
    if target_quarter <= CRISIS_QUARTER:
        run.endgame_path = None
        run.endgame_term_sheet = None
        run.endgame_reasoning = None
        # Also clear COMPLETED status if path C had ended the run
        if company.run_status == RunStatus.COMPLETED:
            company.run_status = RunStatus.ACTIVE
            session.add(company)

    await session.flush()

    return {
        "target_quarter": target_quarter,
        "deleted_quarters": sorted(target_numbers),
        "rewinds_used": run.rewinds_used,
        "rewinds_remaining": MAX_REWINDS - run.rewinds_used,
        "checkpoint": checkpoint,  # None for legacy runs without checkpoints
    }


async def run_state(session: AsyncSession, company: Company) -> dict:
    """Everything a client needs to render the run at any lifecycle point."""
    run = await get_or_create_run(session, company)
    quarters = await locked_quarters(session, company.id)
    state, history = replay(quarters)
    state = _apply_endgame_investment(state, run, history)
    complete = len(quarters) >= TOTAL_QUARTERS

    # Use the company's actual run_status, falling back to completed/active based on quarters
    if company.run_status == RunStatus.FAILED:
        run_status_str = "failed"
    elif company.run_status == RunStatus.DISTRESSED:
        run_status_str = "distressed"
    elif company.run_status == RunStatus.COMPLETED or complete:
        run_status_str = "completed"
    else:
        run_status_str = "active"

    # If the company failed mid-run and there's no settlement yet, create a synthetic one
    # with game_over=true so the frontend displays the failure state properly
    settlement = run.settlement
    if company.run_status == RunStatus.FAILED and settlement is None and history:
        # Create a synthetic settlement indicating game over
        settlement = {
            "path": "C",  # Failed while running independently
            "modifiers": [],
            "final_valuation": "0",
            "game_over": True,
            "ended_early": True,
        }

    return {
        "company_id": str(company.id),
        "total_quarters": TOTAL_QUARTERS,
        "crisis_quarter": CRISIS_QUARTER,
        "current_quarter": None if complete else state.quarter,
        "quarters_locked": len(quarters),
        "run_status": run_status_str,
        "state": state_to_dict(state),
        "legal_moves": list(legal_moves(state, run, len(quarters))),
        "crisis": crisis_briefing(state, run, history),
        "score_trajectory": [
            {"quarter_number": row.number, "ceo_score": row.ceo_score, "band": row.band} for row in quarters
        ],
        "history": [row.result for row in quarters],
        "scores": [row.score for row in quarters],
        "endgame_path": run.endgame_path,
        "rewinds_used": run.rewinds_used,
        "settlement": settlement,
    }
