import time
from datetime import UTC, datetime

from app.celery_app import celery_app
from app.database import SessionLocal
from app.models import Machine, MachineLifecycle
from app.services.instance_manager import reconcile_machine
from sqlalchemy import select


@celery_app.task(name="spulti.demo_add")
def demo_add(a: int, b: int) -> dict:
    # Giả lập thao tác chậm để quan sát xử lý bất đồng bộ.
    time.sleep(5)

    return {
        "operation": "demo_add",
        "result": a + b,
        "executed_by": "celery-worker",
    }


@celery_app.task(name="spulti.sync_machine", acks_late=True, reject_on_worker_lost=True)
def sync_machine(machine_id: int) -> None:
    with SessionLocal() as db:
        reconcile_machine(db, machine_id)


@celery_app.task(name="spulti.reconcile_machines", ignore_result=True)
def reconcile_machines() -> None:
    # Database is the durable queue, so a failed Redis publish cannot lose intent.
    with SessionLocal() as db:
        ids = db.scalars(
            select(MachineLifecycle.machine_id)
            .join(Machine, Machine.id == MachineLifecycle.machine_id)
            .where(
                Machine.status != "DELETED",
                MachineLifecycle.blocked.is_(False),
                MachineLifecycle.next_action_at <= datetime.now(UTC),
            )
            .order_by(MachineLifecycle.next_action_at, MachineLifecycle.machine_id)
            .limit(500)
        ).all()
    for machine_id in ids:
        sync_machine.apply_async(args=[machine_id], retry=False, expires=60)
