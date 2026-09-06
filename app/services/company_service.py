"""Company and quarter creation: turning a scenario into rows a student can act on.

Nothing here computes a simulation number. Opening values come from the scenario config
(`config/scenarios/`) and the company seed (`config/seeds/`); this module only decides *which*
scenario, and writes what those two configs already state.
"""

import hashlib
import random
import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import Float, Integer, func, select
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.loader import available_scenario_ids, load_scenario, load_seed
from app.config.schema import Scenario
from app.core.db import Base
from app.engines.survival import is_terminal
from app.models.company import Company
from app.models.company_state_snapshot import CompanyStateSnapshot
from app.models.cx import CXState
from app.models.finance import FinanceState
from app.models.marketing import MarketingState
from app.models.modifier import Modifier
from app.models.operations import OperationsState
from app.models.product import ProductState
from app.models.quarter import Quarter, QuarterStatus
from app.models.sales import SalesState

# Each workspace's `*State` model and the scenario-config block holding its opening values.
STATE_MODELS: dict[str, type[Base]] = {
    "finance": FinanceState,
    "marketing": MarketingState,
    "product": ProductState,
    "sales": SalesState,
    "operations": OperationsState,
    "cx": CXState,
}


class ScenarioAssignmentError(ValueError):
    """No scenario could be assigned -- raised rather than falling back to an arbitrary one."""


def assign_scenario_id(company_id: uuid.UUID) -> str:
    """Pick a scenario deterministically from the company's own identifier.

    Seeded from a SHA-256 of the identifier rather than `random.choice()` on the global RNG:
    the platform is deterministic (`docs/01-technical-architecture.md`), so replaying a run with
    the same company id must land on the same scenario, in this process and any other. Python's
    `hash()` would not do -- it is salted per process.

    `available_scenario_ids()` is sorted, so the index this maps to is stable across machines.
    """
    scenario_ids = available_scenario_ids()
    if not scenario_ids:
        raise ScenarioAssignmentError("no scenarios are configured in config/scenarios/")

    digest = hashlib.sha256(str(company_id).encode("utf-8")).digest()
    return random.Random(int.from_bytes(digest, "big")).choice(scenario_ids)


_CRISIS_SCENARIOS = ("A", "B", "C", "D")


def assign_crisis_scenario(company_id: uuid.UUID) -> str:
    """Pick which of the four crisis events (docs/11-crisis-system.md) fires for this company,
    deterministically from its own identifier -- the 1-in-4 random split docs/11 §2 calls for,
    made reproducible the same way `assign_scenario_id` is: seeded from a SHA-256 of the company
    id into `random.Random`, never `random.choice()` on the global RNG or Python's salted `hash()`.

    A different digest than `assign_scenario_id`'s (mixed with a fixed salt) so a company that
    happens to get scenario index N and crisis letter index N isn't a coincidence of reusing the
    same random stream for two different assignments.
    """
    digest = hashlib.sha256(f"crisis:{company_id}".encode("utf-8")).digest()
    return random.Random(int.from_bytes(digest, "big")).choice(_CRISIS_SCENARIOS)


def _coerce(model: type[Base], field: str, value: Any) -> Any:
    """Match the column's Python type.

    Config carries every number as `Decimal` (`loader.py` parses with `parse_float=Decimal` so
    coefficients stay exact). The legacy `*State` columns are a mix of Numeric, Integer and
    Float, and asyncpg rejects a Decimal bound to an integer or double column.
    """
    if value is None:
        return None
    column = sa_inspect(model).columns[field]
    if isinstance(column.type, Integer):
        return int(value)
    if isinstance(column.type, Float):
        return float(value)
    return Decimal(value)


def _opening_state_rows(scenario: Scenario, quarter_id: uuid.UUID) -> list[Base]:
    """One `*State` row per workspace, built from scenario config.

    `finance.cash_balance` is the one value overlaid from the seed instead of the scenario:
    it is a company number, and duplicating it into scenario config would create a second place
    to change it (and a way for the two to disagree).
    """
    opening = scenario.opening_workspace_state
    seed = load_seed(scenario.seed)

    rows: list[Base] = []
    for workspace, model in STATE_MODELS.items():
        values = dict(getattr(opening, workspace))
        if workspace == "finance":
            values["cash_balance"] = seed.opening_cash_inr
        rows.append(model(quarter_id=quarter_id, **{f: _coerce(model, f, v) for f, v in values.items()}))
    return rows


