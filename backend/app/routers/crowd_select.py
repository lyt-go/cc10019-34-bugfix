"""人群圈选路由：执行圈选并获取结果。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from app.schemas import ActionPayload, ActionResult
from app.services.crowd_select import (
    get_crowd_result,
    rebuild_crowd,
    select_crowd,
)
from app.store import store, query_filters

router = APIRouter(prefix="/api/crowd-select", tags=["crowd_select"])


@router.get("")
def index(request: Request, page: int = 1, size: int = 20) -> dict[str, Any]:
    rows = store.rows("crowd_select")
    filters=query_filters(request)
    if filters:
        rows = [r for r in rows if all(str(r.get(k)) == str(v) for k, v in filters.items())]
    total = len(rows)
    start = (page - 1) * size
    return {"items": rows[start : start + size], "total": total, "page": page, "size": size}


@router.get("/{entry_id}")
def detail(entry_id: int) -> dict[str, Any]:
    row = store.find("crowd_select", entry_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Not found")
    return row


@router.post("/{condition_id}/recompute")
def recompute(condition_id: str) -> ActionResult:
    try:
        result = rebuild_crowd(condition_id)
        return ActionResult(ok=True, message="Recomputed", entry=result)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/{condition_id}/rosters/{roster_id}")
def roster_detail(condition_id: str, roster_id: str) -> dict[str, Any]:
    roster = store.get_roster(condition_id, roster_id)
    if roster is None:
        raise HTTPException(status_code=404, detail="Roster not found")
    return roster


@router.post("/{condition_id}/run")
def run(condition_id: str) -> ActionResult:
    try:
        result = select_crowd(condition_id)
        return ActionResult(ok=True, message="Selected", entry=result)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/{condition_id}/result")
def result(condition_id: str) -> dict[str, Any]:
    res = get_crowd_result(condition_id)
    if res is None:
        raise HTTPException(status_code=404, detail="Result not found")
    return res


@router.post("/{entry_id}/actions")
def run_action(entry_id: int, payload: ActionPayload) -> ActionResult:
    row = store.find("crowd_select", entry_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Not found")
    action = payload.action
    row["status"] = action
    row["pending"] = False
    return ActionResult(ok=True, message=action, entry=row)
