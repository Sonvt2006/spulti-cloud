from uuid import UUID

from app.auth import router as auth_router
from app.celery_app import celery_app
from app.tasks import demo_add
from celery.result import AsyncResult
from fastapi import FastAPI, HTTPException
from kombu.exceptions import OperationalError
from pydantic import BaseModel, Field

app = FastAPI(title="Spulti-cloud Control Plane")
app.include_router(auth_router)


class DemoTaskRequest(BaseModel):
    a: int = Field(ge=-1_000_000, le=1_000_000)
    b: int = Field(ge=-1_000_000, le=1_000_000)


@app.get("/health")
def health():
    # Kiểm tra API còn phản hồi.
    # Endpoint này không chứng minh worker đang hoạt động.
    return {"status": "ok", "service": "spulti-api"}


@app.post("/api/tasks/demo", status_code=202)
def submit_demo_task(payload: DemoTaskRequest):
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
def get_task_status(task_id: UUID):
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
