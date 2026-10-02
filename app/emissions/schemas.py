from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class RuleRegister(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    description: str = Field(default="", max_length=1000)
    rules: dict[str, Any]
    actor: str = Field(min_length=2, max_length=80)


class FurnaceRunRegister(BaseModel):
    resource_code: str = Field(min_length=2, max_length=40)
    case_id: int | None = Field(default=None, gt=0)
    reservation_id: int | None = Field(default=None, gt=0)
    start_at: datetime
    end_at: datetime
    shift_code: str = Field(default="", max_length=40)
    source: str = Field(default="shift-log", max_length=40)
    recorded_at: datetime | None = None
    idempotency_key: str = Field(min_length=8, max_length=120)

    @model_validator(mode="after")
    def validate_interval(self):
        if self.end_at <= self.start_at:
            raise ValueError("实际运行结束时间必须晚于开始时间")
        return self


class FuelReadingRegister(BaseModel):
    meter_code: str = Field(min_length=2, max_length=40)
    reading_at: datetime
    value: float = Field(ge=0)
    note: str = Field(default="", max_length=500)
    recorded_at: datetime | None = None
    idempotency_key: str = Field(min_length=8, max_length=120)


class PurificationRegister(BaseModel):
    resource_code: str = Field(min_length=2, max_length=40)
    start_at: datetime
    end_at: datetime
    state: Literal["normal", "fault", "bypass", "maintenance"]
    recorded_at: datetime | None = None
    idempotency_key: str = Field(min_length=8, max_length=120)

    @model_validator(mode="after")
    def validate_interval(self):
        if self.end_at <= self.start_at:
            raise ValueError("净化状态区间结束时间必须晚于开始时间")
        return self


class BatchComputeRequest(BaseModel):
    period: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    actor: str = Field(min_length=2, max_length=80)
    rule_version: int | None = Field(default=None, gt=0)
    fixed_clock: datetime | None = None


class BatchCorrectRequest(BaseModel):
    period: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    actor: str = Field(min_length=2, max_length=80)
    corrects_batch_id: int = Field(gt=0)
    reason: str = Field(min_length=2, max_length=500)
    rule_version: int | None = Field(default=None, gt=0)


class BatchReplayRequest(BaseModel):
    period: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    actor: str = Field(min_length=2, max_length=80)
    fixed_clock: datetime
    rule_version: int | None = Field(default=None, gt=0)
    corrects_batch_id: int | None = Field(default=None, gt=0)


class CaseRevokeRequest(BaseModel):
    actor: str = Field(min_length=2, max_length=80)
    reason: str = Field(min_length=2, max_length=500)


class EquipmentFaultRequest(BaseModel):
    resource_code: str = Field(min_length=2, max_length=40)
    start_at: datetime
    end_at: datetime
    kind: Literal["furnace_fault", "purifier_fault"]
    note: str = Field(default="", max_length=500)
    actor: str = Field(min_length=2, max_length=80)

    @model_validator(mode="after")
    def validate_interval(self):
        if self.end_at <= self.start_at:
            raise ValueError("故障区间结束时间必须晚于开始时间")
        return self


class ImpactResolveRequest(BaseModel):
    actor: str = Field(min_length=2, max_length=80)
    note: str = Field(min_length=2, max_length=500)
