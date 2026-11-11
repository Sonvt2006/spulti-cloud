"""Import real specifications/offerings for the configured launch allowlist.

Reads cloud catalog APIs and writes catalog tables only; never creates a VM.
"""

import argparse
import json

from app.adapters.aws import AWSAdapter
from app.adapters.gcp import GCPAdapter
from app.database import SessionLocal
from app.services.catalog import upsert_machine_type_offerings, upsert_machine_types
from app.services.launch_config import LaunchProfile, load_launch_profiles
from sqlalchemy.orm import Session


def sync_launch_catalog(db: Session, profiles: list[LaunchProfile]) -> list[dict]:
    if not profiles:
        raise ValueError("Set SPULTI_LAUNCH_CONFIG before synchronizing the catalog")
    results = []
    for profile in profiles:
        names = sorted(set(profile.machine_types))
        if profile.provider == "AWS":
            offered = AWSAdapter.offered_machine_types_in_zone(
                profile.region, profile.zone
            )
        else:
            offered = GCPAdapter.offered_machine_types(profile.project_id, profile.zone)
        missing = set(names) - set(offered)
        if missing:
            raise ValueError(
                f"Types not offered in {profile.provider}/{profile.zone}: "
                f"{', '.join(sorted(missing))}"
            )
        if profile.provider == "AWS":
            specs = AWSAdapter.describe_machine_specs(profile.region, names)
        else:
            specs = GCPAdapter.describe_machine_specs(
                profile.project_id, profile.zone, names
            )
        if {spec.name for spec in specs} != set(names):
            raise ValueError(f"Incomplete specifications for {profile.zone}")
        upsert_machine_types(db, specs)
        offerings = upsert_machine_type_offerings(
            db,
            provider=profile.provider,
            region=profile.region,
            zone=profile.zone,
            machine_names=names,
        )
        results.append(
            {
                "provider": profile.provider,
                "region": profile.region,
                "zone": profile.zone,
                "machine_types": names,
                **offerings,
            }
        )
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=("AWS", "GCP"))
    args = parser.parse_args()
    profiles = load_launch_profiles()
    if args.provider:
        profiles = [
            profile for profile in profiles if profile.provider == args.provider
        ]
    with SessionLocal.begin() as db:
        result = sync_launch_catalog(db, profiles)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
