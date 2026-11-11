"""Dashboard accounting, authorization and launch-selection regressions."""

import json
import os
import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch
from uuid import uuid4

os.environ.setdefault("JWT_SECRET", "test-only-secret-not-for-deployment-12345")

import test_instance_manager as manager_tests  # noqa: E402
from app.adapters.base import MarketData, SpotPriceQuote  # noqa: E402
from app.models import (  # noqa: E402
    AgentJob,
    Instance,
    InstanceAttempt,
    Machine,
    MachineJob,
    MachineLifecycle,
    SpotPrice,
)
from app.services.cost_and_metering import estimate_costs  # noqa: E402
from app.services.instance_manager import (  # noqa: E402
    MachineRequest,
    _prepare_attempts,
)
from app.services.market_data_collector import save_market_data  # noqa: E402
from app.services.on_demand_pricing import aws_on_demand_prices  # noqa: E402
from sqlalchemy import delete, select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from test_instance_manager import PROFILES  # noqa: E402


class DashboardTests(unittest.TestCase):
    # Reuse the existing isolated SQLite/cloud fixture, not its test methods.
    setUp = manager_tests.InstanceManagerTests.setUp
    tearDown = manager_tests.InstanceManagerTests.tearDown
    headers = manager_tests.InstanceManagerTests.headers
    submit = manager_tests.InstanceManagerTests.submit
    create = manager_tests.InstanceManagerTests.create
    tick = manager_tests.InstanceManagerTests.tick
    provision = manager_tests.InstanceManagerTests.provision

    def instance(self, start, end=None, *, machine_id=None, zone=None):
        row = Instance(
            machine_id=machine_id or self.create(),
            machine_type_id=1,
            provider_instance_id=str(uuid4()),
            region="ap-southeast-1",
            zone=zone or "ap-southeast-1a",
            started_at=start,
            ended_at=end,
        )
        self.db.add(row)
        self.db.commit()
        return row

    def price(self, at, amount, kind="SPOT", zone="ap-southeast-1a"):
        self.db.add(
            SpotPrice(
                machine_type_id=1,
                region="ap-southeast-1",
                zone=zone,
                recorded_at=at,
                price_per_hour=Decimal(amount),
                price_type=kind,
            )
        )
        self.db.commit()

    def test_price_changes_recovery_gap_and_utc_days(self):
        begin = datetime(2026, 1, 1, 23, tzinfo=UTC)
        machine_id = self.create()
        a = self.instance(begin, begin + timedelta(hours=2), machine_id=machine_id)
        b = self.instance(begin + timedelta(hours=3), machine_id=machine_id)
        self.price(begin, "0.10")
        self.price(begin + timedelta(hours=1), "0.20")
        self.price(begin, "0.40", "ON_DEMAND", None)
        result = estimate_costs(
            self.db, [a, b], now=begin + timedelta(hours=4), start=begin, daily=True
        )
        self.assertEqual(result["total"]["runtime_hours"], "3.000000")
        self.assertEqual(result["total"]["spot_cost"], "0.500000")
        self.assertEqual(result["total"]["on_demand_cost"], "1.200000")
        self.assertEqual(result["total"]["savings"], "0.700000")
        self.assertEqual(
            [d["spot_cost"] for d in result["daily"]], ["0.100000", "0.400000"]
        )
        self.assertEqual(result["total"]["spot_price_per_hour"], "0.200000")
        self.assertEqual(result["machines"][machine_id]["spot_cost"], "0.500000")

    def test_missing_history_is_null_no_future_or_cross_zone_backfill(self):
        begin = datetime(2026, 2, 1, tzinfo=UTC)
        row = self.instance(begin, begin + timedelta(hours=2))
        self.price(begin, "0.01", zone="ap-southeast-1b")
        self.price(begin + timedelta(hours=1), "0.2")
        self.price(begin + timedelta(hours=3), "0.4", "ON_DEMAND", None)
        result = estimate_costs(self.db, [row], now=begin + timedelta(hours=2))
        cost = result["total"]
        self.assertIsNone(cost["spot_cost"])
        self.assertIsNone(cost["on_demand_cost"])
        self.assertIsNone(cost["savings"])
        self.assertEqual(cost["known_spot_cost"], "0.200000")
        self.assertEqual(cost["missing_spot_hours"], "1.000000")
        self.assertEqual(cost["missing_on_demand_hours"], "2.000000")

    def test_regional_fallback_period_clipping_and_zone_precedence(self):
        begin = datetime(2026, 2, 1, tzinfo=UTC)
        row = self.instance(begin, begin + timedelta(hours=5))
        self.price(begin, "0.1", zone=None)
        self.price(begin + timedelta(hours=2), "0.2")
        self.price(begin + timedelta(hours=3), "0.9", zone=None)
        self.price(begin, "1", "ON_DEMAND", None)
        cost = estimate_costs(
            self.db,
            [row],
            now=begin + timedelta(hours=4),
            start=begin + timedelta(hours=1),
        )["total"]
        self.assertEqual(cost["spot_cost"], "0.500000")
        self.assertEqual(cost["on_demand_cost"], "3.000000")

    def test_owner_scopes_counts_detail_and_price_totals(self):
        machine_id = self.provision()
        paths = [
            "/api/dashboard",
            "/api/dashboard/machines",
            "/api/dashboard/options",
            f"/api/dashboard/machines/{machine_id}",
        ]
        for path in paths:
            self.assertEqual(self.client.get(path).status_code, 401)
        overview = self.client.get(paths[0], headers=self.headers()).json()
        self.assertEqual(overview["counts"]["total"], 1)
        other = self.client.get(paths[0], headers=self.headers(self.other)).json()
        self.assertEqual(other["counts"]["total"], 0)
        self.assertEqual(other["cost"]["spot_cost"], "0.000000")
        self.assertEqual(
            self.client.get(paths[-1], headers=self.headers(self.other)).status_code,
            404,
        )
        detail = self.client.get(paths[-1], headers=self.headers()).json()
        self.assertEqual(detail["compute"]["machine_type"], "m6i.xlarge")
        self.assertEqual(len(detail["instances"]), 1)
        self.assertNotIn("adapter_config", json.dumps(detail))
        self.assertNotIn("ssh_public_keys", json.dumps(detail))
        self.assertEqual(
            self.client.get(paths[0] + "?days=0", headers=self.headers()).status_code,
            422,
        )

    def test_checkpoint_metadata_never_returns_embedded_state_or_credentials(self):
        machine_id = self.create()
        job = AgentJob(
            id=str(uuid4()),
            user_id=self.owner.id,
            agent_token_hash="secret-hash",
            total_steps=100,
            checkpoint_every=10,
            heartbeat_every=5,
            checkpoint_data={
                "storage": "s3",
                "bucket": "checkpoints",
                "key": "owner/job.json",
                "secret": "never",
            },
        )
        self.db.add(job)
        self.db.flush()
        self.db.add(MachineJob(machine_id=machine_id, job_id=job.id))
        self.db.commit()
        result = self.client.get(
            f"/api/dashboard/machines/{machine_id}", headers=self.headers()
        ).json()
        self.assertEqual(
            result["job"]["checkpoint"],
            {"storage": "s3", "bucket": "checkpoints", "key": "owner/job.json"},
        )
        self.assertNotIn("secret", json.dumps(result))

    def test_search_pagination_and_literal_wildcard(self):
        self.create(name="alpha%")
        self.create(name="alphabet")
        first = self.client.get(
            "/api/dashboard/machines?limit=1", headers=self.headers()
        ).json()
        second = self.client.get(
            "/api/dashboard/machines?limit=1&offset=1", headers=self.headers()
        ).json()
        self.assertEqual(first["total"], 2)
        self.assertNotEqual(first["items"][0]["id"], second["items"][0]["id"])
        found = self.client.get(
            "/api/dashboard/machines?search=%25", headers=self.headers()
        ).json()
        self.assertEqual(found["total"], 1)
        self.assertEqual(found["items"][0]["name"], "alpha%")

    def test_selection_prioritized_but_recovery_re_ranks(self):
        preference = {
            "machine_type_id": 2,
            "region": "asia-southeast1",
            "zone": "asia-southeast1-b",
        }
        machine_id = self.create(preferred_candidate=preference)
        attempts = list(
            self.db.scalars(select(InstanceAttempt).order_by(InstanceAttempt.position))
        )
        self.assertEqual([a.provider for a in attempts], ["GCP", "AWS"])
        payload = MachineRequest.model_validate(
            self.db.get(MachineLifecycle, machine_id).request_payload
        )
        _prepare_attempts(
            self.db, self.db.get(Machine, machine_id), payload, exclusions={}
        )
        self.db.commit()
        attempts = list(
            self.db.scalars(select(InstanceAttempt).order_by(InstanceAttempt.position))
        )
        self.assertEqual([a.provider for a in attempts], ["GCP", "AWS", "AWS", "GCP"])
        preference["machine_type_id"] = 999
        self.assertEqual(self.submit(preferred_candidate=preference).status_code, 409)

    def test_launch_allowlist_applies_before_limit(self):
        with patch.dict(
            os.environ, {"SPULTI_LAUNCH_CONFIG": json.dumps([PROFILES[1]])}
        ):
            response = self.client.post(
                "/api/placement/recommendations?launchable_only=true",
                headers=self.headers(),
                json={"vcpu": 4, "ram_gib": 16, "limit": 1},
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()[0]["provider"], "GCP")
            machine_id = self.create(limit=1)
            attempt = self.db.scalar(
                select(InstanceAttempt).where(InstanceAttempt.machine_id == machine_id)
            )
            self.assertEqual(attempt.provider, "GCP")

    def test_legacy_request_idempotency(self):
        key = str(uuid4())
        machine_id = self.create(key=key)
        life = self.db.get(MachineLifecycle, machine_id)
        old = dict(life.request_payload)
        old.pop("preferred_candidate")
        life.request_payload = old
        self.db.commit()
        self.assertEqual(self.submit(key=key).json()["id"], machine_id)

    def test_retry_can_replace_an_unavailable_original_preference(self):
        machine_id = self.create(
            preferred_candidate={
                "machine_type_id": 2,
                "region": "asia-southeast1",
                "zone": "asia-southeast1-b",
            }
        )
        for attempt in self.db.scalars(select(InstanceAttempt)):
            attempt.state = "REJECTED"
        self.db.get(MachineLifecycle, machine_id).blocked = True
        self.db.commit()
        with patch.dict(
            os.environ, {"SPULTI_LAUNCH_CONFIG": json.dumps([PROFILES[0]])}
        ):
            response = self.client.post(
                f"/api/machines/{machine_id}/retry", headers=self.headers()
            )
        self.assertEqual(response.status_code, 202, response.text)
        latest = self.db.scalar(
            select(InstanceAttempt).order_by(InstanceAttempt.id.desc())
        )
        self.assertEqual(latest.provider, "AWS")
        self.assertEqual(latest.state, "READY")

    def test_static_csp_cache_and_catalog_without_location(self):
        self.assertEqual(self.client.get("/").status_code, 200)
        response = self.client.get("/dashboard/")
        self.assertIn("script-src 'self'", response.headers["content-security-policy"])
        self.assertIn('lang="vi"', response.text)
        for asset in ("app.js", "ui.js", "styles.css", "mark.svg"):
            self.assertEqual(self.client.get("/dashboard/" + asset).status_code, 200)
        response = self.client.get("/api/dashboard", headers=self.headers())
        self.assertEqual(response.headers["cache-control"], "no-store")
        response = self.client.get(
            "/api/catalog?vcpu=1&ram_gib=1", headers=self.headers()
        )
        self.assertEqual(response.status_code, 200, response.text)

    def test_collector_keeps_spot_and_on_demand_distinct_and_idempotent(self):
        self.db.execute(delete(SpotPrice))
        self.db.commit()
        at = datetime(2026, 1, 1, tzinfo=UTC)
        quotes = [
            SpotPriceQuote(
                "AWS", "m6i.xlarge", "ap-southeast-1", None, Decimal(amount), at, kind
            )
            for kind, amount in [("SPOT", ".1"), ("ON_DEMAND", ".4")]
        ]
        factory = sessionmaker(self.engine)
        with patch("app.services.market_data_collector.SessionLocal", factory):
            self.assertEqual(
                save_market_data(MarketData(prices=quotes, risks=[]))["prices_saved"], 2
            )
            self.assertEqual(
                save_market_data(MarketData(prices=quotes, risks=[]))["prices_saved"], 0
            )
        self.assertEqual(len(list(self.db.scalars(select(SpotPrice)))), 2)


