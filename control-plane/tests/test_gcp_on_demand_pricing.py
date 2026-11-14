"""GCP Catalog parsing, collection, persistence and cost coverage regressions."""

import os
import unittest
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

os.environ.setdefault("JWT_SECRET", "test-only-secret-not-for-deployment-12345")

from app.adapters.gcp import GCPAdapter  # noqa: E402
from app.celery_app import celery_app  # noqa: E402
from app.database import Base  # noqa: E402
from app.models import Instance, MachineType, SpotPrice  # noqa: E402
from app.services.catalog import MachineSpec  # noqa: E402
from app.services.cost_and_metering import estimate_costs  # noqa: E402
from app.services.market_data_collector import save_market_data  # noqa: E402
from app.services.on_demand_pricing import (  # noqa: E402
    CATALOG_URL,
    _catalog_skus,
    collect_gcp_on_demand,
    gcp_on_demand_prices,
)
from app.tasks import collect_market_data  # noqa: E402
from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

NOW = datetime(2026, 11, 14, tzinfo=UTC)
EFFECTIVE = NOW - timedelta(hours=12)
REGION = "asia-southeast1"
TOKYO = "asia-northeast1"
SERVICE = "app.services.on_demand_pricing"


def sku(component, nanos, *, region=REGION, at=EFFECTIVE):
    return {
        "description": f"E2 Instance {component} running in Singapore",
        "category": {"resourceFamily": "Compute", "usageType": "OnDemand"},
        "serviceRegions": [region],
        "pricingInfo": [
            {
                "effectiveTime": at.isoformat(),
                "pricingExpression": {
                    "usageUnit": "h" if component == "Core" else "GiBy.h",
                    "displayQuantity": 1000,
                    "tieredRates": [
                        {
                            "startUsageAmount": 0,
                            "unitPrice": {
                                "currencyCode": "USD",
                                "units": "0",
                                "nanos": nanos,
                            },
                        }
                    ],
                },
            }
        ],
    }


def spec(name="e2-standard-4", cpus=4, ram="16"):
    return MachineSpec("GCP", name, cpus, Decimal(ram))


