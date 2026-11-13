import os
import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal

os.environ.setdefault("JWT_SECRET", "test-only-secret-not-for-deployment-12345")

from app.database import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (  # noqa: E402
    MachineType,
    MachineTypeOffering,
    SpotPrice,
    SpotRisk,
    User,
)
from app.security import create_access_token  # noqa: E402
from app.services.placement import PlacementRequest, rank_candidates  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from pydantic import ValidationError  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

NOW = datetime(2026, 10, 31, tzinfo=UTC)


class PlacementTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        Base.metadata.create_all(
            self.engine,
            tables=[
                model.__table__
                for model in (
                    User,
                    MachineType,
                    MachineTypeOffering,
                    SpotPrice,
                    SpotRisk,
                )
            ],
        )
        self.db = Session(self.engine)

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def add_candidate(
        self,
        name,
        price="1",
        risk="0.1",
        *,
        provider="AWS",
        region="ap-southeast-1",
        zone="ap-southeast-1a",
        vcpu=4,
        ram="16",
        gpu_count=0,
    ):
        machine = MachineType(
            provider=provider,
            name=name,
            vcpu=vcpu,
            ram_gib=Decimal(ram),
            gpu_count=gpu_count,
        )
        self.db.add(machine)
        self.db.flush()
        self.db.add(
            MachineTypeOffering(
                machine_type_id=machine.id,
                region=region,
                zone=zone,
            )
        )
        if price is not None:
            self.price(machine, price, region=region, zone=zone)
        if risk is not None:
            self.db.add(
                SpotRisk(
                    machine_type_id=machine.id,
                    region=region,
                    zone=zone,
                    source="TEST",
                    metric="TEST_SCORE",
                    rate_min=Decimal(risk),
                    rate_max=Decimal(risk),
                    observed_at=NOW,
                )
            )
        self.db.flush()
        return machine

    def price(
        self,
        machine,
        value,
        *,
        region="ap-southeast-1",
        zone="ap-southeast-1a",
        kind="SPOT",
        at=NOW,
    ):
        self.db.add(
            SpotPrice(
                machine_type_id=machine.id,
                region=region,
                zone=zone,
                price_type=kind,
                price_per_hour=Decimal(value),
                recorded_at=at,
            )
        )
        self.db.flush()

    def rank(self, **kwargs):
        return rank_candidates(
            self.db, PlacementRequest(vcpu=2, ram_gib=4, **kwargs), now=NOW
        )

    def test_three_modes(self):
        self.add_candidate("cheap", "1", "0.19")
        self.add_candidate("balanced", "2", "0.03")
        self.add_candidate("stable", "5", "0.01")
        for mode, winner in [
            ("SAVING", "cheap"),
            ("BALANCED", "balanced"),
            ("STABLE", "stable"),
        ]:
            with self.subTest(mode=mode):
                results = self.rank(optimization_mode=mode)
                self.assertEqual(results[0].machine_type, winner)
                self.assertTrue(all(0 <= c.score <= 1 for c in results))

    def test_resource_and_location_filters(self):
        self.add_candidate("small", vcpu=1)
        self.add_candidate("low-ram", ram="2")
        self.add_candidate("cpu")
        self.add_candidate("gpu", gpu_count=1)
        self.add_candidate(
            "elsewhere", gpu_count=1, region="us-east-1", zone="us-east-1a"
        )
        self.add_candidate(
            "gcp",
            provider="GCP",
            region="asia-southeast1",
            zone="asia-southeast1-b",
            gpu_count=1,
        )
        results = self.rank(gpu_count=1, region="singapore")
        self.assertEqual({c.machine_type for c in results}, {"gpu", "gcp"})
        self.assertEqual(
            len(self.rank(gpu_count=1, provider="AWS", zone="ap-southeast-1a")), 1
        )
        self.assertEqual(self.rank(region="unknown"), [])

    def test_latest_price_future_and_zone_fallback(self):
        machine = self.add_candidate("a", "2")
        self.price(machine, "9", at=NOW - timedelta(hours=1))
        self.price(machine, "99", at=NOW + timedelta(hours=1))
        self.price(machine, "8", zone=None)
        self.price(machine, "4", kind="ON_DEMAND", zone=None)
        result = self.rank()[0]
        self.assertEqual(result.spot_price_per_hour, Decimal(2))
        self.assertEqual(result.savings_percent, Decimal(50))
        machine = self.add_candidate("regional", None)
        self.price(machine, "3", zone=None)
        self.assertEqual(
            {c.spot_price_per_hour for c in self.rank()}, {Decimal(2), Decimal(3)}
        )

    def test_multiple_locations_filter_before_ranking_and_limit(self):
        self.add_candidate("aws-a", price="2")
        self.add_candidate("aws-b", price="3", zone="ap-southeast-1b")
        self.add_candidate(
            "other-region", price="0.01", region="us-east-1", zone="us-east-1a"
        )
        self.add_candidate(
            "gcp", provider="GCP", region="asia-southeast1", zone="asia-southeast1-b"
        )
        regions = [
            {"provider": "AWS", "region": "ap-southeast-1"},
            {"provider": "GCP", "region": "asia-southeast1"},
        ]
        zones = [
            {**regions[0], "zone": "ap-southeast-1a"},
            {**regions[0], "zone": "ap-southeast-1b"},
            {**regions[1], "zone": "asia-southeast1-b"},
        ]
        self.assertEqual(
            {
                c.machine_type
                for c in self.rank(
                    providers=["AWS", "GCP"], regions=regions, zones=zones
                )
            },
            {"aws-a", "aws-b", "gcp"},
        )
        self.assertEqual(
            {
                c.machine_type
                for c in self.rank(providers=["AWS"], regions=regions, zones=zones)
            },
            {"aws-a", "aws-b"},
        )
        self.assertEqual(
            [
                c.machine_type
                for c in self.rank(regions=regions[:1], zones=zones[:1], limit=1)
            ],
            ["aws-a"],
        )
        # Provider/region/zone belong to the same selection, never a cross product.
        self.assertEqual(
            self.rank(regions=[{"provider": "GCP", "region": "ap-southeast-1"}]), []
        )
        self.assertEqual(
            self.rank(zones=[{**regions[1], "zone": "ap-southeast-1a"}]), []
        )

    def test_no_price_and_wrong_zone_are_excluded(self):
        machine = self.add_candidate("missing", None)
        self.price(machine, "1", zone="ap-southeast-1b")
        self.assertEqual(self.rank(), [])

    def test_price_without_catalog_offering_is_not_eligible(self):
        machine = MachineType(
            provider="AWS", name="unavailable", vcpu=4, ram_gib=Decimal(16), gpu_count=0
        )
        self.db.add(machine)
        self.db.flush()
        self.price(machine, "1")
        self.assertEqual(self.rank(), [])

    def test_zero_demand_and_zone_risk_precedence(self):
        machine = self.add_candidate("a", risk="0.1")
        self.price(machine, "0", kind="ON_DEMAND")
        self.db.add(
            SpotRisk(
                machine_type_id=machine.id,
                region="ap-southeast-1",
                zone=None,
                source="TEST",
                metric="TEST",
                rate_max=Decimal("0.9"),
                observed_at=NOW,
            )
        )
        result = self.rank()[0]
        self.assertEqual(result.risk, Decimal("0.1"))
        self.assertIsNone(result.savings_percent)

    def test_saving_rejects_unknown_high_and_open_bucket_risk(self):
        self.add_candidate("unknown", risk=None)
        self.add_candidate("high", risk="0.8")
        machine = self.add_candidate("open", risk=None)
        self.db.add(
            SpotRisk(
                machine_type_id=machine.id,
                region="ap-southeast-1",
                zone=None,
                source="AWS_SPOT_ADVISOR",
                metric="INTERRUPTION_BUCKET_30D",
                rate_min=Decimal("0.2"),
                rate_max=None,
                observed_at=NOW,
            )
        )
        self.assertEqual(self.rank(optimization_mode="SAVING"), [])
        results = {c.machine_type: c for c in self.rank()}
        self.assertEqual(results["unknown"].risk, Decimal("0.5"))
        self.assertEqual(results["unknown"].risk_source, "DEFAULT")
        self.assertEqual(results["open"].risk, Decimal(1))

    def test_exclusions_expire_and_quota_filters_before_ranking(self):
        self.add_candidate("a")
        self.add_candidate("b")
        key = ("AWS", "ap-southeast-1", "ap-southeast-1a", "a")
        request = PlacementRequest(vcpu=2, ram_gib=4)
        result = rank_candidates(
            self.db, request, now=NOW, exclusions={key: NOW + timedelta(seconds=1)}
        )
        self.assertEqual([c.machine_type for c in result], ["b"])
        self.assertEqual(
            len(rank_candidates(self.db, request, now=NOW, exclusions={key: NOW})), 2
        )
        result = rank_candidates(
            self.db, request, now=NOW, quota_available=lambda c: c.machine_type == "b"
        )
        self.assertEqual([c.machine_type for c in result], ["b"])
        self.assertTrue(result[0].quota_checked)

    def test_ties_singleton_limit_and_zero_price(self):
        self.add_candidate("b", "0")
        self.add_candidate("a", "0")
        results = self.rank(limit=1)
        self.assertEqual(results[0].machine_type, "a")
        self.assertEqual(results[0].score, 0)
        self.assertFalse(results[0].quota_checked)
        self.assertIsNone(results[0].savings_percent)

    def test_latest_risk_and_region_fallback(self):
        machine = self.add_candidate("a", risk=None)
        for at, value in [
            (NOW - timedelta(days=1), "0.8"),
            (NOW, "0.1"),
            (NOW + timedelta(days=1), "0.9"),
        ]:
            self.db.add(
                SpotRisk(
                    machine_type_id=machine.id,
                    region="ap-southeast-1",
                    zone=None,
                    source="TEST",
                    metric="TEST",
                    rate_max=Decimal(value),
                    observed_at=at,
                )
            )
        self.assertEqual(self.rank()[0].risk, Decimal("0.1"))

    def test_input_validation(self):
        for override in [
            {"vcpu": 0},
            {"ram_gib": "NaN"},
            {"gpu_count": -1},
            {"gpu_type": "A100"},
            {"optimization_mode": "invalid"},
            {"limit": 0},
            {"region": ""},
            {"unexpected": True},
        ]:
            with self.subTest(override=override), self.assertRaises(ValidationError):
                PlacementRequest(**({"vcpu": 2, "ram_gib": 4} | override))

    def test_api_auth_validation_and_response(self):
        user = User(
            name="Test",
            email="test@example.com",
            username="test",
            password_hash="unused",
            is_active=True,
        )
        self.db.add(user)
        self.add_candidate("a")
        self.db.commit()
        app.dependency_overrides[get_db] = lambda: self.db
        self.addCleanup(app.dependency_overrides.clear)
        with TestClient(app) as client:
            url = "/api/placement/recommendations"
            payload = {"vcpu": 2, "ram_gib": 4}
            self.assertEqual(client.post(url, json=payload).status_code, 401)
            headers = {"Authorization": "Bearer " + create_access_token(user.id)}
            result = client.post(url, json=payload, headers=headers)
            self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual(result.json()[0]["machine_type"], "a")
            self.assertFalse(result.json()[0]["quota_checked"])
            self.assertEqual(
                client.post(url, json={"vcpu": 0}, headers=headers).status_code, 422
            )
            result = client.post(url, json=payload | {"vcpu": 999}, headers=headers)
            self.assertEqual(result.json(), [])
