from __future__ import annotations

from fastapi import APIRouter, Query

from ..store import progress as progress_store

router = APIRouter()


@router.get("/api/progress")
def get_progress(days: int = Query(14, ge=7, le=120)) -> dict:
    return progress_store.progress(days)
