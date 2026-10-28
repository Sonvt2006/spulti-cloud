import json
import os
import unittest
from dataclasses import asdict, replace
from email import policy
from email.parser import Parser
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

os.environ.setdefault("JWT_SECRET", "test-only-secret-not-for-deployment-12345")

from app.adapters.aws import AWSAdapter  # noqa: E402
from app.adapters.base import CreateInstanceSpec, InstanceInfo  # noqa: E402
from app.adapters.cloud_init import build_aws_user_data  # noqa: E402
from app.adapters.gcp import GCPAdapter  # noqa: E402
from app.database import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import SSHKey, User  # noqa: E402
from app.security import create_access_token  # noqa: E402
from app.services.instance_manager import build_instance_spec  # noqa: E402
from app.ssh import normalize_public_key, public_key_fingerprint  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from google.cloud import compute_v1  # noqa: E402
from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402


def public_key(private_key=None):
    private_key = private_key or ed25519.Ed25519PrivateKey.generate()
    return (
        private_key.public_key()
        .public_bytes(
            serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH
        )
        .decode("ascii")
    )


class SSHProvisioningTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine, tables=[User.__table__, SSHKey.__table__])
        self.db = Session(self.engine, expire_on_commit=False)
        self.owner = User(
            name="Owner",
            username="owner",
            email="owner@example.com",
            password_hash="unused",
            is_active=True,
        )
        self.other = User(
            name="Other",
            username="other",
            email="other@example.com",
            password_hash="unused",
            is_active=True,
        )
        self.db.add_all([self.owner, self.other])
        self.db.commit()
        app.dependency_overrides[get_db] = lambda: self.db
        self.client = TestClient(app)
        self.key = public_key()
        self.other_key = public_key()

    def tearDown(self):
        self.client.close()
        app.dependency_overrides.clear()
        self.db.close()
        self.engine.dispose()

    def headers(self, user=None):
        return {
            "Authorization": "Bearer " + create_access_token((user or self.owner).id)
        }

    def upload(self, key=None, name="laptop", user=None):
        return self.client.post(
            "/api/ssh-keys",
            headers=self.headers(user),
            json={"name": name, "public_key": key or self.key},
        )

    def spec(self, user=None, **overrides):
        params = {
            "user_id": (user or self.owner).id,
            "machine_id": 1,
            "name": "test-vm",
            "machine_type": "t3.small",
            "image_id": "ami-test",
            "zone": "us-east-1c",
            "request_id": "82bc4aa0-5d3e-4abe-994d-dd02a92aaa8b",
            "startup_script": "#!/bin/bash\necho 'ML ready' > /workspace-status\n",
        }
        params.update(overrides)
        return build_instance_spec(self.db, **params)

    def test_register_and_list_keys_are_scoped_to_authenticated_user(self):
        response = self.upload(self.key + " laptop@example.com")
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["public_key"], self.key)
        self.assertEqual(
            response.json()["fingerprint"], public_key_fingerprint(self.key)
        )
        self.assertEqual(self.upload(self.other_key, user=self.other).status_code, 201)
        rows = self.client.get("/api/ssh-keys", headers=self.headers()).json()
        self.assertEqual([row["public_key"] for row in rows], [self.key])
        stored = self.db.scalar(
            select(SSHKey).where(SSHKey.id == response.json()["id"])
        )
        self.assertEqual(stored.user_id, self.owner.id)

    def test_authentication_required(self):
        self.assertEqual(self.client.get("/api/ssh-keys").status_code, 401)
        response = self.client.post(
            "/api/ssh-keys", json={"name": "test", "public_key": self.key}
        )
        self.assertEqual(response.status_code, 401)

    def test_duplicate_material_or_name_returns_conflict(self):
        self.assertEqual(self.upload().status_code, 201)
        self.assertEqual(
            self.upload(self.key + " new-comment", "second").status_code, 409
        )
        self.assertEqual(self.upload(self.other_key).status_code, 409)
        # Same public key is allowed for a different owner.
        self.assertEqual(self.upload(user=self.other).status_code, 201)

    def test_invalid_private_multiline_and_options_keys_rejected(self):
        private = (
            ed25519.Ed25519PrivateKey.generate()
            .private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.OpenSSH,
                serialization.NoEncryption(),
            )
            .decode()
        )
        bad_keys = [
            "ssh-ed25519 AAAA",
            private,
            self.key + "\n" + self.other_key,
            'command="touch /tmp/injected" ' + self.key,
            "x" * 8193,
        ]
        for key in bad_keys:
            with self.subTest(key=key[:20]):
                self.assertEqual(self.upload(key).status_code, 422)
        self.assertEqual(self.db.scalars(select(SSHKey)).all(), [])

    def test_rsa_key_validation(self):
        valid = public_key(
            rsa.generate_private_key(public_exponent=65537, key_size=2048)
        )
        self.assertEqual(normalize_public_key(valid), valid)
        weak = public_key(
            rsa.generate_private_key(public_exponent=65537, key_size=1024)
        )
        with self.assertRaises(ValueError):
            normalize_public_key(weak)

    def test_missing_or_inactive_owner_and_missing_keys_fail_before_provisioning(self):
        with self.assertRaisesRegex(ValueError, "public key"):
            self.spec()
        self.upload()
        self.owner.is_active = False
        self.db.commit()
        with self.assertRaises(ValueError):
            self.spec()
        with self.assertRaises(ValueError):
            self.spec(user_id=9999)

    def test_spec_contains_only_owner_keys_and_snapshot_survives_key_addition(self):
        self.upload()
        self.upload(self.other_key, user=self.other)
        spec = self.spec()
        self.assertEqual(spec.ssh_public_keys, (self.key,))
        snapshot = json.dumps(asdict(spec))
        self.upload(public_key(), "second-device")
        restored = CreateInstanceSpec(**json.loads(snapshot))
        self.assertEqual(restored, spec)
        self.assertEqual(len(self.spec().ssh_public_keys), 2)

    def test_invalid_username_and_empty_keys_rejected(self):
        self.upload()
        spec = self.spec()
        for username in ("root", "alice\nroot", "$(id)"):
            with self.subTest(username=username), self.assertRaises(ValueError):
                replace(spec, ssh_username=username)
        with self.assertRaises(ValueError):
            replace(spec, ssh_public_keys=())

    def test_aws_uses_db_keys_in_cloud_init_without_keypair_and_keeps_script(self):
        self.upload()
        self.upload(self.other_key, "second-device")
        spec = self.spec()
        with patch("app.adapters.aws.boto3.client") as client:
            ec2 = client.return_value
            ec2.describe_images.return_value = {
                "Images": [{"RootDeviceType": "ebs", "RootDeviceName": "/dev/sda1"}]
            }
            ec2.run_instances.return_value = {
                "Instances": [{"InstanceId": "i-test", "State": {"Name": "pending"}}]
            }
            adapter = AWSAdapter("us-east-1", "subnet-test", ["sg-test"])
            adapter.create_instance(spec)
            first = ec2.run_instances.call_args.kwargs
            adapter.create_instance(spec)
            self.assertEqual(first, ec2.run_instances.call_args.kwargs)
            self.assertNotIn("KeyName", first)
            self.assertEqual(first["InstanceMarketOptions"]["MarketType"], "spot")
            message = Parser(policy=policy.default).parsestr(first["UserData"])
            parts = list(message.iter_parts())
            self.assertEqual(parts[0].get_content_type(), "text/cloud-config")
            config = json.loads(parts[0].get_content().split("\n", 1)[1])
            self.assertEqual(config["users"][0]["name"], "spulti")
            self.assertEqual(
                config["users"][0]["ssh_authorized_keys"], [self.key, self.other_key]
            )
            self.assertFalse(config["ssh_pwauth"])
            self.assertEqual(parts[1].get_content_type(), "text/x-shellscript")
            self.assertEqual(parts[1].get_content(), spec.startup_script)

    def test_cloud_init_handles_no_script_and_rejects_oversize_payload(self):
        self.upload()
        spec = self.spec(startup_script="")
        message = Parser(policy=policy.default).parsestr(build_aws_user_data(spec))
        self.assertEqual(len(list(message.iter_parts())), 1)
        with self.assertRaisesRegex(ValueError, "16 KiB"):
            build_aws_user_data(
                replace(spec, startup_script="#!/bin/bash\n" + "x" * 16384)
            )
        with self.assertRaisesRegex(ValueError, "shebang"):
            build_aws_user_data(replace(spec, startup_script="echo missing-shebang"))

    def test_gcp_uses_only_request_keys_in_metadata(self):
        self.upload()
        self.upload(self.other_key, user=self.other)
        spec = self.spec(zone="asia-southeast1-b", machine_type="e2-standard-2")
        with patch("app.adapters.gcp.compute_v1.InstancesClient") as client:
            client.return_value.get.return_value = compute_v1.Instance(
                name=spec.name, id=42, status="RUNNING"
            )
            adapter = GCPAdapter("test-project", spec.zone, "test-subnet")
            adapter.create_instance(spec)
            vm = client.return_value.insert.call_args.kwargs[
                "request"
            ].instance_resource
            metadata = {item.key: item.value for item in vm.metadata.items}
            self.assertEqual(metadata["ssh-keys"], "spulti:" + self.key)
            self.assertEqual(metadata["block-project-ssh-keys"], "true")
            self.assertEqual(metadata["enable-oslogin"], "FALSE")
            self.assertEqual(metadata["startup-script"], spec.startup_script)
            self.assertEqual(vm.scheduling.provisioning_model, "SPOT")

    def test_gcp_script_reuses_saved_keys_without_reading_database_on_retry(self):
        from app.scripts import try_gcp_adapter

        self.upload()
        with TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            with (
                patch.object(try_gcp_adapter, "STATE_FILE", state_path),
                patch.object(try_gcp_adapter, "SessionLocal") as session_factory,
                patch.object(try_gcp_adapter, "GCPAdapter") as adapter_factory,
                patch(
                    "sys.argv",
                    ["try_gcp_adapter", "create", "--user-id", str(self.owner.id)],
                ),
                patch("builtins.print"),
            ):
                session_factory.return_value.__enter__.return_value = self.db
                adapter_factory.return_value.create_instance.return_value = (
                    # A serializable response without contacting either cloud.
                    InstanceInfo("GCP", "test", "42", "RUNNING")
                )
                try_gcp_adapter.main()
                first = adapter_factory.return_value.create_instance.call_args.args[0]
                self.upload(public_key(), "new-key")
                try_gcp_adapter.main()
                second = adapter_factory.return_value.create_instance.call_args.args[0]
                self.assertEqual(first, second)
                session_factory.assert_called_once()


if __name__ == "__main__":
    unittest.main()