class CatalogPriceTests(unittest.TestCase):
    def setUp(self):
        self.skus = [sku("Core", 20_000_000), sku("Ram", 3_000_000)]

    def quotes(self, specs=None):
        return gcp_on_demand_prices(self.skus, REGION, specs or [spec()], now=NOW)

    def test_cpu_ram_combination_and_regional_effective_time(self):
        self.skus[1]["pricingInfo"][0]["effectiveTime"] = (
            EFFECTIVE + timedelta(hours=1)
        ).isoformat()
        quote = self.quotes()[0]
        self.assertEqual(quote.price_per_hour, Decimal(".128"))
        self.assertEqual(quote.effective_at, EFFECTIVE + timedelta(hours=1))
        self.assertEqual(quote.provider, "GCP")
        self.assertEqual(quote.region, REGION)
        self.assertEqual(quote.price_type, "ON_DEMAND")
        self.assertIsNone(quote.zone)

    def test_shared_core_uses_fractional_cpus_instead_of_two_guest_cpus(self):
        quotes = self.quotes(
            [
                spec("e2-micro", 2, "1"),
                spec("e2-small", 2, "2"),
                spec("e2-medium", 2, "4"),
            ]
        )
        self.assertEqual(
            [q.price_per_hour for q in quotes],
            [Decimal(".008"), Decimal(".016"), Decimal(".032")],
        )

    def test_highcpu_highmem_and_unsupported_types(self):
        quotes = self.quotes(
            [
                spec("e2-highcpu-8", 8, "8"),
                spec("e2-highmem-2", 2, "16"),
                spec("e2-custom-4-16384"),
                spec("n2-standard-4"),
                spec("e2-standard-4-metal"),
                MachineSpec("GCP", "e2-standard-4", 4, Decimal(16), 1),
                MachineSpec("AWS", "e2-standard-4", 4, Decimal(16)),
            ]
        )
        self.assertEqual(
            [q.price_per_hour for q in quotes], [Decimal(".184"), Decimal(".088")]
        )

    def test_ignores_other_regions_discounts_custom_spot_and_licenses(self):
        extras = []
        for description in (
            "Spot Preemptible E2 Instance Core running in Singapore",
            "E2 Custom Instance Core running in Singapore",
            "E2 Instance Core running free tier discount",
            "Licensing Fee for E2 Instance Core running in Singapore",
        ):
            extra = sku("Core", 900_000_000)
            extra["description"] = description
            extras.append(extra)
        extra = sku("Core", 900_000_000)
        extra["category"]["usageType"] = "Commit1Yr"
        extras.append(extra)
        extra = sku("Core", 900_000_000)
        extra["category"]["resourceFamily"] = "Storage"
        extras.append(extra)
        extras.append(sku("Core", 900_000_000, region=TOKYO))
        self.skus += extras
        self.assertEqual(self.quotes()[0].price_per_hour, Decimal(".128"))
        self.assertEqual(gcp_on_demand_prices(self.skus, "us-east1", [spec()]), [])

    def test_latest_effective_version_ignores_future_price(self):
        core = self.skus[0]
        for days, nanos in [(-1, 10_000_000), (1, 30_000_000)]:
            core["pricingInfo"] += sku(
                "Core", nanos, at=EFFECTIVE + timedelta(days=days)
            )["pricingInfo"]
        self.assertEqual(self.quotes()[0].price_per_hour, Decimal(".128"))

    def test_tokyo_uses_its_own_regional_rates(self):
        self.skus += [
            sku("Core", 30_000_000, region=TOKYO),
            sku("Ram", 4_000_000, region=TOKYO),
        ]
        tokyo = gcp_on_demand_prices(self.skus, TOKYO, [spec()], now=NOW)[0]
        self.assertEqual(tokyo.region, TOKYO)
        self.assertEqual(tokyo.price_per_hour, Decimal(".184"))
        self.assertEqual(self.quotes()[0].price_per_hour, Decimal(".128"))

    def test_missing_ram_or_future_only_never_produces_partial_machine_price(self):
        self.skus = [self.skus[0]]
        self.assertEqual(self.quotes(), [])
        self.skus.append(sku("Ram", 3_000_000, at=NOW + timedelta(hours=1)))
        self.assertEqual(self.quotes(), [])

    def test_rejects_non_usd_non_hourly_and_tiered_rates(self):
        original = deepcopy(self.skus)
        for mode in ("currency", "unit", "tiers", "start"):
            with self.subTest(mode=mode):
                self.skus = deepcopy(original)
                expression = self.skus[0]["pricingInfo"][0]["pricingExpression"]
                if mode == "currency":
                    expression["tieredRates"][0]["unitPrice"]["currencyCode"] = "VND"
                elif mode == "unit":
                    expression["usageUnit"] = "s"
                elif mode == "tiers":
                    expression["tieredRates"] *= 2
                else:
                    expression["tieredRates"][0]["startUsageAmount"] = 10
                self.assertEqual(self.quotes(), [])

    def test_money_units_and_nanos(self):
        money = self.skus[0]["pricingInfo"][0]["pricingExpression"]["tieredRates"][0]
        money["unitPrice"]["units"] = "1"
        self.assertEqual(self.quotes()[0].price_per_hour, Decimal("4.128"))

    def test_nonpositive_prices_fail_and_conflicting_skus_fail(self):
        for amount in (-1, 0):
            with self.subTest(amount=amount), self.assertRaises(ValueError):
                gcp_on_demand_prices(
                    [sku("Core", amount), self.skus[1]], REGION, [spec()], now=NOW
                )
        self.skus.append(sku("Core", 21_000_000))
        with self.assertRaisesRegex(ValueError, "Ambiguous"):
            self.quotes()

    def test_pagination_requests_usd_and_keeps_e2_only(self):
        session = MagicMock()
        first, second = MagicMock(), MagicMock()
        first.json.return_value = {"skus": [self.skus[0]], "nextPageToken": "page-2"}
        other = sku("Core", 20_000_000)
        other["description"] = "N2 Instance Core running in Singapore"
        second.json.return_value = {"skus": [self.skus[1], other]}
        session.get.side_effect = [first, second]
        self.assertEqual(_catalog_skus(session), self.skus)
        calls = session.get.call_args_list
        self.assertEqual(calls[0].args, (CATALOG_URL,))
        self.assertEqual(
            calls[0].kwargs["params"], {"currencyCode": "USD", "pageSize": 5000}
        )
        self.assertEqual(calls[1].kwargs["params"]["pageToken"], "page-2")
        first.raise_for_status.assert_called_once()
        second.raise_for_status.assert_called_once()


