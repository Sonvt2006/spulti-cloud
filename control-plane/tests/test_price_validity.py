"""Exclusive price expiry across accounting, placement and submission views."""

import os
import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("JWT_SECRET", "test-only-secret-not-for-deployment-12345")

from app.dashboard import _submission_result  # noqa: E402
from app.database import Base  # noqa: E402
from app.models import (  # noqa: E402
    Instance,
    InstanceAttempt,
    MachineType,
    MachineTypeOffering,
    SpotPrice,
    SpotRisk,
)
from app.services.cost_and_metering import PriceBook, estimate_costs  # noqa: E402
from app.services.placement import PlacementRequest, rank_candidates  # noqa: E402
from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

BEGIN = datetime(2026, 11, 14, tzinfo=UTC)
REGION = "asia-southeast1"
ZONE = f"{REGION}-b"


class PriceValidityTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", poolclass=StaticPool)
        Base.metadata.create_all(
            self.engine,
            tables=[
                model.__table__
                for model in (
                    MachineType,
                    MachineTypeOffering,
                    SpotPrice,
                    SpotRisk,
                )
            ],
        )
        self.db = Session(self.engine)
        self.db.add(
            MachineType(
                id=1,
                provider="GCP",
                name="e2-micro",
                vcpu=2,
                ram_gib=Decimal(1),
                gpu_count=0,
            )
        )
        self.db.add(MachineTypeOffering(machine_type_id=1, region=REGION, zone=ZONE))
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def price(self, at, amount, *, until=None, zone=None, kind="SPOT"):
        row = SpotPrice(
            machine_type_id=1,
            region=REGION,
            zone=zone,
            price_type=kind,
            recorded_at=at,
            valid_until=until,
            price_per_hour=Decimal(amount),
        )
        self.db.add(row)
        self.db.flush()
        return row

    def instance(self, start=BEGIN):
        return Instance(
            id=69,
            machine_id=69,
            machine_type_id=1,
            region=REGION,
            zone=ZONE,
            provider_instance_id="gcp-69",
            started_at=start,
        )

    def book(self):
        return PriceBook(list(self.db.scalars(select(SpotPrice))))

    def rank(self, at):
        return rank_candidates(
            self.db, PlacementRequest(vcpu=1, ram_gib=1, provider="GCP"), now=at
        )

    def test_expiry_is_exclusive_and_zero_width_legacy_price_is_unknown(self):
        until = BEGIN + timedelta(hours=1)
        rate = self.price(BEGIN, ".040956", until=until)
        book, instance = self.book(), self.instance()
        self.assertIsNone(book.rate(instance, "SPOT", BEGIN - timedelta(seconds=1)))
        self.assertIs(book.rate(instance, "SPOT", BEGIN), rate)
        self.assertIs(
            book.rate(instance, "SPOT", until - timedelta(microseconds=1)), rate
        )
        self.assertIsNone(book.rate(instance, "SPOT", until))
        self.price(until, ".01", until=until)
        self.assertIsNone(self.book().rate(instance, "SPOT", until))

    def test_expired_spot_has_partial_cost_and_no_savings_or_current_rate(self):
        self.price(BEGIN, ".040956", until=BEGIN + timedelta(hours=1))
        self.price(BEGIN, ".04", kind="ON_DEMAND")
        result = estimate_costs(
            self.db, [self.instance()], now=BEGIN + timedelta(hours=2)
        )
        cost = result["total"]
        self.assertIsNone(cost["spot_cost"])
        self.assertEqual(cost["known_spot_cost"], "0.040956")
        self.assertEqual(cost["missing_spot_hours"], "1.000000")
        self.assertEqual(cost["on_demand_cost"], "0.080000")
        self.assertIsNone(cost["savings"])
        self.assertIsNone(cost["savings_percent"])
        self.assertIsNone(cost["spot_price_per_hour"])
        self.assertEqual(
            result["instances"][69]["on_demand_price_per_hour"], "0.040000"
        )

    def test_gap_does_not_revive_an_older_unbounded_price(self):
        self.price(BEGIN - timedelta(hours=1), ".01")
        self.price(BEGIN, ".02", until=BEGIN + timedelta(hours=1))
        self.price(BEGIN + timedelta(hours=2), ".03", until=BEGIN + timedelta(hours=3))
        self.price(BEGIN, ".1", kind="ON_DEMAND")
        result = estimate_costs(
            self.db, [self.instance()], now=BEGIN + timedelta(hours=3)
        )
        self.assertEqual(result["total"]["known_spot_cost"], "0.050000")
        self.assertEqual(result["total"]["missing_spot_hours"], "1.000000")
        self.assertIsNone(
            self.book().rate(self.instance(), "SPOT", BEGIN + timedelta(hours=1))
        )
        self.assertIsNone(
            self.book().rate(self.instance(), "SPOT", BEGIN + timedelta(hours=3))
        )

    def test_expiration_splits_costs_across_midnight(self):
        begin = BEGIN - timedelta(minutes=30)
        self.price(begin, ".04", until=begin + timedelta(hours=1))
        self.price(begin, ".1", kind="ON_DEMAND")
        result = estimate_costs(
            self.db,
            [self.instance(begin)],
            start=begin,
            now=begin + timedelta(hours=2),
            daily=True,
        )
        first, second = result["daily"]
        self.assertEqual(first["spot_cost"], "0.020000")
        self.assertEqual(second["known_spot_cost"], "0.020000")
        self.assertEqual(second["missing_spot_hours"], "1.000000")
        self.assertIsNone(second["spot_cost"])
        self.assertEqual(result["total"]["known_spot_cost"], "0.040000")

    def test_regional_fallback_after_zonal_expiry_never_uses_sibling_zone(self):
        self.price(BEGIN - timedelta(hours=1), ".01", zone=ZONE)
        self.price(BEGIN, ".04", zone=ZONE, until=BEGIN + timedelta(hours=1))
        regional = self.price(BEGIN, ".03", until=BEGIN + timedelta(hours=2))
        self.price(BEGIN, ".001", zone=f"{REGION}-a")
        book, instance = self.book(), self.instance()
        self.assertEqual(
            book.rate(instance, "SPOT", BEGIN).price_per_hour, Decimal(".04")
        )
        self.assertIs(book.rate(instance, "SPOT", BEGIN + timedelta(hours=1)), regional)
        self.assertIsNone(book.rate(instance, "SPOT", BEGIN + timedelta(hours=2)))

    def test_valid_negative_savings_are_preserved(self):
        self.price(BEGIN, ".040956", until=BEGIN + timedelta(hours=2))
        self.price(BEGIN, ".04", kind="ON_DEMAND")
        result = estimate_costs(
            self.db, [self.instance()], now=BEGIN + timedelta(hours=1)
        )
        self.assertEqual(result["total"]["savings"], "-0.000956")
        self.assertEqual(result["total"]["savings_percent"], "-2.390000")
        self.assertEqual(
            result["instances"][69]["spot_price_valid_until"],
            BEGIN + timedelta(hours=2),
        )

    def test_placement_excludes_expired_latest_price_without_older_fallback(self):
        self.price(BEGIN - timedelta(hours=1), ".01")
        self.price(BEGIN, ".02", until=BEGIN + timedelta(hours=1))
        self.assertEqual(len(self.rank(BEGIN)), 1)
        self.assertEqual(self.rank(BEGIN + timedelta(hours=1)), [])

    def test_placement_allows_valid_regional_fallback_but_not_sibling_zone(self):
        self.price(BEGIN, ".04", zone=ZONE, until=BEGIN + timedelta(hours=1))
        self.price(BEGIN, ".001", zone=f"{REGION}-a")
        self.assertEqual(self.rank(BEGIN + timedelta(hours=1)), [])
        self.price(BEGIN, ".03", until=BEGIN + timedelta(hours=2))
        candidates = self.rank(BEGIN + timedelta(hours=1))
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].spot_price_per_hour, Decimal(".03"))

    def test_placement_expired_demand_does_not_supply_savings(self):
        self.price(BEGIN, ".01")
        self.price(BEGIN - timedelta(hours=1), ".1", kind="ON_DEMAND")
        self.price(BEGIN, ".04", kind="ON_DEMAND", until=BEGIN + timedelta(hours=1))
        candidate = self.rank(BEGIN + timedelta(hours=1))[0]
        self.assertIsNone(candidate.on_demand_price_per_hour)
        self.assertIsNone(candidate.savings_percent)

    def test_submission_view_uses_valid_price_at_attempt_time(self):
        # Submission rows do not require a real launch: exercise the view's
        # historical quote selection with real SQLite prices and fake identity.
        self.price(BEGIN - timedelta(hours=1), ".01", zone=ZONE)
        self.price(BEGIN, ".04", zone=ZONE, until=BEGIN + timedelta(hours=1))
        self.price(BEGIN, ".001", zone=f"{REGION}-a")
        response = SimpleNamespace(job_id="test", model_dump=lambda **_: {})
        attempt = InstanceAttempt(
            machine_type_id=1,
            region=REGION,
            zone=ZONE,
            provider="GCP",
            created_at=BEGIN + timedelta(hours=1),
        )
        real_scalars = self.db.scalars

        def scalars(query):
            if query.column_descriptions[0]["entity"] is InstanceAttempt:
                return SimpleNamespace(first=lambda: attempt)
            return real_scalars(query)

        with (
            patch.object(self.db, "scalars", side_effect=scalars),
            patch("app.dashboard.machine_response", return_value=response),
        ):
            result = _submission_result(self.db, SimpleNamespace(id=69))
        self.assertIsNone(result["candidate"]["spot_price_per_hour"])


if __name__ == "__main__":
    unittest.main()
