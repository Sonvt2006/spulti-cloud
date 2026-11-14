"""GCP Spot history preserves price validity and actual region/zone scope."""

import unittest
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import MagicMock, patch

from app.adapters.gcp import GCPAdapter

REGION = "asia-southeast1"
ZONE = REGION + "-b"
NAME = "e2-standard-4"


def record(start="2026-09-07T00:00:00Z", end=None, *, nanos=40_956_000):
    interval = {"startTime": start}
    if end is not None:
        interval["endTime"] = end
    return {
        "interval": interval,
        "listPrice": {"currencyCode": "USD", "units": "0", "nanos": nanos},
    }


class GCPPriceIntervalTests(unittest.TestCase):
    def fetch(self, history, *, location=None, zone=None, machine_type=NAME):
        payload = {"machineType": machine_type, "priceHistory": history}
        if location is not None:
            payload["location"] = location
        with (
            patch("google.auth.default", return_value=(MagicMock(), None)),
            patch("google.auth.transport.requests.AuthorizedSession") as session,
        ):
            response = session.return_value.post.return_value
            response.json.return_value = payload
            data = GCPAdapter.get_market_data(
                "test-project", REGION, [NAME], zone=zone, include_risk=False
            )
            self.request = session.return_value.post.call_args
        return data.prices

    def test_preserves_expired_open_and_future_intervals_in_chronological_order(self):
        prices = self.fetch(
            [
                record(
                    "2099-01-01T00:00:00Z", "2099-02-01T00:00:00Z", nanos=10_000_000
                ),
                record("2026-09-08T00:00:00Z", nanos=30_000_000),
                record("2026-09-07T00:00:00Z", "2026-09-07T12:00:00Z"),
            ]
        )
        self.assertEqual(len(prices), 3)
        expired, open_ended, future = prices
        self.assertEqual(expired.effective_at, datetime(2026, 9, 7, tzinfo=UTC))
        self.assertEqual(expired.valid_until, datetime(2026, 9, 7, 12, tzinfo=UTC))
        self.assertEqual(expired.price_per_hour, Decimal(".040956"))
        self.assertIsNone(open_ended.valid_until)
        self.assertEqual(future.effective_at.year, 2099)
        self.assertEqual(future.valid_until, datetime(2099, 2, 1, tzinfo=UTC))
        self.assertTrue(all(price.zone is None for price in prices))

    def test_orders_timestamps_as_instants_instead_of_iso_strings(self):
        prices = self.fetch(
            [
                record("2026-11-14T05:00:00Z", "2026-11-14T06:00:00Z"),
                record("2026-11-14T10:00:00+07:00", "2026-11-14T11:00:00+07:00"),
            ]
        )
        self.assertEqual([p.effective_at.hour for p in prices], [3, 5])
        self.assertEqual([p.valid_until.hour for p in prices], [4, 6])
        self.assertTrue(all(p.effective_at.tzinfo == UTC for p in prices))

    def test_skips_missing_start_malformed_or_nonpositive_duration(self):
        missing = record()
        missing["interval"].pop("startTime")
        malformed = [
            missing,
            record("not-a-date"),
            record(123),
            record("2026-09-07T00:00:00"),
            record(end="not-an-end"),
            record(end="2026-09-07T00:00:00Z"),
            record(end="2026-09-06T00:00:00Z"),
        ]
        prices = self.fetch(malformed + [record(end="2026-09-08T00:00:00Z")])
        self.assertEqual(len(prices), 1)
        self.assertEqual(prices[0].valid_until, datetime(2026, 9, 8, tzinfo=UTC))

    def test_rejects_non_usd_negative_and_nonfinite_prices(self):
        non_usd = record()
        non_usd["listPrice"]["currencyCode"] = "VND"
        prices = self.fetch(
            [
                non_usd,
                record(nanos=-1),
                record(nanos="NaN"),
                record(nanos="Infinity"),
                record(nanos="not-money"),
                record(),
            ]
        )
        self.assertEqual(len(prices), 1)
        self.assertEqual(prices[0].price_per_hour, Decimal(".040956"))

    def test_regional_request_preserves_returned_zone_without_promoting_scope(self):
        location = (
            "https://compute.googleapis.com/compute/beta/projects/test-project/zones/"
            + ZONE
        )
        prices = self.fetch([record()], location=location)
        self.assertEqual(len(prices), 1)
        self.assertEqual(prices[0].region, REGION)
        self.assertEqual(prices[0].zone, ZONE)
        self.assertNotIn("locationPolicy", self.request.kwargs["json"])

    def test_explicit_zone_request_uses_policy_and_never_accepts_a_sibling_zone(self):
        prices = self.fetch([record()], location="zones/" + ZONE, zone=ZONE)
        self.assertEqual(len(prices), 1)
        self.assertEqual(prices[0].zone, ZONE)
        self.assertEqual(
            self.request.kwargs["json"]["locationPolicy"], {"location": "zones/" + ZONE}
        )
        self.assertEqual(
            self.fetch([record()], location="zones/" + REGION + "-a", zone=ZONE), []
        )
        self.assertEqual(
            self.fetch([record()], location="regions/" + REGION, zone=ZONE), []
        )

    def test_missing_location_keeps_explicitly_requested_scope(self):
        self.assertIsNone(self.fetch([record()])[0].zone)
        self.assertEqual(self.fetch([record()], zone=ZONE)[0].zone, ZONE)

    def test_other_region_unknown_scope_and_other_type_are_rejected(self):
        for location in (
            "regions/asia-northeast1",
            "zones/asia-northeast1-a",
            "global",
        ):
            with self.subTest(location=location):
                self.assertEqual(self.fetch([record()], location=location), [])
        self.assertEqual(self.fetch([record()], machine_type="n2-standard-4"), [])

    def test_requesting_a_zone_in_another_region_is_rejected_before_auth(self):
        with patch("google.auth.default") as credentials:
            with self.assertRaisesRegex(ValueError, "must belong"):
                GCPAdapter.get_market_data(
                    "test-project", REGION, [NAME], zone="asia-northeast1-a"
                )
            credentials.assert_not_called()


if __name__ == "__main__":
    unittest.main()
