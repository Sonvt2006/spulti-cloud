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
        ssh_username: str,
        ssh_public_key: str,
    ):
        self.client = compute_v1.InstancesClient()

        self.project_id = project_id
        self.zone = zone

        # Ví dụ:
        # projects/my-project/regions/asia-southeast1/subnetworks/my-subnet
        self.subnetwork = subnetwork

        self.ssh_username = ssh_username
        self.ssh_public_key = ssh_public_key.strip()

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

    def create_instance(
        self,
        spec: CreateInstanceSpec,
    ) -> InstanceInfo:
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
                        value=(f"{self.ssh_username}:" f"{self.ssh_public_key}"),
                    ),
                    compute_v1.Items(
                        key="block-project-ssh-keys",
                        value="true",
                    ),
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
