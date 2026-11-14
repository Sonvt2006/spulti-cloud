"""Price validity persistence and exact-scope legacy GCP history repair."""

import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

from app.adapters.base import MarketData, SpotPriceQuote
from app.database import Base
from app.models import MachineType, SpotPrice
from app.scripts.repair_gcp_price_history import repair_gcp_price_history
from app.services.market_data_collector import save_market_data
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

BEGIN = datetime(2026, 9, 7, tzinfo=UTC)
END = BEGIN + timedelta(days=1)
REGION = "asia-southeast1"
QUOTE = SpotPriceQuote("GCP", "e2-micro", REGION, None, Decimal(".040956"), BEGIN)


class MarketPriceValidityTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(
            self.engine, tables=[MachineType.__table__, SpotPrice.__table__]
        )
        self.factory = sessionmaker(self.engine)
        with self.factory.begin() as db:
            db.add_all(
                [
                    MachineType(
                        id=1,
                        provider="GCP",
                        name="e2-micro",
                        vcpu=2,
                        ram_gib=Decimal(1),
                        gpu_count=0,
                    ),
                    MachineType(
                        id=2,
                        provider="AWS",
                        name="m6i.xlarge",
                        vcpu=4,
                        ram_gib=Decimal(16),
                        gpu_count=0,
                    ),
                ]
            )
        for target in (
            "app.services.market_data_collector.SessionLocal",
            "app.scripts.repair_gcp_price_history.SessionLocal",
        ):
            patcher = patch(target, self.factory)
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self):
        self.engine.dispose()

    def save(self, quote):
        return save_market_data(MarketData(prices=[quote], risks=[]))

    def legacy(self, *, zone=None, until=BEGIN):
        with self.factory.begin() as db:
            row = SpotPrice(
                machine_type_id=1,
                region=REGION,
                zone=zone,
                price_type="SPOT",
                price_per_hour=QUOTE.price_per_hour,
                recorded_at=BEGIN,
                valid_until=until,
            )
            db.add(row)
            db.flush()
            return row.id

    def test_open_interval_can_be_closed_in_place_and_resaved_idempotently(self):
        self.assertEqual(self.save(QUOTE)["prices_saved"], 1)
        with self.factory() as db:
            original = db.scalar(select(SpotPrice)).id
        closed = replace(QUOTE, valid_until=END)
        result = self.save(closed)
        self.assertEqual(result["prices_saved"], 0)
        self.assertEqual(result["prices_updated"], 1)
        self.assertEqual(self.save(closed)["prices_updated"], 0)
        with self.factory() as db:
            rows = list(db.scalars(select(SpotPrice)))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].id, original)
            self.assertEqual(rows[0].price_per_hour, QUOTE.price_per_hour)
            self.assertEqual(rows[0].valid_until.replace(tzinfo=UTC), END)

    def test_verified_open_interval_restores_quarantined_row(self):
        original = self.legacy()
        self.assertEqual(self.save(QUOTE)["prices_updated"], 1)
        with self.factory() as db:
            self.assertIsNone(db.get(SpotPrice, original).valid_until)

    def test_identical_legacy_duplicates_are_all_repaired_without_deletion(self):
        first, second = self.legacy(), self.legacy()
        result = self.save(replace(QUOTE, valid_until=END))
        self.assertEqual(result["prices_updated"], 2)
        with self.factory() as db:
            rows = list(db.scalars(select(SpotPrice).order_by(SpotPrice.id)))
            self.assertEqual([row.id for row in rows], [first, second])
            self.assertTrue(
                all(row.valid_until.replace(tzinfo=UTC) == END for row in rows)
            )

    def test_invalid_interval_rolls_back_the_entire_batch(self):
        data = MarketData(
            prices=[
                replace(QUOTE, valid_until=END),
                replace(QUOTE, effective_at=END, valid_until=BEGIN),
            ],
            risks=[],
        )
        with self.assertRaisesRegex(ValueError, "after its start"):
            save_market_data(data)
        with self.factory() as db:
            self.assertEqual(list(db.scalars(select(SpotPrice))), [])

    def test_conflicting_price_does_not_reopen_legacy_interval(self):
        original = self.legacy()
        result = self.save(replace(QUOTE, price_per_hour=Decimal(".03")))
        self.assertEqual(result["price_conflicts"], 1)
        self.assertEqual(result["prices_updated"], 0)
        with self.factory() as db:
            row = db.get(SpotPrice, original)
            self.assertEqual(row.price_per_hour, QUOTE.price_per_hour)
            self.assertEqual(row.valid_until, row.recorded_at)

    def test_storage_rounding_does_not_block_validity_update(self):
        self.legacy()
        result = self.save(
            replace(QUOTE, price_per_hour=Decimal(".040956001"), valid_until=END)
        )
        self.assertEqual(result["price_conflicts"], 0)
        self.assertEqual(result["prices_updated"], 1)

    def test_repair_previews_without_writes_then_restores_exact_scope(self):
        original = self.legacy(zone=f"{REGION}-b")
        returned = replace(QUOTE, zone=f"{REGION}-b", valid_until=END)
        with patch(
            "app.scripts.repair_gcp_price_history.GCPAdapter.get_market_data"
        ) as fetch:
            fetch.return_value = MarketData(prices=[returned], risks=[])
            preview = repair_gcp_price_history("test-project")
            self.assertTrue(preview["dry_run"])
            self.assertEqual(preview["prices_received"], 1)
            self.assertEqual(preview["prices_updated"], 0)
            self.assertEqual(preview["quarantined_remaining"], 1)
            result = repair_gcp_price_history("test-project", apply=True)
            self.assertEqual(result["prices_updated"], 1)
            self.assertEqual(result["quarantined_remaining"], 0)
            self.assertEqual(result["status"], "ok")
            self.assertEqual(fetch.call_args.kwargs["zone"], f"{REGION}-b")
        with self.factory() as db:
            row = db.get(SpotPrice, original)
            self.assertEqual(row.price_per_hour, QUOTE.price_per_hour)
            self.assertEqual(row.valid_until.replace(tzinfo=UTC), END)

    def test_repair_does_not_borrow_a_sibling_zone_or_region(self):
        self.legacy(zone=f"{REGION}-b")
        for region, zone in (
            (REGION, f"{REGION}-a"),
            ("asia-northeast1", "asia-northeast1-b"),
        ):
            with (
                self.subTest(region=region, zone=zone),
                patch(
                    "app.scripts.repair_gcp_price_history.GCPAdapter.get_market_data"
                ) as fetch,
            ):
                fetch.return_value = MarketData(
                    prices=[replace(QUOTE, region=region, zone=zone, valid_until=END)],
                    risks=[],
                )
                result = repair_gcp_price_history("test-project", apply=True)
                self.assertEqual(result["scope_mismatches"], 1)
                self.assertEqual(result["prices_updated"], 0)
                self.assertEqual(result["quarantined_remaining"], 1)

    def test_unavailable_history_stays_quarantined(self):
        self.legacy()
        with patch(
            "app.scripts.repair_gcp_price_history.GCPAdapter.get_market_data"
        ) as fetch:
            fetch.return_value = MarketData(prices=[], risks=[])
            result = repair_gcp_price_history("test-project", apply=True)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["scopes_without_data"], 1)
        self.assertEqual(result["quarantined_remaining"], 1)


if __name__ == "__main__":
    unittest.main()
