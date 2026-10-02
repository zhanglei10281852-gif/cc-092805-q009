from __future__ import annotations

from fastapi import APIRouter, Query

from app.emissions.schemas import (
    CalibrationRuleCreate,
    CorrectionCreate,
    CrematorCreate,
    EquipmentFlagCreate,
    FuelReadingCreate,
    PurifierStateCreate,
    RevokeReason,
    RunRecord,
)
from app.emissions.service import EmissionsService

router = APIRouter(prefix="/api/emissions", tags=["emissions"])


@router.post("/cremators", status_code=201)
def create_cremator(payload: CrematorCreate, actor: str = Query(min_length=2, max_length=80)) -> dict:
    return EmissionsService().create_cremator(payload.model_dump(), actor)


@router.get("/cremators")
def list_cremators() -> list[dict]:
    return EmissionsService().list_cremators()


@router.post("/calibration-rules", status_code=201)
def create_rule(payload: CalibrationRuleCreate, actor: str = Query(min_length=2, max_length=80)) -> dict:
    return EmissionsService().create_rule(payload.model_dump(), actor)


@router.get("/calibration-rules")
def list_rules() -> list[dict]:
    return EmissionsService().list_rules()


@router.post("/fuel-readings", status_code=201)
def add_fuel_reading(payload: FuelReadingCreate) -> dict:
    return EmissionsService().add_fuel_reading(payload.model_dump())


@router.post("/purifier-states", status_code=201)
def add_purifier_state(payload: PurifierStateCreate) -> dict:
    return EmissionsService().add_purifier_state(payload.model_dump())


@router.post("/runs", status_code=201)
def record_run(payload: RunRecord) -> dict:
    return EmissionsService().record_run(payload.model_dump())


@router.post("/equipment-flags", status_code=201)
def raise_equipment_flag(payload: EquipmentFlagCreate) -> dict:
    return EmissionsService().raise_equipment_flag(payload.model_dump())


@router.post("/equipment-flags/{flag_id}/resolve")
def resolve_equipment_flag(flag_id: int, actor: str = Query(min_length=2, max_length=80)) -> dict:
    return EmissionsService().resolve_equipment_flag(flag_id, actor)


@router.post("/reports/compute", status_code=201)
def compute_report(
    period_month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    rule_code: str = Query(min_length=2, max_length=40),
    created_by: str = Query(min_length=2, max_length=80),
    fixed_clock: str | None = Query(default=None, description="固定时钟重放，ISO8601"),
) -> dict:
    from datetime import datetime

    replay_clock = datetime.fromisoformat(fixed_clock) if fixed_clock else None
    return EmissionsService().compute_report(period_month, rule_code, created_by, fixed_clock=replay_clock)


@router.post("/reports/corrections", status_code=201)
def create_correction(payload: CorrectionCreate) -> dict:
    return EmissionsService().create_correction(
        payload.period_month, payload.rule_code, payload.created_by, payload.reason,
        fixed_clock=payload.fixed_clock)


@router.post("/reports/{report_id}/issue")
def issue_report(report_id: int, actor: str = Query(min_length=2, max_length=80)) -> dict:
    return EmissionsService().issue_report(report_id, actor)


@router.get("/reports")
def list_reports(period_month: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}$")) -> list[dict]:
    return EmissionsService().list_reports(period_month)


@router.get("/reports/{report_id}")
def get_report(report_id: int) -> dict:
    return EmissionsService().get_report(report_id)


@router.get("/reports/{report_id}/export")
def export_report(report_id: int) -> dict:
    """监管接口：导出某一版本的明细、异常原因、前后差异与输入摘要。"""
    return EmissionsService().export_report(report_id)


@router.post("/fuel-readings/{reading_id}/revoke")
def revoke_fuel_reading(reading_id: int, payload: RevokeReason) -> dict:
    return EmissionsService().revoke_fuel_reading(reading_id, payload.reason, payload.actor)


@router.post("/runs/{run_id}/revoke")
def revoke_run(run_id: int, payload: RevokeReason) -> dict:
    return EmissionsService().revoke_run(run_id, payload.reason, payload.actor)


@router.post("/cases/{case_ref}/revoke")
def revoke_case(case_ref: str, payload: RevokeReason) -> dict:
    return EmissionsService().revoke_case(case_ref, payload.reason, payload.actor)


@router.get("/impacts")
def list_impacts(subject_type: str | None = Query(default=None, pattern=r"^(reading|run|case|flag)$"),
                 subject_id: str | None = Query(default=None, max_length=80)) -> list[dict]:
    return EmissionsService().list_impacts(subject_type, subject_id)
