import unittest
from unittest.mock import MagicMock, patch

from app.adapters.aws import AWSAdapter
from app.adapters.base import CreateInstanceSpec
from app.adapters.gcp import GCPAdapter
from app.services.cloud_errors import (
    CapacityUnavailableError,
    CreateRejectedError,
    InstanceIdentityError,
)
from google.api_core.exceptions import Forbidden, NotFound, ServiceUnavailable
from google.cloud import compute_v1
from test_ssh_provisioning import public_key


class InstanceLookupTests(unittest.TestCase):
    def spec(self):
        return CreateInstanceSpec(
            machine_id=1,
            name="spulti-test",
            machine_type="e2-standard-4",
            image_id="test-image",
            zone="asia-southeast1-b",
            request_id="e84ca1d8-9ee7-4769-9580-d71fe6129dfe",
            ssh_public_keys=(public_key(),),
        )

    def test_aws_recovers_by_saved_client_token(self):
        with patch("app.adapters.aws.boto3.client") as client:
            client.return_value.describe_instances.return_value = {
                "Reservations": [
                    {
                        "Instances": [
                            {"InstanceId": "i-123", "State": {"Name": "pending"}}
                        ]
                    }
                ]
            }
            adapter = AWSAdapter("ap-southeast-1", "subnet", ["sg"])
            spec = self.spec()
            self.assertEqual(adapter.find_instance(spec).instance_ref, "i-123")
            client.return_value.describe_instances.assert_called_once_with(
                Filters=[{"Name": "client-token", "Values": [spec.request_id]}]
            )
            client.return_value.run_instances.assert_not_called()

    def test_gcp_find_requires_matching_machine_and_request_labels(self):
        with patch("app.adapters.gcp.compute_v1.InstancesClient") as client:
            spec = self.spec()
            vm = compute_v1.Instance(name=spec.name, id=123, status="RUNNING")
            client.return_value.get.return_value = vm
            adapter = GCPAdapter("project", spec.zone, "subnet")
            with self.assertRaises(InstanceIdentityError):
                adapter.find_instance(spec)
            vm.labels = {"spulti-machine-id": "1", "spulti-request-id": spec.request_id}
            self.assertEqual(adapter.find_instance(spec).provider_instance_id, "123")
            client.return_value.get.side_effect = NotFound("absent")
            self.assertIsNone(adapter.find_instance(spec))
            client.return_value.insert.assert_not_called()

    def test_gcp_operation_capacity_code_is_distinct_from_generic_unavailable(self):
        with patch("app.adapters.gcp.compute_v1.InstancesClient") as client:
            operation = MagicMock()
            operation.result.side_effect = ServiceUnavailable("failed")
            operation.error = compute_v1.Error(
                errors=[compute_v1.Errors(code="ZONE_RESOURCE_POOL_EXHAUSTED")]
            )
            client.return_value.insert.return_value = operation
            adapter = GCPAdapter("project", "asia-southeast1-b", "subnet")
            with self.assertRaises(CapacityUnavailableError):
                adapter.create_instance(self.spec())
            operation.error = compute_v1.Error()
            with self.assertRaises(ServiceUnavailable):
                adapter.create_instance(self.spec())

    def test_gcp_insert_denial_is_distinct_from_polling_denial(self):
        with patch("app.adapters.gcp.compute_v1.InstancesClient") as client:
            adapter = GCPAdapter("project", "asia-southeast1-b", "subnet")
            client.return_value.insert.side_effect = Forbidden("insert denied")
            with self.assertRaises(CreateRejectedError):
                adapter.create_instance(self.spec())
            client.return_value.insert.side_effect = None
            operation = client.return_value.insert.return_value
            operation.error = compute_v1.Error()
            operation.result.side_effect = Forbidden("poll denied after acceptance")
            with self.assertRaises(Forbidden):
                adapter.create_instance(self.spec())


if __name__ == "__main__":
    unittest.main()