class CollectionTests(unittest.TestCase):
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
                        provider="GCP",
                        name="a2-highgpu-1g",
                        vcpu=12,
                        ram_gib=Decimal(85),
                        gpu_count=1,
                    ),
                ]
            )
        self.skus = [sku("Core", 20_000_000), sku("Ram", 3_000_000)]

    def tearDown(self):
        self.engine.dispose()

    def test_gcp_spot_keeps_provider_effective_date_when_collected_again(self):
        effective = datetime(2026, 9, 7, tzinfo=UTC)
        price_history = [
            {
                "interval": {"startTime": at.isoformat()},
                "listPrice": {"currencyCode": "USD", "units": "0", "nanos": nanos},
            }
            for at, nanos in (
                (effective, 34_460_000),
                (effective - timedelta(days=1), 40_000_000),
            )
        ]
        with (
            patch("google.auth.default", return_value=(MagicMock(), None)),
            patch("google.auth.transport.requests.AuthorizedSession") as session,
            patch("app.services.market_data_collector.SessionLocal", self.factory),
        ):
            response = session.return_value.post.return_value
            response.json.return_value = {"priceHistory": price_history}
            outcomes = []
            for _ in range(2):
                data = GCPAdapter.get_market_data(
                    "test-project", REGION, ["e2-micro"], include_risk=False
                )
                self.assertEqual(len(data.prices), 2)
                self.assertEqual(data.prices[-1].effective_at, effective)
                self.assertEqual(data.prices[-1].price_per_hour, Decimal(".03446"))
                outcomes.append(save_market_data(data))
            self.assertEqual(session.return_value.post.call_count, 2)
        self.assertEqual([outcome["prices_saved"] for outcome in outcomes], [2, 0])
        with self.factory() as db:
            rows = list(db.scalars(select(SpotPrice).order_by(SpotPrice.recorded_at)))
            self.assertEqual(len(rows), 2)
            self.assertTrue(all(row.price_type == "SPOT" for row in rows))
            self.assertEqual(rows[-1].recorded_at.replace(tzinfo=UTC), effective)
            self.assertEqual(rows[-1].price_per_hour, Decimal(".03446"))

    def test_persist_idempotently_and_feed_dashboard_costs_without_backdating(self):
        with (
            patch(f"{SERVICE}.SessionLocal", self.factory),
            patch("app.services.market_data_collector.SessionLocal", self.factory),
            patch(f"{SERVICE}.google.auth.default", return_value=(MagicMock(), None)),
            patch(f"{SERVICE}.AuthorizedSession"),
            patch(f"{SERVICE}._catalog_skus", return_value=self.skus),
        ):
            first = collect_gcp_on_demand([REGION, REGION], limit_types=1)
            second = collect_gcp_on_demand([REGION], limit_types=1)
        self.assertEqual(first["status"], "ok")
        self.assertEqual(first["prices_saved"], 1)
        self.assertEqual(second["prices_saved"], 0)
        with self.factory() as db:
            row = db.scalar(select(SpotPrice))
            self.assertEqual(row.price_type, "ON_DEMAND")
            self.assertEqual(row.price_per_hour, Decimal(".008"))
            instance = Instance(
                id=65,
                machine_id=65,
                machine_type_id=1,
                region=REGION,
                zone=f"{REGION}-b",
                started_at=EFFECTIVE - timedelta(hours=1),
            )
            result = estimate_costs(db, [instance], now=EFFECTIVE + timedelta(hours=1))
            cost = result["instances"][65]
            self.assertEqual(cost["on_demand_price_per_hour"], "0.008000")
            self.assertIsNone(cost["on_demand_cost"])
            self.assertEqual(cost["known_on_demand_cost"], "0.008000")
            self.assertEqual(cost["missing_on_demand_hours"], "1.000000")
            instance.started_at = EFFECTIVE
            cost = estimate_costs(db, [instance], now=EFFECTIVE + timedelta(hours=1))
            self.assertEqual(cost["total"]["on_demand_cost"], "0.008000")

    def test_api_failure_does_not_persist_prices(self):
        with (
            patch(f"{SERVICE}.SessionLocal", self.factory),
            patch(f"{SERVICE}.google.auth.default", return_value=(MagicMock(), None)),
            patch(f"{SERVICE}.AuthorizedSession") as session,
            patch(f"{SERVICE}.save_market_data") as save,
        ):
            response = session.return_value.__enter__.return_value.get.return_value
            response.raise_for_status.side_effect = RuntimeError("Forbidden")
            with self.assertRaisesRegex(RuntimeError, "Forbidden"):
                collect_gcp_on_demand([REGION])
            save.assert_not_called()

    def test_missing_region_is_reported_without_fabricating_a_price(self):
        with (
            patch(f"{SERVICE}.SessionLocal", self.factory),
            patch("app.services.market_data_collector.SessionLocal", self.factory),
            patch(f"{SERVICE}.google.auth.default", return_value=(MagicMock(), None)),
            patch(f"{SERVICE}.AuthorizedSession"),
            patch(f"{SERVICE}._catalog_skus", return_value=self.skus),
        ):
            result = collect_gcp_on_demand([REGION, TOKYO])
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["prices_received"], 1)
        self.assertEqual(result["prices_missing"], 1)

    def test_empty_e2_catalog_needs_no_cloud_credentials(self):
        with self.factory.begin() as db:
            db.delete(db.get(MachineType, 1))
        with (
            patch(f"{SERVICE}.SessionLocal", self.factory),
            patch(f"{SERVICE}.google.auth.default") as credentials,
        ):
            self.assertEqual(collect_gcp_on_demand([REGION])["status"], "empty")
            credentials.assert_not_called()


