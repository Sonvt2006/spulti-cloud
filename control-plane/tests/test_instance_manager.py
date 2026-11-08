import json
import os
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch
from uuid import uuid4

os.environ.setdefault("JWT_SECRET", "test-only-secret-not-for-deployment-12345")

from app.adapters.base import InstanceInfo  # noqa: E402
from app.database import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (  # noqa: E402
    Instance,
    InstanceAttempt,
    Machine,
    MachineLifecycle,
    MachineType,
    MachineTypeOffering,
    SpotPrice,
    SpotRisk,
    SSHKey,
    User,
)
from app.security import create_access_token  # noqa: E402
from app.services.instance_manager import (  # noqa: E402
    reconcile_machine,
    request_delete,
)
from botocore.exceptions import ClientError  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from google.api_core.exceptions import NotFound  # noqa: E402
from kombu.exceptions import OperationalError  # noqa: E402
from sqlalchemy import create_engine, func, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402
from test_ssh_provisioning import public_key  # noqa: E402

MODELS = (
    User,
    SSHKey,
    MachineType,
    MachineTypeOffering,
    SpotPrice,
    SpotRisk,
    Machine,
    Instance,
    MachineLifecycle,
    InstanceAttempt,
)
PROFILES = [
    {
        "provider": "AWS",
        "region": "ap-southeast-1",
        "zone": "ap-southeast-1a",
        "image_id": "ami-test",
        "machine_types": ["m6i.xlarge"],
        "subnet_id": "subnet-test",
        "security_group_ids": ["sg-test"],
    },
    {
        "provider": "GCP",
        "region": "asia-southeast1",
        "zone": "asia-southeast1-b",
        "image_id": "projects/test/global/images/test",
        "machine_types": ["e2-standard-4"],
        "project_id": "test-project",
        "subnetwork": "test-subnet",
    },
]


def seed_manager(db):
    owner = User(
        name="Owner",
        username="owner",
        email="owner@example.com",
        password_hash="unused",
        is_active=True,
    )
    other = User(
        name="Other",
        username="other",
        email="other@example.com",
        password_hash="unused",
        is_active=True,
    )
    db.add_all([owner, other])
    db.flush()
    db.add(
        SSHKey(
            user_id=owner.id, name="laptop", public_key=public_key(), fingerprint="test"
        )
    )
    for index, profile in enumerate(PROFILES):
        machine_type = MachineType(
            provider=profile["provider"],
            name=profile["machine_types"][0],
            vcpu=4,
            ram_gib=Decimal(16),
            gpu_count=0,
        )
        db.add(machine_type)
        db.flush()
        db.add(
            MachineTypeOffering(
                machine_type_id=machine_type.id,
                region=profile["region"],
                zone=profile["zone"],
            )
        )
        db.add(
            SpotPrice(
                machine_type_id=machine_type.id,
                region=profile["region"],
                zone=profile["zone"],
                price_type="SPOT",
                price_per_hour=Decimal("0.1") + index,
                recorded_at=datetime.now(UTC) - timedelta(minutes=1),
            )
        )
    db.commit()
    return owner, other


class FakeCloud:
    def __init__(self, provider="AWS"):
        self.provider = provider
        self.instances = {}
        self.specs = []
        self.deleted = []
        self.on_create = None

    def find_instance(self, spec):
        return self.instances.get(spec.request_id)

    def create_instance(self, spec):
        self.specs.append(spec)
        if self.on_create:
            self.on_create(spec)
        info = InstanceInfo(
            provider=self.provider,
            instance_ref="i-test" if self.provider == "AWS" else spec.name,
            provider_instance_id="i-test" if self.provider == "AWS" else "123456",
            status="pending" if self.provider == "AWS" else "RUNNING",
            public_ip="203.0.113.5",
            private_ip="10.0.0.5",
        )
        self.instances[spec.request_id] = info
        return info

    def get_instance(self, ref):
        for info in self.instances.values():
            if info.instance_ref == ref:
                return info
        raise NotFound("missing")

    def delete_instance(self, ref):
        self.deleted.append(ref)
        for key, info in list(self.instances.items()):
            if info.instance_ref == ref:
                if self.provider == "AWS":
                    self.instances[key] = replace(info, status="terminated")
                else:
                    del self.instances[key]


class InstanceManagerTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        Base.metadata.create_all(self.engine, tables=[m.__table__ for m in MODELS])
        self.db = Session(self.engine, expire_on_commit=False)
        self.owner, self.other = seed_manager(self.db)
        self.env = patch.dict(
            os.environ,
            {
                "SPULTI_LAUNCH_CONFIG": json.dumps(PROFILES),
                "SPULTI_MAX_MACHINES_PER_USER": "5",
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.cloud = FakeCloud()
        self.gcp = FakeCloud("GCP")
        self.factory = patch(
            "app.services.instance_manager.make_adapter",
            side_effect=lambda provider, config: (
                self.cloud if provider == "AWS" else self.gcp
            ),
        ).start()
        self.wake = patch("app.machines.sync_machine.apply_async").start()
        self.addCleanup(patch.stopall)
        app.dependency_overrides[get_db] = lambda: self.db
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        app.dependency_overrides.clear()
        self.db.close()
        self.engine.dispose()

    def headers(self, user=None, key=None):
        headers = {
            "Authorization": "Bearer " + create_access_token((user or self.owner).id)
        }
        if key:
            headers["Idempotency-Key"] = key
        return headers

    def submit(self, key=None, **changes):
        payload = {"name": "train", "vcpu": 4, "ram_gib": 16}
        payload.update(changes)
        return self.client.post(
            "/api/machines", headers=self.headers(key=key), json=payload
        )

    def create(self, **changes):
        response = self.submit(**changes)
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()["id"]

    def tick(self, machine_id):
        life = self.db.get(MachineLifecycle, machine_id)
        life.next_action_at = datetime.now(UTC) - timedelta(seconds=1)
        self.db.commit()
        reconcile_machine(self.db, machine_id)
        self.db.commit()
        return self.db.get(Machine, machine_id)

    def view(self, machine_id):
        return self.client.get(
            f"/api/machines/{machine_id}", headers=self.headers()
        ).json()

    def provision(self, **changes):
        machine_id = self.create(**changes)
        self.tick(machine_id)
        self.tick(machine_id)
        return machine_id

    def test_aws_lifecycle_waits_for_running_and_confirmed_deletion(self):
        machine_id = self.provision()
        self.assertEqual(self.view(machine_id)["status"], "PROVISIONING")
        key = self.cloud.specs[0].request_id
        self.cloud.instances[key] = replace(self.cloud.instances[key], status="running")
        self.tick(machine_id)
        view = self.view(machine_id)
        self.assertEqual(view["status"], "RUNNING")
        self.assertEqual(view["instance"]["public_ip"], "203.0.113.5")
        response = self.client.delete(
            f"/api/machines/{machine_id}", headers=self.headers()
        )
        self.assertTrue(response.json()["delete_requested"])
        self.tick(machine_id)
        self.assertNotEqual(self.view(machine_id)["status"], "DELETED")
        self.tick(machine_id)
        self.assertEqual(self.view(machine_id)["status"], "DELETED")
        instance = self.db.scalar(select(Instance))
        ended = instance.ended_at
        self.tick(machine_id)
        self.assertEqual(instance.ended_at, ended)
        self.assertEqual(len(self.cloud.specs), 1)
        history = self.client.get(
            f"/api/machines/{machine_id}/instances", headers=self.headers()
        )
        self.assertEqual(len(history.json()), 1)
        self.assertIsNotNone(history.json()[0]["ended_at"])

    def test_gcp_uses_name_for_delete_and_numeric_provider_id_for_history(self):
        machine_id = self.provision(provider="GCP")
        view = self.view(machine_id)
        self.assertEqual(view["status"], "RUNNING")
        self.assertEqual(view["instance"]["provider_instance_id"], "123456")
        ref = view["instance"]["instance_ref"]
        request_delete(self.db, self.owner.id, machine_id)
        self.tick(machine_id)
        self.tick(machine_id)
        self.assertEqual(self.gcp.deleted, [ref])
        self.assertEqual(self.view(machine_id)["status"], "DELETED")

    def test_idempotency_replays_same_machine_and_rejects_changed_payload(self):
        key = str(uuid4())
        first = self.submit(key=key)
        second = self.submit(key=key)
        self.assertEqual(first.json()["id"], second.json()["id"])
        self.assertEqual(self.submit(key=key, name="different").status_code, 409)
        self.assertEqual(self.db.scalar(select(func.count()).select_from(Machine)), 1)

    def test_quota_counts_pending_and_is_released_only_after_deleted(self):
        with patch.dict(os.environ, {"SPULTI_MAX_MACHINES_PER_USER": "1"}):
            machine_id = self.create()
            self.assertEqual(self.submit().status_code, 409)
            request_delete(self.db, self.owner.id, machine_id)
            self.assertEqual(self.submit().status_code, 409)
            self.tick(machine_id)
            self.assertEqual(self.submit().status_code, 202)
        self.factory.assert_not_called()

    def test_auth_and_owner_checks_cover_all_routes(self):
        machine_id = self.create()
        path = f"/api/machines/{machine_id}"
        for method, suffix in [
            ("get", ""),
            ("get", "/instances"),
            ("delete", ""),
            ("post", "/retry"),
        ]:
            request = getattr(self.client, method)
            self.assertEqual(request(path + suffix).status_code, 401)
            self.assertEqual(
                request(path + suffix, headers=self.headers(self.other)).status_code,
                404,
            )
        self.assertEqual(
            self.client.get("/api/machines", headers=self.headers(self.other)).json(),
            [],
        )
        self.assertEqual(
            self.client.post(
                "/api/machines", json={"name": "x", "vcpu": 1, "ram_gib": 1}
            ).status_code,
            401,
        )

    def test_invalid_requests_and_missing_keys_never_create_machine(self):
        for changes in [
            {"vcpu": 0},
            {"ram_gib": "NaN"},
            {"disk_size_gb": 0},
            {"name": " "},
            {"startup_script": "echo hi"},
            {"user_id": self.other.id},
        ]:
            self.assertEqual(self.submit(**changes).status_code, 422)
        self.db.delete(self.db.scalar(select(SSHKey)))
        self.db.commit()
        self.assertEqual(self.submit().status_code, 422)
        self.assertEqual(self.db.scalar(select(func.count()).select_from(Machine)), 0)

    def test_missing_config_or_candidate_rolls_back_machine(self):
        with patch.dict(os.environ, {"SPULTI_LAUNCH_CONFIG": "[]"}):
            self.assertEqual(self.submit().status_code, 503)
        self.assertEqual(self.submit(vcpu=999).status_code, 409)
        self.assertEqual(self.db.scalar(select(func.count()).select_from(Machine)), 0)

    def test_broker_failure_keeps_durable_intent(self):
        self.wake.side_effect = OperationalError("redis unavailable")
        machine_id = self.create()
        self.assertEqual(self.view(machine_id)["status"], "PENDING")
        from app import tasks

        with (
            patch.object(tasks, "SessionLocal") as sessions,
            patch.object(tasks.sync_machine, "apply_async") as publish,
        ):
            sessions.return_value.__enter__.return_value = self.db
            tasks.reconcile_machines.run()
            publish.assert_called_once_with(args=[machine_id], retry=False, expires=60)

    def test_snapshot_survives_key_config_changes_and_timeout(self):
        machine_id = self.create()
        saved = self.db.scalar(
            select(InstanceAttempt).order_by(InstanceAttempt.position)
        )
        original = dict(saved.spec)
        self.db.scalar(select(SSHKey)).public_key = public_key()
        self.db.commit()
        self.tick(machine_id)
        with (
            patch.object(self.cloud, "create_instance", side_effect=TimeoutError),
            self.assertLogs("app.services.instance_manager", level="ERROR"),
        ):
            self.tick(machine_id)
        self.assertEqual(saved.state, "CREATING")
        with patch.dict(os.environ, {"SPULTI_LAUNCH_CONFIG": "[]"}):
            self.tick(machine_id)
        self.assertEqual(self.cloud.specs[0].request_id, original["request_id"])
        self.assertEqual(
            list(self.cloud.specs[0].ssh_public_keys), list(original["ssh_public_keys"])
        )
        self.assertEqual(len(self.gcp.specs), 0)

    def test_lost_response_finds_existing_vm_without_second_create(self):
        machine_id = self.create()
        self.tick(machine_id)
        real_create = self.cloud.create_instance

        def lose_response(spec):
            real_create(spec)
            raise TimeoutError("response lost")

        with (
            patch.object(self.cloud, "create_instance", side_effect=lose_response),
            self.assertLogs("app.services.instance_manager", level="ERROR"),
        ):
            self.tick(machine_id)
        self.assertEqual(self.db.scalar(select(func.count()).select_from(Instance)), 0)
        self.tick(machine_id)
        self.assertEqual(len(self.cloud.specs), 1)
        self.assertEqual(self.db.scalar(select(func.count()).select_from(Instance)), 1)

    def test_capacity_failure_moves_to_next_candidate(self):
        machine_id = self.create()
        self.tick(machine_id)
        error = ClientError(
            {"Error": {"Code": "InsufficientInstanceCapacity"}}, "RunInstances"
        )
        with (
            patch.object(self.cloud, "create_instance", side_effect=error),
            self.assertLogs("app.services.instance_manager", level="ERROR"),
        ):
            self.tick(machine_id)
        self.tick(machine_id)
        self.tick(machine_id)
        self.assertEqual(self.view(machine_id)["instance"]["provider"], "GCP")
        self.assertEqual(self.db.scalar(select(func.count()).select_from(Instance)), 1)

    def test_unknown_errors_have_bounded_retries_and_do_not_fallback(self):
        machine_id = self.create()
        self.tick(machine_id)
        with (
            patch.object(self.cloud, "create_instance", side_effect=TimeoutError),
            self.assertLogs("app.services.instance_manager", level="ERROR"),
        ):
            for _ in range(5):
                self.tick(machine_id)
        self.assertTrue(self.view(machine_id)["needs_attention"])
        self.tick(machine_id)
        self.assertEqual(self.gcp.specs, [])
        response = self.client.post(
            f"/api/machines/{machine_id}/retry", headers=self.headers()
        )
        self.assertEqual(response.status_code, 202)
        self.tick(machine_id)
        self.assertFalse(self.view(machine_id)["needs_attention"])

    def test_explicit_create_rejection_can_be_deleted_without_uncertain_vm(self):
        machine_id = self.create()
        self.tick(machine_id)
        error = ClientError(
            {"Error": {"Code": "InvalidAMIID.NotFound"}}, "DescribeImages"
        )
        with (
            patch.object(self.cloud, "create_instance", side_effect=error),
            self.assertLogs("app.services.instance_manager", level="ERROR"),
        ):
            self.tick(machine_id)
        self.assertTrue(self.view(machine_id)["needs_attention"])
        request_delete(self.db, self.owner.id, machine_id)
        self.tick(machine_id)
        self.assertEqual(self.view(machine_id)["status"], "DELETED")
        self.assertEqual(self.cloud.specs, [])

    def test_retry_definitive_rejection_uses_new_config_and_request_id(self):
        machine_id = self.create(provider="AWS")
        self.tick(machine_id)
        error = ClientError(
            {"Error": {"Code": "UnauthorizedOperation"}}, "RunInstances"
        )
        with (
            patch.object(self.cloud, "create_instance", side_effect=error),
            self.assertLogs("app.services.instance_manager", level="ERROR"),
        ):
            self.tick(machine_id)
        old = self.db.scalar(select(InstanceAttempt))
        profiles = [dict(PROFILES[0], image_id="ami-fixed")]
        with patch.dict(os.environ, {"SPULTI_LAUNCH_CONFIG": json.dumps(profiles)}):
            response = self.client.post(
                f"/api/machines/{machine_id}/retry", headers=self.headers()
            )
        self.assertEqual(response.status_code, 202, response.text)
        self.tick(machine_id)
        self.tick(machine_id)
        self.assertEqual(self.cloud.specs[0].image_id, "ami-fixed")
        self.assertNotEqual(self.cloud.specs[0].request_id, old.request_id)

    def test_delete_pending_does_not_call_cloud(self):
        machine_id = self.create()
        request_delete(self.db, self.owner.id, machine_id)
        self.tick(machine_id)
        self.assertEqual(self.view(machine_id)["status"], "DELETED")
        self.factory.assert_not_called()

    def test_delete_prepared_but_not_dispatched_machine_does_not_call_cloud(self):
        machine_id = self.create()
        self.tick(machine_id)
        self.assertEqual(self.view(machine_id)["status"], "PROVISIONING")
        request_delete(self.db, self.owner.id, machine_id)
        self.tick(machine_id)
        self.assertEqual(self.view(machine_id)["status"], "DELETED")
        self.factory.assert_not_called()

    def test_delete_uncertain_creation_does_not_launch_another_vm(self):
        machine_id = self.create()
        self.tick(machine_id)
        with (
            patch.object(self.cloud, "create_instance", side_effect=TimeoutError),
            self.assertLogs("app.services.instance_manager", level="ERROR"),
        ):
            self.tick(machine_id)
        request_delete(self.db, self.owner.id, machine_id)
        for _ in range(5):
            self.tick(machine_id)
        self.assertEqual(self.cloud.specs, [])
        self.assertTrue(self.view(machine_id)["needs_attention"])
        self.assertNotEqual(self.view(machine_id)["status"], "DELETED")

    def test_delete_after_lost_response_finds_and_cleans_up(self):
        machine_id = self.create()
        self.tick(machine_id)
        attempt = self.db.scalar(
            select(InstanceAttempt).order_by(InstanceAttempt.position)
        )
        from app.adapters.base import CreateInstanceSpec

        attempt.state = "CREATING"
        self.db.commit()
        self.cloud.create_instance(CreateInstanceSpec(**attempt.spec))
        request_delete(self.db, self.owner.id, machine_id)
        self.tick(machine_id)
        self.tick(machine_id)
        self.assertEqual(self.view(machine_id)["status"], "DELETED")
        self.assertEqual(len(self.cloud.specs), 1)

    def test_delete_errors_do_not_mark_deleted_and_retry_is_bounded(self):
        machine_id = self.provision()
        request_delete(self.db, self.owner.id, machine_id)
        with (
            patch.object(self.cloud, "delete_instance", side_effect=TimeoutError),
            self.assertLogs("app.services.instance_manager", level="ERROR"),
        ):
            for _ in range(5):
                self.tick(machine_id)
        self.assertTrue(self.view(machine_id)["needs_attention"])
        self.assertIsNone(self.db.scalar(select(Instance)).ended_at)

    def test_disappeared_gcp_vm_enters_recovering_and_preserves_history(self):
        machine_id = self.provision(provider="GCP")
        self.gcp.instances.clear()
        self.tick(machine_id)
        self.assertEqual(self.view(machine_id)["status"], "RECOVERING")
        self.assertTrue(self.view(machine_id)["needs_attention"])
        self.assertIsNotNone(self.db.scalar(select(Instance)).ended_at)
        response = self.client.post(
            f"/api/machines/{machine_id}/retry", headers=self.headers()
        )
        self.assertEqual(response.status_code, 202, response.text)
        self.tick(machine_id)
        # Provider assigns a new numeric id for the replacement.
        with patch.object(
            self.gcp,
            "create_instance",
            return_value=InstanceInfo("GCP", "new-name", "987654", "RUNNING"),
        ):
            self.tick(machine_id)
        self.assertEqual(self.db.scalar(select(func.count()).select_from(Instance)), 2)
        self.assertEqual(self.view(machine_id)["status"], "RUNNING")

    def test_gcp_terminated_means_stopped_not_deleted(self):
        machine_id = self.provision(provider="GCP")
        key = self.gcp.specs[0].request_id
        self.gcp.instances[key] = replace(self.gcp.instances[key], status="TERMINATED")
        self.tick(machine_id)
        self.assertEqual(self.view(machine_id)["status"], "RECOVERING")
        self.assertIsNone(self.db.scalar(select(Instance)).ended_at)

    def test_same_name_replacement_is_never_deleted(self):
        machine_id = self.provision(provider="GCP")
        key = self.gcp.specs[0].request_id
        self.gcp.instances[key] = replace(
            self.gcp.instances[key], provider_instance_id="different-owner"
        )
        request_delete(self.db, self.owner.id, machine_id)
        with self.assertLogs("app.services.instance_manager", level="ERROR"):
            self.tick(machine_id)
        self.assertTrue(self.view(machine_id)["needs_attention"])
        self.assertEqual(self.gcp.deleted, [])

    def test_disabled_owner_is_not_provisioned(self):
        machine_id = self.create()
        self.owner.is_active = False
        self.db.commit()
        self.tick(machine_id)
        self.assertTrue(self.db.get(MachineLifecycle, machine_id).blocked)
        self.factory.assert_not_called()

    def test_repeated_delivery_obeys_next_action_at(self):
        machine_id = self.provision()
        self.factory.reset_mock()
        reconcile_machine(self.db, machine_id)
        self.factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
