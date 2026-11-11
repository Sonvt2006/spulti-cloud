"""AWS Linux/shared compute list prices, collected by the worker, never the UI."""

import json
from datetime import UTC, datetime
from decimal import Decimal

import boto3
from app.adapters.base import MarketData, SpotPriceQuote
from app.services.market_data_collector import _known_types, save_market_data
from botocore.config import Config


def aws_on_demand_prices(client, region: str, names: set[str], *, now=None):
    now = now or datetime.now(UTC)
    filters = {
        "productFamily": "Compute Instance",
        "regionCode": region,
        "operatingSystem": "Linux",
        "tenancy": "Shared",
        "preInstalledSw": "NA",
        "capacitystatus": "Used",
    }
    quotes = {}
    if not names:
        return []
    for page in client.get_paginator("get_products").paginate(
        ServiceCode="AmazonEC2",
        Filters=[
            {"Type": "TERM_MATCH", "Field": key, "Value": value}
            for key, value in filters.items()
        ],
        FormatVersion="aws_v1",
    ):
        for encoded in page.get("PriceList", []):
            product = json.loads(encoded)
            attributes = product["product"]["attributes"]
            name = attributes.get("instanceType")
            if name not in names:
                continue
            for term in product.get("terms", {}).get("OnDemand", {}).values():
                effective = datetime.fromisoformat(
                    term["effectiveDate"].replace("Z", "+00:00")
                )
                if effective > now:
                    continue
                for dimension in term.get("priceDimensions", {}).values():
                    if (
                        dimension.get("unit") != "Hrs"
                        or dimension.get("beginRange") != "0"
                        or dimension.get("endRange") != "Inf"
                        or "USD" not in dimension.get("pricePerUnit", {})
                    ):
                        continue
                    price = Decimal(dimension["pricePerUnit"]["USD"])
                    if not price.is_finite() or price < 0:
                        raise ValueError("Invalid AWS list price")
                    previous = quotes.get(name)
                    if (
                        previous
                        and previous.effective_at == effective
                        and previous.price_per_hour != price
                    ):
                        raise ValueError(
                            f"Ambiguous AWS list price for {name} in {region}"
                        )
                    if previous is None or effective > previous.effective_at:
                        quotes[name] = SpotPriceQuote(
                            "AWS", name, region, None, price, effective, "ON_DEMAND"
                        )
    return list(quotes.values())


def collect_aws_on_demand(regions: list[str], limit_types: int | None = None) -> dict:
    names = set(_known_types("AWS", limit_types))
    if not names:
        return {"status": "empty", "prices_saved": 0}
    client = boto3.client(
        "pricing",
        region_name="us-east-1",
        config=Config(
            connect_timeout=5,
            read_timeout=30,
            retries={"mode": "standard", "total_max_attempts": 3},
        ),
    )
    saved = received = 0
    for region in regions:
        prices = aws_on_demand_prices(client, region, names)
        result = save_market_data(MarketData(prices=prices, risks=[]))
        saved += result["prices_saved"]
        received += result["prices_received"]
    return {
        "status": "ok" if received else "empty",
        "prices_saved": saved,
        "prices_received": received,
    }
