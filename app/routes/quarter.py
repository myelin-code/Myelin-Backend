import uuid

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.models.company import Company
from app.config.loader import load_profile, load_scenario, load_seed
from app.engines.quarter import compute_quarter
from app.engines.state import CrisisEvent
from app.models.quarter import Quarter, QuarterStatus
from app.models.quarter_allocation import QuarterAllocation
from app.models.quarter_performance import QuarterPerformance
from app.routes.deps import get_current_user, get_quarter, get_quarter_for_write
from app.schemas.crisis import CrisisBriefingResponse
from app.schemas.errors import READ_RESPONSES, READ_RESPONSES_WITH_PLAIN_CONFLICT
from app.schemas.quarter import (
    LeaderboardEntry,
    LeaderboardResponse,
    QuarterReportPdfResponse,
    QuarterReportResponse,
)
from app.services.auth_service import CurrentUser
from app.services.authorization_service import require_read_access
from app.services.company_service import assign_crisis_scenario
from app.services.crisis_briefing_service import NotCrisisQuarterError, build_crisis_briefing
from app.services.quarter_run_service import run_quarter, load_opening_state, to_allocations
from app.services.report_service import QuarterNotLockedError, build_report_for_quarter
from app.services.storage_service import (
    REPORT_BUCKET,
    SupabaseStorageClient,
    SupabaseStorageError,
    get_supabase_storage_client,
)

router = APIRouter(prefix="/companies/{company_id}", tags=["quarter"])


@router.post(
    "/quarters/{quarter_id}/lock",
    response_model=QuarterReportResponse,
    responses=READ_RESPONSES,
    summary="Lock the quarter and run the engine",
    description="Runs the pure 22-line engine over this quarter's submitted allocations, "
    "persists the result, and returns the full report. Idempotent: calling this twice on an "
    "already-locked quarter returns the same persisted result unchanged, not a 409 -- there is "
    "no illegal_move refusal for this route.",
)
async def lock_quarter(
    company_id: uuid.UUID,
    quarter: Quarter = Depends(get_quarter_for_write),
    session: AsyncSession = Depends(get_db),
) -> QuarterReportResponse:
    """Runs the pure 22-line engine (`compute_quarter`, via `run_quarter`) over this quarter's
    submitted allocations, persists the result, and returns the full Phase 9 report for it.

    Idempotent: `run_quarter` itself is the lock guard -- an already-locked quarter returns its
    persisted result unchanged rather than recomputing or 409ing, so calling this twice is safe.
    """
    # Pre-flight validation: prevent locking if closing cash would be negative
    if quarter.status != QuarterStatus.CLOSED:
        company = await session.get(Company, quarter.company_id)
        seed = load_seed(company.seed_name)
        profile = load_profile(company.profile_name)
        scenario = load_scenario(company.scenario_id)
        
        allocation_row = (
            await session.execute(select(QuarterAllocation).where(QuarterAllocation.quarter_id == quarter.id))
        ).scalar_one_or_none()
        allocations = to_allocations(allocation_row)
        opening_state = await load_opening_state(session, quarter, seed)
        
        # Check if this is a crisis quarter
        crisis_event = None
        if quarter.number == scenario.crisis_quarter:
            letter = scenario.crisis_scenario or assign_crisis_scenario(company.id)
            crisis_event = CrisisEvent(scenario=letter)
        
        # Compute the quarter result to check closing cash
        result = compute_quarter(opening_state, allocations, profile, seed, crisis_event)
        
        if result.closing_state.cash_inr <= 0:
            raise HTTPException(
                status_code=422,
                detail=f"Cannot lock quarter: closing cash would be ₹{result.closing_state.cash_inr:,.2f}. "
                       f"You need to reduce your allocations to maintain positive cash balance. "
                       f"Opening cash: ₹{opening_state.cash_inr:,.2f}"
            )
    
    await run_quarter(session, quarter.id)
    report = await build_report_for_quarter(session, quarter.id)
    return QuarterReportResponse.model_validate(report)


@router.get(
    "/quarters/{quarter_id}/report",
    response_model=QuarterReportResponse,
    responses=READ_RESPONSES_WITH_PLAIN_CONFLICT,
    summary="Read a locked quarter's report",
    description="Reads back everything the lock transaction already persisted -- never "
    "recomputes, never mutates. 409s (plain-detail, not the illegal_move envelope) for a quarter "
    "that hasn't been locked yet.",
)
async def get_quarter_report(
    company_id: uuid.UUID,
    quarter: Quarter = Depends(get_quarter),
    session: AsyncSession = Depends(get_db),
) -> QuarterReportResponse:
    """Reads back everything the lock transaction already persisted -- never recomputes, never
    mutates. 409s (matching `routes/deps.py`'s convention for "wrong quarter-lock state") for a
    quarter that hasn't been locked yet: there is no report before the quarter is run.
    """
    try:
        report = await build_report_for_quarter(session, quarter.id)
    except QuarterNotLockedError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return QuarterReportResponse.model_validate(report)


_PDF_SIGNED_URL_TTL = 3600


def _report_pdf_path(company_id: uuid.UUID, quarter_id: uuid.UUID) -> str:
    return f"{company_id}/{quarter_id}.pdf"


