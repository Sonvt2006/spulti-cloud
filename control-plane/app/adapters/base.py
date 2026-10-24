from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class CreateInstanceSpec:
    machine_id: int
    name: str

    machine_type: str
    image_id: str
    zone: str

    # UUID lưu trước vào DB.
    # Khi retry cùng yêu cầu phải dùng lại giá trị này.
    request_id: str

    disk_size_gb: int = 30
    gpu_type: str | None = None
    gpu_count: int = 0
    startup_script: str = ""

    def __post_init__(self):
        if self.disk_size_gb <= 0:
            raise ValueError("disk_size_gb phải lớn hơn 0")


@dataclass(frozen=True)
class InstanceInfo:
    provider: str

    # AWS: instance ID; GCP: instance name.
    # Dùng giá trị này để gọi get/delete.
    instance_ref: str

    # ID thực tế do cloud cấp.
    provider_instance_id: str

    status: str

    private_ip: str | None = None
    public_ip: str | None = None


class CloudAdapter(ABC):
    def __init__(self, config):
        self.config = config

    @abstractmethod
    def create_instance(self, spec: CreateInstanceSpec) -> InstanceInfo:
        raise NotImplementedError("This method should be implemented by subclasses.")

    @abstractmethod
    def delete_instance(self, instance_ref: str) -> None:
        raise NotImplementedError("This method should be implemented by subclasses.")

    @abstractmethod
    def get_instance(self, instance_ref: str) -> InstanceInfo:
        raise NotImplementedError("This method should be implemented by subclasses.")

    @abstractmethod
    def list_instances(self) -> list[InstanceInfo]:
        raise NotImplementedError("This method should be implemented by subclasses.")
