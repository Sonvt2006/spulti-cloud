from typing import Annotated

from app.auth import CurrentUser
from app.database import get_db
from app.services.placement import PlacementCandidate, PlacementRequest, rank_candidates
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

router = APIRouter(prefix="/api/placement", tags=["placement"])


@router.post("/recommendations", response_model=list[PlacementCandidate])
def recommendations(
    payload: PlacementRequest,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
) -> list[PlacementCandidate]:
    return rank_candidates(db, payload)