async def _next_seq(session: AsyncSession, owner_id: uuid.UUID | None) -> int:
    """This owner's next run number: 1 for their first run, then one past their highest.

    Reads the maximum rather than counting rows, so the number never repeats even if a run is
    later removed -- a `/run/<n>` link that is already in someone's history must not start
    resolving to a different run. The unique index on (owner_id, seq) is what makes two
    simultaneous `POST /companies` from one caller fail loudly instead of both landing on the
    same number; there is no correct silent answer to that race.
    """
    highest = (
        await session.execute(
            select(func.max(Company.seq)).where(Company.owner_id == owner_id)
        )
    ).scalar_one()
    return (highest or 0) + 1


async def create_company(
    session: AsyncSession,
    *,
    name: str,
    scenario_id: str | None = None,
    company_id: uuid.UUID | None = None,
    owner_id: uuid.UUID | None = None,
) -> Company:
    """Materialise a company and everything its first quarter needs.

    `company_id` is accepted so a run can be replayed onto the same identifier and land the same
    scenario assignment; omitted, it is generated. `owner_id` is the authenticated caller
    (Phase 13) -- optional here only so non-HTTP callers (tests, scripts) aren't forced to
    thread a user through; `routes/company.py::create_company_route` always passes it.
    """
    company_id = company_id or uuid.uuid4()
    scenario = load_scenario(scenario_id or assign_scenario_id(company_id))

    company = Company(
        id=company_id,
        name=name,
        scenario_id=scenario.scenario_id,
        seed_name=scenario.seed,
        profile_name=scenario.profile,
        owner_id=owner_id,
        seq=await _next_seq(session, owner_id),
    )
    session.add(company)
    await session.flush()
    return company


async def _opening_cash(session: AsyncSession, company: Company, number: int) -> Decimal:
    """Q1 opens on the seed's cash; every later quarter opens on the prior quarter's closing cash.

    Read from the prior `CompanyStateSnapshot` -- the same row `run_quarter()` carries forward --
    so the quarter's own denormalised `cash_balance` cannot drift from what the engine will
    actually run against.
    """
    if number == 1:
        return load_seed(company.seed_name).opening_cash_inr

    snapshot = (
        await session.execute(
            select(CompanyStateSnapshot)
            .join(Quarter, Quarter.id == CompanyStateSnapshot.quarter_id)
            .where(Quarter.company_id == company.id, Quarter.number == number - 1)
        )
    ).scalar_one_or_none()
    if snapshot is None:
        raise ValueError(
            f"quarter {number - 1} has not been locked yet -- lock it before opening quarter {number}, "
            "so this quarter starts from real closing state rather than a guess"
        )
    return Decimal(snapshot.state["cash_inr"])


async def create_quarter(session: AsyncSession, company: Company) -> Quarter:
    """Create the company's next quarter, carrying forward the prior quarter's closing state.

    The engine-facing carry-forward is implicit: `run_quarter()` reads the prior quarter's
    `CompanyStateSnapshot` directly, so the full ~20-field `CompanyState` is never copied here.
    Only the denormalised `cash_balance` is materialised onto the Quarter row.
    """
    scenario = load_scenario(company.scenario_id)

    last_number = (
        await session.execute(select(func.max(Quarter.number)).where(Quarter.company_id == company.id))
    ).scalar_one()
    number = (last_number or 0) + 1

    if number > scenario.total_quarters:
        raise ValueError(
            f"scenario '{scenario.scenario_id}' runs {scenario.total_quarters} quarters; "
            f"quarter {number} is past the end of the simulation"
        )

    # A terminated run cannot open another quarter. FAILED means the company ran out of cash;
    # COMPLETED means it played its last quarter. DISTRESSED is deliberately not here -- it is a
    # warning tier that changes the Q4 term-sheet menu, and the company keeps playing.
    if is_terminal(company.run_status):
        raise ValueError(
            f"company {company.id} is {company.run_status.value} and cannot open another quarter"
            + (f" ({company.survival_detail})" if company.survival_detail else "")
        )

    # Validate that the prior quarter has sufficient cash before creating the next quarter
    opening_cash = await _opening_cash(session, company, number)
    if opening_cash <= 0:
        raise ValueError(
            f"Cannot open quarter {number}: insufficient cash balance (₹{opening_cash:,.2f}). "
            f"The previous quarter ended with zero or negative cash, preventing further progression."
        )

    quarter = Quarter(
        company_id=company.id,
        number=number,
        status=QuarterStatus.IN_PROGRESS,
        cash_balance=opening_cash,
        revenue=Decimal(0),
    )
    session.add(quarter)
    await session.flush()

    session.add_all(_opening_state_rows(scenario, quarter.id))
    session.add_all(
        Modifier(company_id=company.id, quarter_id=quarter.id, modifier_key=key, value=float(modifier.value))
        for key, modifier in scenario.modifiers.items()
    )
    await session.flush()
    return quarter
