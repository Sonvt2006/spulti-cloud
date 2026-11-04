"""Operator-owned image/network allowlist; never accept cloud credentials via API."""

import os
from typing import Literal, Self

from app.adapters.aws import AWSAdapter
from app.adapters.base import CloudAdapter
from app.adapters.gcp import GCPAdapter
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator


class LaunchProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    provider: Literal["AWS", "GCP"]
    region: str = Field(min_length=1)
    zone: str = Field(min_length=1)
    image_id: str = Field(min_length=1)
    # Explicit allowlist prevents pairing an image with the wrong CPU architecture.
    machine_types: list[str] = Field(min_length=1)
    subnet_id: str | None = None
    security_group_ids: list[str] = Field(default_factory=list)
    project_id: str | None = None
    subnetwork: str | None = None

    @model_validator(mode="after")
    def validate_network(self) -> Self:
        if self.provider == "AWS" and (
            not self.subnet_id or not self.security_group_ids
        ):
            raise ValueError("AWS cần subnet_id và security_group_ids")
        if self.provider == "GCP" and (not self.project_id or not self.subnetwork):
            raise ValueError("GCP cần project_id và subnetwork")
        if any(not name.strip() for name in self.machine_types):
            raise ValueError("machine_types không được chứa tên rỗng")
        return self

    def adapter_config(self) -> dict:
        if self.provider == "AWS":
            return {
                "region": self.region,
                "subnet_id": self.subnet_id,
                "security_group_ids": self.security_group_ids,
            }
        return {
            "project_id": self.project_id,
            "zone": self.zone,
            "subnetwork": self.subnetwork,
        }


def load_launch_profiles() -> list[LaunchProfile]:
    profiles = TypeAdapter(list[LaunchProfile]).validate_json(
        os.getenv("SPULTI_LAUNCH_CONFIG", "[]")
    )
    seen = set()
    for profile in profiles:
        for name in profile.machine_types:
            key = (profile.provider, profile.region, profile.zone, name)
            if key in seen:
                raise ValueError("Cấu hình launch trùng provider/region/zone/type")
            seen.add(key)
    return profiles


def make_adapter(provider: str, config: dict) -> CloudAdapter:
    if provider == "AWS":
        return AWSAdapter(**config)
    if provider == "GCP":
        return GCPAdapter(**config)
    raise ValueError("Nhà cung cấp không được hỗ trợ")