class TaskRoutingTests(unittest.TestCase):
    def test_daily_beat_entry_dispatches_enabled_gcp_on_demand_task(self):
        entries = [
            entry
            for entry in celery_app.conf.beat_schedule.values()
            if tuple(entry.get("args", ())) == ("GCP", "on_demand")
        ]
        self.assertEqual(len(entries), 1, "GCP On-demand needs one daily Beat task")
        entry = entries[0]
        self.assertEqual(entry["task"], "spulti.market.collect")
        schedule = entry["schedule"]
        today = NOW.replace(hour=1, minute=5)
        previous = today - timedelta(days=1)
        with patch.object(schedule, "nowfun", lambda: today - timedelta(minutes=1)):
            due = schedule.is_due(previous)
            self.assertFalse(due.is_due)
            self.assertEqual(due.next, 60)
        with patch.object(schedule, "nowfun", lambda: today):
            due = schedule.is_due(previous)
            self.assertTrue(due.is_due)
            self.assertEqual(due.next, 24 * 60 * 60)
        with (
            patch.dict(
                os.environ,
                {
                    "MARKET_GCP_PROJECT_ID": "test-project",
                    "MARKET_GCP_ZONES": f"{REGION}-b",
                    "MARKET_LIMIT_TYPES": "5",
                },
            ),
            patch("app.tasks.Redis.from_url") as redis_factory,
            patch(f"{SERVICE}.collect_gcp_on_demand") as collect,
        ):
            redis_factory.return_value.set.return_value = True
            collect.return_value = {"status": "ok", "prices_saved": 1}
            task = celery_app.tasks[entry["task"]]
            self.assertEqual(task.run(*entry["args"]), collect.return_value)
            collect.assert_called_once_with([REGION], 5)

    def test_aws_on_demand_dispatch_remains_separate_from_gcp(self):
        with (
            patch.dict(
                os.environ,
                {
                    "MARKET_AWS_REGIONS": "ap-southeast-1,us-east-1",
                    "MARKET_LIMIT_TYPES": "",
                    "MARKET_GCP_PROJECT_ID": "",
                    "MARKET_GCP_ZONES": "",
                },
            ),
            patch("app.tasks.Redis.from_url") as redis_factory,
            patch(f"{SERVICE}.collect_aws_on_demand") as collect_aws,
            patch(f"{SERVICE}.collect_gcp_on_demand") as collect_gcp,
        ):
            redis_factory.return_value.set.return_value = True
            collect_aws.return_value = {"status": "ok", "prices_saved": 2}
            self.assertEqual(
                collect_market_data("aws", "ON_DEMAND"), collect_aws.return_value
            )
            collect_aws.assert_called_once_with(["ap-southeast-1", "us-east-1"], None)
            collect_gcp.assert_not_called()
            self.assertEqual(
                redis_factory.return_value.set.call_args.args[0],
                "spulti:market:lock:aws:on_demand",
            )

    def test_gcp_routes_unique_regions_and_always_releases_lease(self):
        for fails in (False, True):
            with (
                self.subTest(fails=fails),
                patch.dict(
                    os.environ,
                    {
                        "MARKET_GCP_PROJECT_ID": "test-project",
                        "MARKET_GCP_ZONES": f"{REGION}-b,{TOKYO}-a,{REGION}-c",
                        "MARKET_LIMIT_TYPES": "7",
                    },
                ),
                patch("app.tasks.Redis.from_url") as redis_factory,
                patch(f"{SERVICE}.collect_gcp_on_demand") as collect,
            ):
                redis = redis_factory.return_value
                redis.set.return_value = True
                collect.return_value = {"status": "ok", "prices_saved": 2}
                if fails:
                    collect.side_effect = RuntimeError("Catalog unavailable")
                    with self.assertRaisesRegex(RuntimeError, "Catalog unavailable"):
                        collect_market_data("gcp", "ON_DEMAND")
                else:
                    self.assertEqual(
                        collect_market_data("gcp", "ON_DEMAND"), collect.return_value
                    )
                collect.assert_called_once_with([TOKYO, REGION], 7)
                self.assertEqual(
                    redis.set.call_args.args[0], "spulti:market:lock:gcp:on_demand"
                )
                redis.eval.assert_called_once()
                redis.close.assert_called_once()

    def test_overlap_guard_skips_gcp_collection(self):
        with (
            patch("app.tasks.Redis.from_url") as redis_factory,
            patch(f"{SERVICE}.collect_gcp_on_demand") as collect,
        ):
            redis_factory.return_value.set.return_value = False
            self.assertEqual(
                collect_market_data("GCP", "on_demand")["status"], "skipped"
            )
            collect.assert_not_called()
            redis_factory.return_value.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
