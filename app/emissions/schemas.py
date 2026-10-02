from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class CrematorCreate(BaseModel):
    code: str = Field(min_length=2, max_length=40, pattern=r"^[A-Za-z0-9_-]+$")
    name: str = Field(min_length=2, max_length=120)
    site_code: str = Field(min_length=2, max_length=40)
    meter_code: str = Field(default="", max_length=40)


class CalibrationRuleCreate(BaseModel):
    code: str = Field(min_length=2, max_length=40, pattern=r"^[A-Za-z0-9_-]+$")
    name: str = Field(min_length=2, max_length=120)
    fuel_factor: float = Field(gt=0, le=10_000)
    purifier_uplift: float = Field(default=0.0, ge=0, le=100)
    meter_tolerance: float = Field(default=0.0, ge=0)
    max_run_minutes: int = Field(default=600, gt=0, le=100_000)
    valid_from: datetime
    valid_to: datetime | None = None


class FuelReadingCreate(BaseModel):
    meter_code: str = Field(min_length=2, max_length=40)
    read_at: datetime
    reading: float = Field(ge=0)
    batch_key: str = Field(default="", max_length=120)
    idempotency_key: str = Field(min_length=8, max_length=120)
    recorded_by: str = Field(default="system", max_length=80)


class PurifierStateCreate(BaseModel):
    cremator_code: str = Field(min_length=2, max_length=40)
    start_at: datetime
    end_at: datetime | None = None
    state: str = Field(pattern=r"^(normal|bypassed|fault)$")
    note: str = Field(default="", max_length=500)
    idempotency_key: str = Field(min_length=8, max_length=120)
    recorded_by: str = Field(default="system", max_length=80)


class RunRecord(BaseModel):
    cremator_code: str = Field(min_length=2, max_length=40)
    case_ref: str | None = Field(default=None, max_length=80)
    reservation_id: int | None = Field(default=None, gt=0)
    actual_start_at: datetime
    actual_end_at: datetime | None = None
    shift_code: str = Field(default="", max_length=40)
    idempotency_key: str = Field(min_length=8, max_length=120)
    recorded_by: str = Field(default="system", max_length=80)


class EquipmentFlagCreate(BaseModel):
    equipment_code: str = Field(min_length=2, max_length=40)
    flag_type: str = Field(pattern=r"^(fault|maintenance)$")
    start_at: datetime
    end_at: datetime | None = None
    reason: str = Field(min_length=2, max_length=500)
    raised_by: str = Field(min_length=2, max_length=80)
    idempotency_key: str = Field(min_length=8, max_length=120)


class CorrectionCreate(BaseModel):
    period_month: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    rule_code: str = Field(min_length=2, max_length=40)
    reason: str = Field(min_length=2, max_length=500)
    fixed_clock: datetime | None = None
    created_by: str = Field(min_length=2, max_length=80)


class RevokeReason(BaseModel):
    reason: str = Field(min_length=2, max_length=500)
    actor: str = Field(min_length=2, max_length=80)
