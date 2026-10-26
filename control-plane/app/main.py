from decimal import Decimal
from uuid import UUID

from app.api_keys import router as api_keys_router
from app.auth import CurrentUser
from app.auth import router as auth_router
from app.celery_app import celery_app
from app.database import get_db
from app.services.catalog import find_machine_types
from app.tasks import demo_add
from celery.result import AsyncResult
from fastapi import Depends, FastAPI, HTTPException, Query
from kombu.exceptions import OperationalError
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

app = FastAPI(title="Spulti-cloud Control Plane")
app.include_router(auth_router)
app.include_router(api_keys_router)


class DemoTaskRequest(BaseModel):
    a: int = Field(ge=-1_000_000, le=1_000_000)
    b: int = Field(ge=-1_000_000, le=1_000_000)


@app.get("/health")
def health():
    # Kiểm tra API còn phản hồi.
    # Endpoint này không chứng minh worker đang hoạt động.
    return {"status": "ok", "service": "spulti-api"}


@app.post("/api/tasks/demo", status_code=202)
def submit_demo_task(payload: DemoTaskRequest, user: CurrentUser):
    try:
        task = demo_add.delay(payload.a, payload.b)
    except OperationalError as exc:
        raise HTTPException(
            status_code=503,
            detail="Không thể gửi task vào hàng đợi",
        ) from exc

    return {
        "task_id": task.id,
        "status": "QUEUED",
        "status_url": f"/api/tasks/{task.id}",
    }


@app.get("/api/tasks/{task_id}")
def get_task_status(task_id: UUID, user: CurrentUser):
    task = AsyncResult(str(task_id), app=celery_app)
    state = task.state

    response = {
        "task_id": str(task_id),
        "status": state,
    }

    if state == "SUCCESS":
        response["result"] = task.result
    elif state == "FAILURE":
        # Chi tiết exception nằm trong log worker.
        response["error"] = "Task thất bại; kiểm tra log worker"

    return response


VCPU_QUERY = Query(default=None, ge=1)
RAM_QUERY = Query(default=None, gt=0)
GPU_QUERY = Query(default=0, ge=0)
DB_DEPENDENCY = Depends(get_db)


@app.get("/api/catalog")
def get_catalog(
    vcpu: int | None = VCPU_QUERY,
    ram_gib: Decimal | None = RAM_QUERY,
    gpu_count: int = GPU_QUERY,
    provider: str | None = None,
    db: Session = DB_DEPENDENCY,
):
    machine_types = find_machine_types(
        db,
        vcpu=vcpu,
        ram_gib=ram_gib,
        gpu_count=gpu_count,
        provider=provider,
    )

    return [
        {
            "id": machine.id,
            "provider": machine.provider,
            "name": machine.name,
            "vcpu": machine.vcpu,
            "ram_gib": float(machine.ram_gib),
            "gpu_count": machine.gpu_count,
        }
        for machine in machine_types
    ]
