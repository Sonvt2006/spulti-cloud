"""AWS and GCP Linux compute list prices, collected by the worker, never the UI."""

import json
import logging
import re
from datetime import UTC, datetime
from decimal import Decimal

import boto3
import google.auth
from app.adapters.base import MarketData, SpotPriceQuote
from app.database import SessionLocal
from app.models import MachineType
from app.services.catalog import MachineSpec
from app.services.market_data_collector import _known_types, save_market_data
from botocore.config import Config
from google.auth.transport.requests import AuthorizedSession
from sqlalchemy import select

logger = logging.getLogger(__name__)
CATALOG_URL = "https://cloudbilling.googleapis.com/v1/services/6F81-5844-456A/skus"
_E2_RESOURCE = re.compile(r"^E2 Instance (Core|Ram) running in .+$", re.IGNORECASE)
_SHARED_CPUS = {
    "e2-micro": Decimal("0.25"),
    "e2-small": Decimal("0.5"),
    "e2-medium": Decimal("1"),
}


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


def _supported(spec: MachineSpec) -> bool:
    return (
        spec.provider == "GCP"
        and spec.gpu_count == 0
        and (
            spec.name in _SHARED_CPUS
            or re.fullmatch(r"e2-(standard|highmem|highcpu)-[1-9][0-9]*", spec.name)
            is not None
        )
    )


def _component(sku: dict) -> str | None:
    category = sku.get("category", {})
    if (
        category.get("resourceFamily") != "Compute"
        or category.get("usageType") != "OnDemand"
    ):
        return None
    match = _E2_RESOURCE.fullmatch(sku.get("description", ""))
    return match[1].lower() if match else None


def _latest_rate(sku: dict, component: str, now: datetime):
    versions = []
    for info in sku.get("pricingInfo", []):
        effective = datetime.fromisoformat(info["effectiveTime"].replace("Z", "+00:00"))
        if effective <= now:
            versions.append((effective, info))
    if not versions:
        return None
    effective, info = max(versions, key=lambda item: item[0])
    expression = info.get("pricingExpression", {})
    tiers = expression.get("tieredRates", [])
    # These compute SKUs must have a single, unconditional hourly USD rate.
    if (
        expression.get("usageUnit") != ("h" if component == "core" else "GiBy.h")
        or len(tiers) != 1
        or Decimal(str(tiers[0].get("startUsageAmount", 0))) != 0
    ):
        return None
    money = tiers[0].get("unitPrice", {})
    if money.get("currencyCode") != "USD":
        return None
    price = Decimal(str(money.get("units", "0"))) + (
        Decimal(str(money.get("nanos", 0))) / Decimal(10**9)
    )
    if not price.is_finite() or price <= 0:
        raise ValueError("Invalid GCP list price")
    # displayQuantity only affects presentation, never the unit price.
    return effective, price


def gcp_on_demand_prices(
    skus: list[dict], region: str, specs: list[MachineSpec], *, now=None
) -> list[SpotPriceQuote]:
    """Compose CPU + RAM rates; shared E2 uses fractional, not guest, vCPUs.

    Only predefined E2 is supported. Custom, Spot, commitments, licenses and
    free-tier discount SKUs must never contribute to an On-demand comparison.
    """
    now = now or datetime.now(UTC)
    rates = {}
    for sku in skus:
        component = _component(sku)
        if component is None or region not in sku.get("serviceRegions", []):
            continue
        rate = _latest_rate(sku, component, now)
        if rate is None:
            continue
        previous = rates.get(component)
        if previous and previous[0] == rate[0] and previous[1] != rate[1]:
            raise ValueError(f"Ambiguous GCP {component} list price in {region}")
        if previous is None or rate[0] > previous[0]:
            rates[component] = rate
    if "core" not in rates or "ram" not in rates:
        return []
    core_at, core = rates["core"]
    ram_at, ram = rates["ram"]
    quotes = []
    for spec in specs:
        if not _supported(spec):
            continue
        cpus = _SHARED_CPUS.get(spec.name, Decimal(spec.vcpu))
        price = cpus * core + spec.ram_gib * ram
        quotes.append(
            SpotPriceQuote(
                "GCP", spec.name, region, None, price, max(core_at, ram_at), "ON_DEMAND"
            )
        )
    return quotes


def _catalog_skus(session) -> list[dict]:
    """Read the catalog once for all requested regions, retaining only E2 SKUs."""
    params = {"currencyCode": "USD", "pageSize": 5000}
    skus = []
    while True:
        response = session.get(CATALOG_URL, params=dict(params), timeout=60)
        response.raise_for_status()
        page = response.json()
        skus.extend(sku for sku in page.get("skus", []) if _component(sku))
        token = page.get("nextPageToken")
        if not token:
            return skus
        params["pageToken"] = token


def collect_gcp_on_demand(regions: list[str], limit_types: int | None = None) -> dict:
    with SessionLocal() as db:
        specs = [
            MachineSpec("GCP", m.name, m.vcpu, m.ram_gib, m.gpu_count)
            for m in db.scalars(
                select(MachineType)
                .where(MachineType.provider == "GCP", MachineType.name.like("e2-%"))
                .order_by(MachineType.name)
            )
        ]
    specs = [spec for spec in specs if _supported(spec)]
    if limit_types is not None:
        specs = specs[:limit_types]
    regions = sorted(set(regions))
    if not specs or not regions:
        return {"status": "empty", "prices_saved": 0, "prices_received": 0}

    credentials, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    with AuthorizedSession(credentials) as session:
        skus = _catalog_skus(session)
    now = datetime.now(UTC)
    # Validate all regions before writing any prices, including ambiguous SKUs.
    prices = [
        price
        for region in regions
        for price in gcp_on_demand_prices(skus, region, specs, now=now)
    ]
    outcome = save_market_data(MarketData(prices=prices, risks=[]))
    missing = len(specs) * len(regions) - len(prices)
    if missing:
        logger.warning("GCP On-demand: missing %d E2 regional prices", missing)
    return {
        "status": "empty" if not prices else "partial" if missing else "ok",
        "prices_saved": outcome["prices_saved"],
        "prices_received": outcome["prices_received"],
        "prices_missing": missing,
    }
