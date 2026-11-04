"""Optional real-lock tests; each run owns a disposable PostgreSQL schema."""

import json
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from unittest.mock import patch
from uuid import uuid4

from app.database import Base
from app.models import Instance, InstanceAttempt, Machine, MachineLifecycle
from app.services.instance_manager import (
    MachineRequest,
    ManagerError,
    create_machine,
    reconcile_machine,
    request_delete,
)
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session
from test_instance_manager import MODELS, PROFILES, FakeCloud, seed_manager


@unittest.skipUnless(
    os.getenv("TEST_POSTGRES_URL"), "Set TEST_POSTGRES_URL for lock tests"
)
class PostgresConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.url = os.environ["TEST_POSTGRES_URL"]
        self.schema = "instance_manager_test_" + uuid4().hex
        self.admin = create_engine(self.url)
        with self.admin.begin() as db:
            db.execute(text(f'CREATE SCHEMA "{self.schema}"'))
        self.engine = create_engine(
            self.url, connect_args={"options": f"-csearch_path={self.schema}"}
        )
        Base.metadata.create_all(self.engine, tables=[m.__table__ for m in MODELS])
        with Session(self.engine) as db:
            owner, _ = seed_manager(db)
            self.owner_id = owner.id
        self.env = patch.dict(
            os.environ,
            {
                "SPULTI_LAUNCH_CONFIG": json.dumps(PROFILES),
                "SPULTI_MAX_MACHINES_PER_USER": "5",
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)

    def tearDown(self):
        self.engine.dispose()
        with self.admin.begin() as db:
            db.execute(text(f'DROP SCHEMA "{self.schema}" CASCADE'))
        self.admin.dispose()

    def submit(self, key):
        with Session(self.engine, expire_on_commit=False) as db:
            return create_machine(
                db, self.owner_id, MachineRequest(name="test", vcpu=4, ram_gib=16), key
            ).id

    def run_worker(self, machine_id):
        with Session(self.engine) as db:
            reconcile_machine(db, machine_id)

    def test_concurrent_submissions_share_idempotency_key(self):
        barrier = Barrier(2)
        key = str(uuid4())

        def submit():
            barrier.wait(timeout=5)
            return self.submit(key)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(submit) for _ in range(2)]
            ids = [future.result(timeout=10) for future in futures]
        self.assertEqual(ids[0], ids[1])
        with Session(self.engine) as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(Machine)), 1)

    def test_concurrent_submissions_cannot_exceed_quota(self):
        barrier = Barrier(2)

        def submit():
            barrier.wait(timeout=5)
            try:
                return self.submit(str(uuid4()))
            except ManagerError as exc:
                self.assertEqual(exc.status_code, 409)
                return None

        with (
            patch.dict(os.environ, {"SPULTI_MAX_MACHINES_PER_USER": "1"}),
            ThreadPoolExecutor(max_workers=2) as pool,
        ):
            futures = [pool.submit(submit) for _ in range(2)]
            ids = [future.result(timeout=10) for future in futures]
        self.assertEqual(sum(value is not None for value in ids), 1)

    def test_duplicate_worker_skips_lock_and_delete_can_arrive_during_create(self):
        machine_id = self.submit(str(uuid4()))
        self.run_worker(machine_id)  # Prepare PROVISIONING before entering cloud.
        entered, release = Event(), Event()
        cloud = FakeCloud()

        def hold_create(spec):
            entered.set()
            if not release.wait(timeout=10):
                raise TimeoutError("test did not release create")

        cloud.on_create = hold_create
        with (
            patch("app.services.instance_manager.make_adapter", return_value=cloud),
            ThreadPoolExecutor(max_workers=3) as pool,
        ):
            first = pool.submit(self.run_worker, machine_id)
            try:
                self.assertTrue(entered.wait(timeout=5))
                duplicate = pool.submit(self.run_worker, machine_id)
                duplicate.result(timeout=3)

                def delete():
                    with Session(self.engine) as db:
                        request_delete(db, self.owner_id, machine_id)

                pool.submit(delete).result(timeout=3)
            finally:
                release.set()
            first.result(timeout=5)
        self.assertEqual(len(cloud.specs), 1)
        self.assertEqual(cloud.deleted, ["i-test"])
        with Session(self.engine) as db:
            self.assertTrue(db.get(MachineLifecycle, machine_id).delete_requested)
            self.assertEqual(db.scalar(select(func.count()).select_from(Instance)), 1)
            self.assertNotEqual(db.get(Machine, machine_id).status, "DELETED")

    def test_create_marker_survives_rollback_after_cloud_success(self):
        machine_id = self.submit(str(uuid4()))
        self.run_worker(machine_id)
        cloud = FakeCloud()
        with patch("app.services.instance_manager.make_adapter", return_value=cloud):
            with (
                Session(self.engine) as db,
                patch.object(
                    db, "commit", side_effect=RuntimeError("worker died before commit")
                ),
                self.assertRaisesRegex(RuntimeError, "worker died"),
            ):
                reconcile_machine(db, machine_id)
            with Session(self.engine) as db:
                self.assertEqual(
                    db.scalar(select(func.count()).select_from(Instance)), 0
                )
                attempt = db.scalar(
                    select(InstanceAttempt).where(InstanceAttempt.state == "CREATING")
                )
                self.assertIsNotNone(attempt)
            self.run_worker(machine_id)
        self.assertEqual(len(cloud.specs), 1)
        with Session(self.engine) as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(Instance)), 1)


if __name__ == "__main__":
    unittest.main()