class OnDemandTests(unittest.TestCase):
    @staticmethod
    def product(name, amount, effective="2026-01-01T00:00:00Z", unit="Hrs"):
        return json.dumps(
            {
                "product": {"attributes": {"instanceType": name}},
                "terms": {
                    "OnDemand": {
                        "term": {
                            "effectiveDate": effective,
                            "priceDimensions": {
                                "price": {
                                    "unit": unit,
                                    "beginRange": "0",
                                    "endRange": "Inf",
                                    "pricePerUnit": {"USD": amount},
                                }
                            },
                        }
                    }
                },
            }
        )

    def test_paginated_filtered_linux_prices_and_effective_dates(self):
        client = MagicMock()
        client.get_paginator.return_value.paginate.return_value = [
            {
                "PriceList": [
                    self.product("m6i.xlarge", ".40"),
                    self.product("other", "9"),
                ]
            },
            {
                "PriceList": [
                    self.product("m6i.xlarge", ".50", "2099-01-01T00:00:00Z"),
                    self.product("m6i.xlarge", "99", unit="Quantity"),
                ]
            },
        ]
        quotes = aws_on_demand_prices(
            client,
            "ap-southeast-1",
            {"m6i.xlarge"},
            now=datetime(2026, 2, 1, tzinfo=UTC),
        )
        self.assertEqual(len(quotes), 1)
        self.assertEqual(quotes[0].price_type, "ON_DEMAND")
        self.assertEqual(quotes[0].price_per_hour, Decimal(".40"))
        self.assertIsNone(quotes[0].zone)
        kwargs = client.get_paginator.return_value.paginate.call_args.kwargs
        filters = {f["Field"]: f["Value"] for f in kwargs["Filters"]}
        self.assertEqual(filters["regionCode"], "ap-southeast-1")
        self.assertEqual(filters["operatingSystem"], "Linux")
        self.assertEqual(filters["tenancy"], "Shared")

    def test_ambiguous_prices_fail_instead_of_picking_silently(self):
        client = MagicMock()
        client.get_paginator.return_value.paginate.return_value = [
            {
                "PriceList": [
                    self.product("m6i.xlarge", ".4"),
                    self.product("m6i.xlarge", ".5"),
                ]
            }
        ]
        with self.assertRaisesRegex(ValueError, "Ambiguous"):
            aws_on_demand_prices(client, "ap-southeast-1", {"m6i.xlarge"})


if __name__ == "__main__":
    unittest.main()
