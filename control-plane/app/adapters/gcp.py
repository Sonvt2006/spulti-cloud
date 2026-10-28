from app.adapters.base import (
    CloudAdapter,
    CreateInstanceSpec,
    InstanceInfo,
)
from google.cloud import compute_v1


class GCPAdapter(CloudAdapter):
    def __init__(
        self,
        project_id: str,
        zone: str,
        subnetwork: str,
    ):
        self.client = compute_v1.InstancesClient()

        self.project_id = project_id
        self.zone = zone

        # Ví dụ:
        # projects/my-project/regions/asia-southeast1/subnetworks/my-subnet
        self.subnetwork = subnetwork

    @staticmethod
    def _to_info(instance) -> InstanceInfo:
        private_ip = None
        public_ip = None

        if instance.network_interfaces:
            interface = instance.network_interfaces[0]
            private_ip = interface.network_i_p or None

            if interface.access_configs:
                public_ip = interface.access_configs[0].nat_i_p or None

        return InstanceInfo(
            provider="GCP",
            instance_ref=instance.name,
            provider_instance_id=str(instance.id),
            status=instance.status,
            private_ip=private_ip,
            public_ip=public_ip,
        )

    def create_instance(self, spec: CreateInstanceSpec) -> InstanceInfo:
        if spec.zone != self.zone:
            raise ValueError("Zone của spec khác zone của adapter")

        instance = compute_v1.Instance(
            name=spec.name,
            machine_type=(f"zones/{self.zone}/machineTypes/{spec.machine_type}"),
            disks=[
                compute_v1.AttachedDisk(
                    boot=True,
                    auto_delete=True,
                    initialize_params=(
                        compute_v1.AttachedDiskInitializeParams(
                            source_image=spec.image_id,
                            disk_size_gb=spec.disk_size_gb,
                            disk_type=(f"zones/{self.zone}/diskTypes/pd-balanced"),
                        )
                    ),
                )
            ],
            network_interfaces=[
                compute_v1.NetworkInterface(
                    subnetwork=self.subnetwork,
                    access_configs=[
                        compute_v1.AccessConfig(
                            name="External NAT",
                            type_="ONE_TO_ONE_NAT",
                        )
                    ],
                )
            ],
            scheduling=compute_v1.Scheduling(
                provisioning_model="SPOT",
                automatic_restart=False,
                on_host_maintenance="TERMINATE",
                instance_termination_action="DELETE",
            ),
            metadata=compute_v1.Metadata(
                items=[
                    compute_v1.Items(
                        key="startup-script",
                        value=spec.startup_script,
                    ),
                    compute_v1.Items(
                        key="ssh-keys",
                        value="\n".join(
                            f"{spec.ssh_username}:{key}" for key in spec.ssh_public_keys
                        ),
                    ),
                    compute_v1.Items(
                        key="block-project-ssh-keys",
                        value="true",
                    ),
                    compute_v1.Items(key="enable-oslogin", value="FALSE"),
                ]
            ),
            labels={
                "spulti-machine-id": str(spec.machine_id),
            },
        )

        if spec.gpu_count < 0:
            raise ValueError("gpu_count không được âm")

        if spec.gpu_count > 0:
            if not spec.gpu_type:
                raise ValueError("Phải có gpu_type khi gpu_count > 0")

            if not spec.machine_type.startswith("n1-"):
                raise ValueError("Đoạn xử lý GPU này chỉ dành cho machine type N1")

            instance.guest_accelerators = [
                compute_v1.AcceleratorConfig(
                    accelerator_type=(
                        f"projects/{self.project_id}"
                        f"/zones/{self.zone}"
                        f"/acceleratorTypes/{spec.gpu_type}"
                    ),
                    accelerator_count=spec.gpu_count,
                )
            ]

        elif spec.gpu_type:
            raise ValueError("Có gpu_type nhưng gpu_count bằng 0")

        operation = self.client.insert(
            request=compute_v1.InsertInstanceRequest(
                project=self.project_id,
                zone=self.zone,
                instance_resource=instance,
                request_id=spec.request_id,
            )
        )

        operation.result(timeout=300)
        return self.get_instance(spec.name)

    def get_instance(self, instance_ref: str) -> InstanceInfo:
        instance = self.client.get(
            project=self.project_id,
            zone=self.zone,
            instance=instance_ref,
        )
        return self._to_info(instance)

    def delete_instance(self, instance_ref: str) -> None:
        operation = self.client.delete(
            project=self.project_id,
            zone=self.zone,
            instance=instance_ref,
        )
        operation.result(timeout=300)

    @staticmethod
    def get_spot_prices(
        project_id: str,
        region: str,
        machine_types: list[str],
        zone: str | None = None,
    ):
        return GCPAdapter.get_market_data(
            project_id, region, machine_types, zone=zone, include_risk=False
        ).prices

    @staticmethod
    def get_interruption_risks(
        project_id: str,
        region: str,
        machine_types: list[str],
        zone: str | None = None,
    ):
        return GCPAdapter.get_market_data(
            project_id,
            region,
            machine_types,
            zone=zone,
            include_risk=True,
            include_price=False,
        ).risks

    @staticmethod
    def get_market_data(
        project_id: str,
        region: str,
        machine_types: list[str],
        zone: str | None = None,
        include_risk: bool = True,
        include_price: bool = True,
    ):
        if zone is not None and include_price:
            raise ValueError("GCP: lấy giá theo Region, lấy rủi ro theo Zone")
        """GCP advice.capacityHistory: USD/hour price + historical preemption."""
        import logging
        from datetime import datetime
        from decimal import Decimal
        from urllib.parse import quote

        import google.auth
        from app.adapters.base import MarketData, SpotPriceQuote, SpotRiskEstimate
        from google.auth.transport.requests import AuthorizedSession

        credentials, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        session = AuthorizedSession(credentials)
        url = (
            "https://compute.googleapis.com/compute/v1/projects/"
            f"{quote(project_id, safe='')}/regions/{quote(region, safe='')}"
            "/advice/capacityHistory"
        )
        prices = []
        risks = []

        def timestamp(value):
            return datetime.fromisoformat(value.replace("Z", "+00:00"))

        for name in machine_types:
            body = {
                "instanceProperties": {
                    "machineType": name,
                    "scheduling": {"provisioningModel": "SPOT"},
                },
                "types": (["PRICE"] if include_price else [])
                + (["PREEMPTION"] if include_risk else []),
            }
            if zone:
                body["locationPolicy"] = {"location": f"zones/{zone}"}

            try:
                response = session.post(url, json=body, timeout=30)
                response.raise_for_status()
                data = response.json()
            except Exception as exc:
                logging.warning(
                    "GCP advice unavailable for %s in %s: %s", name, zone or region, exc
                )
                continue

            if include_price and data.get("priceHistory"):
                latest = max(
                    data["priceHistory"],
                    key=lambda row: row.get("interval", {}).get("startTime", ""),
                )
                money = latest.get("listPrice", {})
                if money.get("currencyCode") == "USD" and latest.get(
                    "interval", {}
                ).get("startTime"):
                    amount = Decimal(str(money.get("units", "0"))) + (
                        Decimal(str(money.get("nanos", 0))) / Decimal(10**9)
                    )
                    if amount >= 0:
                        prices.append(
                            SpotPriceQuote(
                                provider="GCP",
                                machine_type=name,
                                region=region,
                                zone=zone,
                                price_per_hour=amount,
                                effective_at=timestamp(latest["interval"]["startTime"]),
                            )
                        )

            if include_risk and data.get("preemptionHistory"):
                latest = max(
                    data["preemptionHistory"],
                    key=lambda row: row.get("interval", {}).get("endTime", ""),
                )
                interval = latest.get("interval", {})
                rate = Decimal(str(latest["preemptionRate"]))
                if Decimal(0) <= rate <= Decimal(1) and interval.get("endTime"):
                    risks.append(
                        SpotRiskEstimate(
                            provider="GCP",
                            machine_type=name,
                            region=region,
                            zone=zone,
                            source="GCP_CAPACITY_HISTORY",
                            metric="PREEMPTION_RATE_DAILY",
                            rate_min=rate,
                            rate_max=rate,
                            label=None,
                            observed_at=timestamp(interval["endTime"]),
                            period_start=timestamp(interval["startTime"])
                            if interval.get("startTime")
                            else None,
                            period_end=timestamp(interval["endTime"]),
                        )
                    )

        return MarketData(prices=prices, risks=risks)

    @staticmethod
    def available_regions(project_id: str) -> list[str]:
        client = compute_v1.RegionsClient()
        return sorted(r.name for r in client.list(project=project_id))

    @staticmethod
    def available_zones(project_id: str, region: str) -> list[str]:
        client = compute_v1.ZonesClient()
        return sorted(
            z.name
            for z in client.list(project=project_id)
            if z.region.rsplit("/", 1)[-1] == region
        )

    @staticmethod
    def offered_machine_types(project_id: str, zone: str) -> list[str]:
        client = compute_v1.MachineTypesClient()
        return sorted(m.name for m in client.list(project=project_id, zone=zone))

    @staticmethod
    def describe_machine_specs(
        project_id: str,
        zone: str,
        machine_types: list[str],
    ):
        from decimal import Decimal

        from app.services.catalog import MachineSpec

        client = compute_v1.MachineTypesClient()
        names = set(machine_types)
        specs = []

        for m in client.list(project=project_id, zone=zone):
            if m.name not in names:
                continue

            specs.append(
                MachineSpec(
                    provider="GCP",
                    name=m.name,
                    vcpu=m.guest_cpus,
                    ram_gib=Decimal(m.memory_mb) / Decimal(1024),
                    gpu_count=sum(
                        gpu.guest_accelerator_count for gpu in m.accelerators
                    ),
                )
            )

        return specs
