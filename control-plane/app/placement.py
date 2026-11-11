from typing import Annotated

from app.auth import CurrentUser
from app.database import get_db
from app.services.launch_config import launch_keys, load_launch_profiles
from app.services.placement import PlacementCandidate, PlacementRequest, rank_candidates
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

router = APIRouter(prefix="/api/placement", tags=["placement"])


@router.post("/recommendations", response_model=list[PlacementCandidate])
def recommendations(
    payload: PlacementRequest,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
    launchable_only: bool = False,
) -> list[PlacementCandidate]:
    allowed = None
    if launchable_only:
        try:
            allowed = launch_keys(load_launch_profiles())
        except ValueError as exc:
            raise HTTPException(503, "Cấu hình launch không hợp lệ") from exc
    return rank_candidates(db, payload, allowed_keys=allowed)
