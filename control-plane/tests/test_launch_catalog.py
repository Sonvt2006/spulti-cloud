import unittest
from decimal import Decimal
from unittest.mock import patch

from app.database import Base
from app.models import MachineType, MachineTypeOffering
from app.scripts.sync_launch_catalog import sync_launch_catalog
from app.services.catalog import MachineSpec
from app.services.launch_config import LaunchProfile
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session


class LaunchCatalogTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(
            self.engine, tables=[MachineType.__table__, MachineTypeOffering.__table__]
        )
        self.db = Session(self.engine)
        self.addCleanup(self.engine.dispose)
        self.addCleanup(self.db.close)
        self.aws = LaunchProfile(
            provider="AWS",
            region="us-east-1",
            zone="us-east-1a",
            machine_types=["m6i.xlarge"],
            image_id="ami-test",
            subnet_id="subnet-test",
            security_group_ids=["sg-test"],
        )

    def test_imports_only_allowlisted_types_and_replays_without_duplicates(self):
        with (
            patch(
                "app.scripts.sync_launch_catalog.AWSAdapter.offered_machine_types_in_zone",
                return_value=["m6i.xlarge", "t3.micro"],
            ),
            patch(
                "app.scripts.sync_launch_catalog.AWSAdapter.describe_machine_specs",
                return_value=[MachineSpec("AWS", "m6i.xlarge", 4, Decimal(16))],
            ) as specs,
        ):
            first = sync_launch_catalog(self.db, [self.aws])
            second = sync_launch_catalog(self.db, [self.aws])
        specs.assert_called_with("us-east-1", ["m6i.xlarge"])
        self.assertEqual(first[0]["created"], 1)
        self.assertEqual(second[0]["refreshed"], 1)
        self.assertEqual(len(list(self.db.scalars(select(MachineType)))), 1)
        self.assertEqual(len(list(self.db.scalars(select(MachineTypeOffering)))), 1)

    def test_missing_offering_is_rejected_before_inserting(self):
        with (
            patch(
                "app.scripts.sync_launch_catalog.AWSAdapter.offered_machine_types_in_zone",
                return_value=["t3.micro"],
            ),
            self.assertRaisesRegex(ValueError, "not offered"),
        ):
            sync_launch_catalog(self.db, [self.aws])
        self.assertEqual(list(self.db.scalars(select(MachineType))), [])

    def test_gcp_uses_profile_project_and_zone(self):
        profile = LaunchProfile(
            provider="GCP",
            region="asia-southeast1",
            zone="asia-southeast1-b",
            machine_types=["e2-standard-4"],
            image_id="image-test",
            project_id="test-project",
            subnetwork="test-subnet",
        )
        with (
            patch(
                "app.scripts.sync_launch_catalog.GCPAdapter.offered_machine_types",
                return_value=["e2-standard-4"],
            ),
            patch(
                "app.scripts.sync_launch_catalog.GCPAdapter.describe_machine_specs",
                return_value=[MachineSpec("GCP", "e2-standard-4", 4, Decimal(16))],
            ) as specs,
        ):
            sync_launch_catalog(self.db, [profile])
        specs.assert_called_once_with(
            "test-project", "asia-southeast1-b", ["e2-standard-4"]
        )
        row = self.db.scalar(select(MachineTypeOffering))
        self.assertEqual(row.region, "asia-southeast1")

    def test_empty_config_does_not_populate_demo_catalog(self):
        with self.assertRaisesRegex(ValueError, "SPULTI_LAUNCH_CONFIG"):
            sync_launch_catalog(self.db, [])


if __name__ == "__main__":
    unittest.main()