@router.post(
    "/quarters/{quarter_id}/report/pdf",
    response_model=QuarterReportPdfResponse,
    responses=READ_RESPONSES_WITH_PLAIN_CONFLICT,
    summary="Store a client-rendered report PDF",
    description="The frontend renders the PDF itself from the same report data `GET .../report` "
    "already serves, then hands the finished bytes here to be stored in Supabase Storage's "
    "private `quarter-reports` bucket (service-role write, never public). 409s if the quarter "
    "isn't locked yet -- there is nothing to have rendered a report from.",
)
async def store_quarter_report_pdf(
    company_id: uuid.UUID,
    file: UploadFile = File(...),
    quarter: Quarter = Depends(get_quarter),
    session: AsyncSession = Depends(get_db),
    storage: SupabaseStorageClient = Depends(get_supabase_storage_client),
) -> QuarterReportPdfResponse:
    try:
        await build_report_for_quarter(session, quarter.id)
    except QuarterNotLockedError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc

    data = await file.read()
    path = _report_pdf_path(company_id, quarter.id)
    try:
        await storage.upload(REPORT_BUCKET, path, data, content_type="application/pdf")
        signed_url = await storage.create_signed_url(REPORT_BUCKET, path, expires_in=_PDF_SIGNED_URL_TTL)
    except SupabaseStorageError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Supabase Storage rejected the upload: {exc.message}") from exc

    return QuarterReportPdfResponse(
        bucket=REPORT_BUCKET, path=path, signed_url=signed_url, expires_in=_PDF_SIGNED_URL_TTL
    )


@router.get(
    "/quarters/{quarter_id}/report/pdf",
    response_model=QuarterReportPdfResponse,
    responses=READ_RESPONSES_WITH_PLAIN_CONFLICT,
    summary="Re-sign a previously stored report PDF",
    description="404s if this quarter's PDF has never been generated (`POST` this same path "
    "first). Signed URLs expire, so this always mints a fresh one rather than caching -- don't "
    "persist the URL itself, only that a PDF exists.",
)
async def get_quarter_report_pdf(
    company_id: uuid.UUID,
    quarter: Quarter = Depends(get_quarter),
    storage: SupabaseStorageClient = Depends(get_supabase_storage_client),
) -> QuarterReportPdfResponse:
    path = _report_pdf_path(company_id, quarter.id)
    try:
        signed_url = await storage.create_signed_url(REPORT_BUCKET, path, expires_in=_PDF_SIGNED_URL_TTL)
    except SupabaseStorageError as exc:
        if exc.status_code == 404:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No report PDF has been generated for this quarter yet") from exc
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Supabase Storage rejected the request: {exc.message}") from exc

    return QuarterReportPdfResponse(
        bucket=REPORT_BUCKET, path=path, signed_url=signed_url, expires_in=_PDF_SIGNED_URL_TTL
    )


@router.get(
    "/quarters/{quarter_id}/crisis",
    response_model=CrisisBriefingResponse,
    responses=READ_RESPONSES,
    summary="Read this quarter's crisis briefing",
    description="The narrative, the Strategic Choices, and the response spend lines that "
    "actually feed this scenario's recovery formulas -- the half of a crisis "
    "`docs/11-crisis-system.md` says students are told. Never returns a coefficient, threshold "
    "or penalty magnitude: diagnosing those from their own results is the exercise. Readable "
    "before and after the response is submitted; 404s for any quarter that isn't the "
    "scenario's crisis quarter.",
)
async def get_crisis_briefing(
    company_id: uuid.UUID,
    quarter: Quarter = Depends(get_quarter),
    session: AsyncSession = Depends(get_db),
) -> CrisisBriefingResponse:
    """Read-through of config copy plus `engines/crisis`'s own response-line mapping -- computes
    nothing and reads no allocation, so it returns the same briefing whether or not the student
    has already responded."""
    company = await session.get(Company, quarter.company_id)
    try:
        briefing = build_crisis_briefing(company, quarter)
    except NotCrisisQuarterError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return CrisisBriefingResponse.model_validate(briefing)


@router.get(
    "/leaderboard",
    response_model=LeaderboardResponse,
    responses=READ_RESPONSES,
    summary="Read this company's per-quarter score rollups",
    description="Reads persisted `QuarterPerformance` rollups -- never live-aggregates. Entries "
    "exist only for quarters that have locked.",
)
async def get_leaderboard(
    company_id: uuid.UUID,
    session: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> LeaderboardResponse:
    """Reads QuarterPerformance rollups -- never live-aggregates cognitive_scores.

    Phase 13: previously had no company lookup at all -- a read side-channel around ownership.
    Now 404s if the company doesn't exist and gates on read access (owner-or-instructor)
    before running the query.
    """
    company = await session.get(Company, company_id)
    if company is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Company {company_id} not found")
    require_read_access(company, user)

    rows = (
        await session.execute(
            select(QuarterPerformance, Quarter.number)
            .join(Quarter, Quarter.id == QuarterPerformance.quarter_id)
            .where(QuarterPerformance.company_id == company_id)
            .order_by(Quarter.number)
        )
    ).all()
    entries = [
        LeaderboardEntry(
            company_id=company_id,
            quarter_id=performance.quarter_id,
            quarter_number=number,
            ceo_score=performance.ceo_score,
            band=performance.score_band,
        )
        for performance, number in rows
    ]
    return LeaderboardResponse(entries=entries)
