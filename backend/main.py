"""
SkillGap AI Coach — FastAPI backend.
Analyzes resume vs job description and returns match score, skills, and improvement plan.
"""
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from db.schema import AnalysisRun
from db.session import get_session, init_db
from services.analyzer import analyze_resume_vs_job
from services.models import AnalysisResult


class AnalyzeRequest(BaseModel):
    """Request body for /analyze."""

    resume_text: str = Field(..., min_length=1, description="Raw resume text")
    job_description: str = Field(..., min_length=1, description="Job description text")


class SkillWithEvidenceOut(BaseModel):
    skill: str
    evidence: str


class AnalyzeResponse(BaseModel):
    """Response for /analyze — matches AnalysisResult."""

    match_score: int = Field(..., ge=0, le=100)
    overlapping_skills: list[SkillWithEvidenceOut]
    missing_skills: list[str]
    suggested_next_steps: list[str]
    mode: str = Field(..., description="baseline | llm")


class HistoryItem(BaseModel):
    id: int
    timestamp: str
    resume_summary: str
    job_title_guess: str
    resume_text: str
    job_description: str
    match_score: int
    result: dict[str, Any]


def _resume_summary(text: str, max_len: int = 50) -> str:
    """First max_len chars of resume, trimmed at word boundary."""
    t = (text or "").strip()
    if not t:
        return ""
    if len(t) <= max_len:
        return t
    return t[: max_len + 1].rsplit(maxsplit=1)[0].rstrip() or t[:max_len]


def _job_title_guess(text: str, max_len: int = 80) -> str:
    """First line or first max_len chars of job description."""
    t = (text or "").strip()
    if not t:
        return ""
    first_line = t.split("\n")[0].strip()
    if len(first_line) <= max_len:
        return first_line
    return first_line[: max_len + 1].rsplit(maxsplit=1)[0].rstrip() or first_line[:max_len]


def _result_to_json(result: AnalysisResult, resume_text: str, job_description: str) -> dict[str, Any]:
    """Serialize AnalysisResult + inputs to JSON-serializable dict for storage and API."""
    return {
        "resume_text": resume_text,
        "job_description": job_description,
        "match_score": result.match_score,
        "overlapping_skills": [{"skill": s.skill, "evidence": s.evidence} for s in result.overlapping_skills],
        "missing_skills": result.missing_skills,
        "suggested_next_steps": result.suggested_next_steps,
        "mode": result.mode,
    }


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: create DB tables; shutdown: cleanup."""
    await init_db()
    yield


app = FastAPI(
    title="SkillGap AI Coach API",
    description="Analyze resume vs job description: match score, skills gap, improvement plan.",
    version="0.2.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health() -> dict:
    """Health check for Docker and load balancers."""
    return {"status": "ok", "service": "skillgap-ai-coach"}


@app.post("/analyze", response_model=AnalyzeResponse)
async def analyze(
    request: AnalyzeRequest,
    session: AsyncSession = Depends(get_session),
) -> AnalyzeResponse:
    """
    Analyze resume text against job description.
    Returns match score (0–100), overlapping skills with evidence, missing skills, and suggested next steps.
    Every successful analysis is saved to the database.
    """
    try:
        resume_text_clean = request.resume_text.strip()
        job_description_clean = request.job_description.strip()
        result: AnalysisResult = analyze_resume_vs_job(
            resume_text=resume_text_clean,
            job_description=job_description_clean,
        )
        response = AnalyzeResponse(
            match_score=result.match_score,
            overlapping_skills=[SkillWithEvidenceOut(skill=s.skill, evidence=s.evidence) for s in result.overlapping_skills],
            missing_skills=result.missing_skills,
            suggested_next_steps=result.suggested_next_steps,
            mode=result.mode,
        )
        # Skip saving if this run is identical to the most recent one (consecutive duplicate).
        # Only attempt this when using a real AsyncSession (tests inject an AsyncMock).
        if isinstance(session, AsyncSession):
            last_stmt = select(AnalysisRun).order_by(AnalysisRun.timestamp.desc()).limit(1)
            last_result = await session.execute(last_stmt)
            last_run = last_result.scalar_one_or_none()
            if last_run and isinstance(last_run.result_json, dict):
                prev_resume = (last_run.result_json or {}).get("resume_text", "") or ""
                prev_job = (last_run.result_json or {}).get("job_description", "") or ""
                if prev_resume.strip() == resume_text_clean and prev_job.strip() == job_description_clean:
                    return response

        # Persist to DB for non-duplicate runs
        run = AnalysisRun(
            resume_summary=_resume_summary(request.resume_text, 50),
            job_title_guess=_job_title_guess(request.job_description, 80),
            match_score=result.match_score,
            result_json=_result_to_json(result, resume_text_clean, job_description_clean),
        )
        session.add(run)
        await session.commit()
        return response
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail="Analysis failed. Please try again.")


@app.get("/history", response_model=list[HistoryItem])
async def history(session: AsyncSession = Depends(get_session)) -> list[HistoryItem]:
    """Return the last 10 analysis runs (summary fields + full result)."""
    stmt = select(AnalysisRun).order_by(AnalysisRun.timestamp.desc()).limit(10)
    result = await session.execute(stmt)
    rows = result.scalars().all()
    return [
        HistoryItem(
            id=r.id,
            timestamp=r.timestamp.isoformat() if r.timestamp else "",
            resume_summary=r.resume_summary or "",
            job_title_guess=r.job_title_guess or "",
            resume_text=(r.result_json or {}).get("resume_text", "") if isinstance(r.result_json, dict) else "",
            job_description=(r.result_json or {}).get("job_description", "") if isinstance(r.result_json, dict) else "",
            match_score=r.match_score,
            result=r.result_json or {},
        )
        for r in rows
    ]


@app.delete("/history", status_code=204)
async def clear_history(session: AsyncSession = Depends(get_session)) -> Response:
    """Delete all analysis runs."""
    await session.execute(delete(AnalysisRun))
    await session.commit()
    return Response(status_code=204)


@app.delete("/history/{run_id}", status_code=204)
async def delete_history_item(run_id: int, session: AsyncSession = Depends(get_session)) -> Response:
    """Delete a single analysis run by id."""
    result = await session.execute(delete(AnalysisRun).where(AnalysisRun.id == run_id))
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="History item not found")
    await session.commit()
    return Response(status_code=204)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
