from __future__ import annotations

from fastapi import APIRouter, Query

from app.core.clock import to_storage
from app.emissions.schemas import (
    BatchComputeRequest,
    BatchCorrectRequest,
    BatchReplayRequest,
    CaseRevokeRequest,
    EquipmentFaultRequest,
    FuelReadingRegister,
    FurnaceRunRegister,
    ImpactResolveRequest,
    PurificationRegister,
    RuleRegister,
)
from app.emissions.service import EmissionsBatchService

router = APIRouter(prefix="/api/emissions", tags=["火化排放批次监管"])


def service() -> EmissionsBatchService:
    return EmissionsBatchService()


# ---- 规则版本 ------------------------------------------------------------
@router.post("/rules", status_code=201)
def register_rules(payload: RuleRegister):
    data = payload.model_dump()
    actor = data.pop("actor")
    return service().register_rules(data, actor)


@router.get("/rules")
def list_rules():
    return {"items": service().list_rule_versions()}


# ---- 源数据登记 ----------------------------------------------------------
@router.post("/furnace-runs", status_code=201)
def register_run(payload: FurnaceRunRegister):
    return service().register_run(payload.model_dump(exclude_none=True))


@router.post("/fuel-readings", status_code=201)
def register_reading(payload: FuelReadingRegister):
    return service().register_reading(payload.model_dump(exclude_none=True))


@router.post("/purification-records", status_code=201)
def register_purification(payload: PurificationRegister):
    return service().register_purification(payload.model_dump(exclude_none=True))


# ---- 批次计算 / 更正 / 重放 ----------------------------------------------
@router.post("/batches/compute", status_code=201)
def compute_batch_version(payload: BatchComputeRequest):
    return service().compute(
        payload.period, payload.actor, rule_version=payload.rule_version,
        fixed_clock=to_storage(payload.fixed_clock) if payload.fixed_clock else None,
    )


@router.post("/batches/correct", status_code=201)
def correct_batch(payload: BatchCorrectRequest):
    return service().compute(
        payload.period, payload.actor, rule_version=payload.rule_version,
        corrects_batch_id=payload.corrects_batch_id, reason=payload.reason,
    )


@router.post("/batches/replay", status_code=201)
def replay_batch(payload: BatchReplayRequest):
    return service().compute(
        payload.period, payload.actor, rule_version=payload.rule_version,
        fixed_clock=to_storage(payload.fixed_clock), replay=True,
        corrects_batch_id=payload.corrects_batch_id,
    )


@router.get("/batches")
def list_batches(period: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}$")):
    return {"items": service().list_batches(period)}


@router.get("/batches/{batch_id}")
def get_batch(batch_id: int):
    return service().get_batch(batch_id)


@router.post("/batches/{batch_id}/issue")
def issue_batch(batch_id: int, actor: str = Query(..., min_length=2, max_length=80)):
    return service().issue(batch_id, actor)


# ---- 撤销档案 / 设备故障：影响清单 ----------------------------------------
@router.post("/cases/{case_id}/revoke", status_code=201)
def revoke_case(case_id: int, payload: CaseRevokeRequest):
    return service().revoke_case(case_id, payload.actor, payload.reason)


@router.post("/equipment-faults", status_code=201)
def mark_equipment_fault(payload: EquipmentFaultRequest):
    return service().mark_equipment_fault(payload.model_dump(), payload.actor)


@router.get("/impacts")
def list_impacts(status: str | None = Query(default=None, pattern=r"^(open|resolved)$")):
    return {"items": service().list_impacts(status)}


@router.post("/impacts/{impact_id}/resolve")
def resolve_impact(impact_id: int, payload: ImpactResolveRequest):
    return service().resolve_impact(impact_id, payload.actor, payload.note)


# ---- 监管导出 ------------------------------------------------------------
@router.get("/batches/{batch_id}/export")
def export_batch(batch_id: int):
    return service().export_version(batch_id)
