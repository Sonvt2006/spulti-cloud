import time

from app.celery_app import celery_app


@celery_app.task(name="spulti.demo_add")
def demo_add(a: int, b: int) -> dict:
    # Giả lập thao tác chậm để quan sát xử lý bất đồng bộ.
    time.sleep(5)

    return {
        "operation": "demo_add",
        "result": a + b,
        "executed_by": "celery-worker",
    }
