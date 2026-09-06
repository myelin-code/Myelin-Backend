"""One POST route per department, each upserting that department's columns onto the quarter's
single QuarterAllocation row. Six calls build up the 22-line spend model, plus a 7th for crisis
response (Phase 10, only meaningful in the quarter a crisis fires) -- not the legacy per-decision
submission flow `routes/_factory.py` serves.

Allocations are pure inputs to `compute_quarter()`; nothing here computes or writes a `*State`
row. Only `run_quarter()` does that, and only once the quarter is locked (see the design-rule
comment in `routes/_factory.py`).
"""

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from decimal import Decimal

from app.core.db import get_db
from app.engines.run_state import Move
from app.models.quarter import Quarter
from app.models.quarter_allocation import QuarterAllocation
from app.models.company import Company
from app.models.company_state_snapshot import CompanyStateSnapshot
from app.routes.deps import require_quarter_move
from app.config.loader import load_seed
from app.schemas.allocation import (
    CrisisAllocationSubmit,
    FinanceAdminAllocationSubmit,
    HrAllocationSubmit,
    MarketingAllocationSubmit,
    OperationsAllocationSubmit,
    QuarterAllocationResponse,
    RndAllocationSubmit,
    SalesAllocationSubmit,
)
from app.schemas.errors import WRITE_RESPONSES

router = APIRouter(prefix="/companies/{company_id}/quarters/{quarter_id}/allocations", tags=["allocations"])


async def _upsert(
    company_id: uuid.UUID, quarter: Quarter, fields: dict[str, Any], session: AsyncSession
) -> QuarterAllocation:
    row = (
        await session.execute(select(QuarterAllocation).where(QuarterAllocation.quarter_id == quarter.id))
    ).scalar_one_or_none()
    if row is None:
        row = QuarterAllocation(company_id=company_id, quarter_id=quarter.id)
        session.add(row)
    
    # Calculate total allocation after applying the new fields
    temp_row = QuarterAllocation(company_id=company_id, quarter_id=quarter.id)
    # Copy existing values from the row if it exists
    if row.id is not None:
        for column in row.__table__.columns:
            if column.key not in ('id', 'company_id', 'quarter_id', 'created_at'):
                setattr(temp_row, column.key, getattr(row, column.key))
    
    # Apply new field values to temp row
    for field, value in fields.items():
        setattr(temp_row, field, value)
    
    # Calculate total discretionary spend
    total_spend = sum([
        temp_row.google_ads or Decimal(0),
        temp_row.meta_ads or Decimal(0),
        temp_row.social_influencer or Decimal(0),
        temp_row.content_seo or Decimal(0),
        temp_row.events_pr or Decimal(0),
        temp_row.email_marketing or Decimal(0),
        temp_row.referral or Decimal(0),
        temp_row.prelaunch_buzz or Decimal(0),
        temp_row.reps or Decimal(0),
        temp_row.crm_tools or Decimal(0),
        temp_row.onboarding or Decimal(0),
        temp_row.quality_qa or Decimal(0),
        temp_row.innovation or Decimal(0),
        temp_row.manufacturing or Decimal(0),
        temp_row.supplier_qc or Decimal(0),
        temp_row.logistics or Decimal(0),
        temp_row.culture_benefits or Decimal(0),
        temp_row.training_development or Decimal(0),
        temp_row.cx_team or Decimal(0),
        temp_row.compliance_legal or Decimal(0),
        temp_row.financial_planning or Decimal(0),
        temp_row.audit_prep or Decimal(0),
        temp_row.price_match_fund or Decimal(0),
        temp_row.comparison_ads or Decimal(0),
        temp_row.retention_offers or Decimal(0),
        temp_row.emergency_supply_fund or Decimal(0),
        temp_row.crisis_choice_d_spend or Decimal(0),
    ])
    
    # Get available cash and fixed costs
    available_cash = quarter.cash_balance
    company = await session.get(Company, company_id)
    seed = load_seed(company.seed_name)
    
    # Get fixed costs from the opening state
    if quarter.number == 1:
        fixed_costs = seed.fixed_costs_inr
    else:
        # Get the prior quarter's closing state
        prior_quarters = (
            await session.execute(
                select(Quarter).where(
                    Quarter.company_id == company_id,
                    Quarter.number == quarter.number - 1
                )
            )
        ).scalars().all()
        
        if prior_quarters:
            prior_quarter = prior_quarters[0]
            snapshot = (
                await session.execute(
                    select(CompanyStateSnapshot).where(
                        CompanyStateSnapshot.quarter_id == prior_quarter.id
                    )
                )
            ).scalar_one_or_none()
            
            if snapshot and snapshot.state:
                fixed_costs = Decimal(str(snapshot.state.get('fixed_costs_inr', seed.fixed_costs_inr)))
            else:
                fixed_costs = seed.fixed_costs_inr
        else:
            fixed_costs = seed.fixed_costs_inr
    
    buffer = seed.working_capital_buffer_inr
    RUPEES_PER_LAKH = Decimal(100000)
    
    # Calculate total discretionary in rupees
    total_discretionary_inr = total_spend * RUPEES_PER_LAKH
    
    # Calculate projected cash after allocations and fixed costs
    projected_cash_after_allocations = available_cash - total_discretionary_inr - fixed_costs
    
    # Hard validation: cash must NEVER go negative (even ignoring buffer)
    if projected_cash_after_allocations < 0:
        raise HTTPException(
            status_code=422,
            detail=f"Insufficient cash. Total allocation (₹{total_discretionary_inr:,.2f}) plus "
                   f"fixed costs (₹{fixed_costs:,.2f}) exceeds available cash (₹{available_cash:,.2f}). "
                   f"You cannot allocate more than your available cash. "
                   f"Reduce allocations by at least ₹{abs(projected_cash_after_allocations):,.2f}."
        )
    
    # Calculate discretionary ceiling (including buffer)
    discretionary_ceiling_lakhs = (available_cash - fixed_costs - buffer) / RUPEES_PER_LAKH
    
    if total_spend > discretionary_ceiling_lakhs:
        raise HTTPException(
            status_code=422,
            detail=f"Total allocation (₹{total_spend:.2f} lakhs) exceeds available budget "
                   f"(₹{discretionary_ceiling_lakhs:.2f} lakhs). "
                   f"Available cash: ₹{available_cash:,.2f}, Fixed costs: ₹{fixed_costs:,.2f}, "
                   f"Working capital buffer: ₹{buffer:,.2f}"
        )
    
    # If validation passes, apply the actual updates
    for field, value in fields.items():
        setattr(row, field, value)
    await session.commit()
    await session.refresh(row)
    return row


