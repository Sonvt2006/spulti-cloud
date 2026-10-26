from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.services.catalog import MachineSpec


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


@dataclass(frozen=True)
class SpotPriceQuote:
    provider: str
    machine_type: str
    region: str
    zone: str | None
    price_per_hour: Decimal
    effective_at: datetime


@dataclass(frozen=True)
class SpotRiskEstimate:
    provider: str
    machine_type: str
    region: str
    zone: str | None
    source: str
    metric: str
    rate_min: Decimal | None
    rate_max: Decimal | None
    label: str | None
    observed_at: datetime
    period_start: datetime | None = None
    period_end: datetime | None = None


@dataclass(frozen=True)
class MarketData:
    prices: list[SpotPriceQuote]
    risks: list[SpotRiskEstimate]


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
    def available_regions(*args, **kwargs) -> list[str]:
        raise NotImplementedError

    @staticmethod
    @abstractmethod
    def available_zones(*args, **kwargs) -> list[str]:
        raise NotImplementedError

    @staticmethod
    @abstractmethod
    def offered_machine_types(*args, **kwargs) -> list[str]:
        raise NotImplementedError

    @staticmethod
    @abstractmethod
    def describe_machine_specs(*args, **kwargs) -> list["MachineSpec"]:
        raise NotImplementedError

    @staticmethod
    @abstractmethod
    def get_spot_prices(*args, **kwargs) -> list[SpotPriceQuote]:
        raise NotImplementedError

    @staticmethod
    @abstractmethod
    def get_interruption_risks(*args, **kwargs) -> list[SpotRiskEstimate]:
        raise NotImplementedError

    @staticmethod
    @abstractmethod
    def get_market_data(*args, **kwargs) -> MarketData:
        raise NotImplementedError