def _add_department_route(department: str, schema: type[BaseModel], move: Move) -> None:
    """A closure per call, not per loop iteration -- `department`/`schema`/`move` are bound as
    this function's own arguments, so each of the 7 calls below gets its own value, not the
    loop's last one (the classic late-binding bug this sidesteps by not looping over a shared
    scope). `move` (Phase 12) is `SUBMIT_ALLOCATION` for the 6 real departments and
    `SUBMIT_CRISIS_ALLOCATION` for crisis -- the single gatekeeper refuses crisis submissions
    outside the scenario's crisis quarter, not just outside an open quarter."""

    is_crisis = department == "crisis"

    @router.post(
        f"/{department}",
        response_model=QuarterAllocationResponse,
        name=f"submit_{department}_allocation",
        responses=WRITE_RESPONSES,
        summary=f"Submit the {department} allocation",
        description=(
            "Upserts the crisis-response fields (price_match_fund, comparison_ads, "
            "retention_offers, emergency_supply_fund, crisis_choice, crisis_choice_d_spend) onto "
            "this quarter's single allocation row. Legal only in the scenario's crisis quarter "
            "(`RunStateResponse.crisis_quarter`) -- submitting it in any other quarter returns "
            "`illegal_move` (409). Evaluated against the opening snapshot only; never mutates "
            "`*State` -- only `POST .../lock` does that."
            if is_crisis
            else f"Upserts the {department} department's spend lines onto this quarter's single "
            "allocation row -- pure input, evaluated only when the quarter locks. Legal only "
            "while this quarter is open (`RunStateResponse.legal_moves` includes "
            "`submit_allocation`); calling it on a locked quarter or before one is open returns "
            "`illegal_move` (409)."
        ),
    )
    async def submit_allocation(
        company_id: uuid.UUID,
        submission: schema,  # type: ignore[valid-type]
        quarter: Quarter = Depends(require_quarter_move(move)),
        session: AsyncSession = Depends(get_db),
    ) -> QuarterAllocation:
        return await _upsert(company_id, quarter, submission.model_dump(), session)


_add_department_route("marketing", MarketingAllocationSubmit, Move.SUBMIT_ALLOCATION)
_add_department_route("sales", SalesAllocationSubmit, Move.SUBMIT_ALLOCATION)
_add_department_route("rnd", RndAllocationSubmit, Move.SUBMIT_ALLOCATION)
_add_department_route("operations", OperationsAllocationSubmit, Move.SUBMIT_ALLOCATION)
_add_department_route("hr", HrAllocationSubmit, Move.SUBMIT_ALLOCATION)
_add_department_route("finance_admin", FinanceAdminAllocationSubmit, Move.SUBMIT_ALLOCATION)
# Not one of the 6 CLAUDE.md departments -- crisis response (Phase 10) is its own category, only
# meaningful in the quarter a crisis fires. Reuses the same upsert-onto-one-row factory unchanged.
_add_department_route("crisis", CrisisAllocationSubmit, Move.SUBMIT_CRISIS_ALLOCATION)
