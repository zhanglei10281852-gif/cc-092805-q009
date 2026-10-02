"""排放批次复算引擎。

引擎是纯函数：给定同一组输入快照与规则版本，必然产生相同的明细、异常和汇总，
不读取当前时间，也不依赖数据库，从而支持固定时钟重放跨月炉次。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from app.core.clock import from_storage

RULES_VERSION = "emission-rules-1"


def digest(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()

ANOMALY_MESSAGES = {
    "run_overlap": "同一火化炉实际运行区间重叠",
    "run_no_reservation": "实际运行找不到对应火化预约",
    "reservation_no_run": "火化预约缺少实际运行区间",
    "reservation_run_mismatch": "实际运行与预约档案或时间不一致",
    "fuel_missing": "运行区间缺少起止燃料读数",
    "fuel_unassignable": "燃料读数无法归属到任何运行区间或仪表",
    "fuel_overlap": "燃料读数在时间上重叠，归属不确定",
    "fuel_decreasing": "燃料止码小于起码",
    "fuel_unmapped_meter": "燃料仪表未配置对应火化炉",
    "purification_missing": "运行区间缺少净化设施状态记录",
    "purification_overlap": "净化状态记录在时间上重叠",
    "purification_fault": "运行区间内净化设施处于故障或旁通状态",
    "revoked_case": "业务档案已撤销，炉次不计入排放合计",
    "cross_month": "炉次跨月，按规则归入起始月",
}


def parse_ts(value: str | None) -> datetime | None:
    return from_storage(value) if value else None


def intervals_overlap(start_a: str, end_a: str, start_b: str, end_b: str) -> bool:
    return start_a < end_b and start_b < end_a


def interval_contains(start: str, end: str, point: str) -> bool:
    return start <= point <= end


def month_key(value: str) -> str:
    return value[:7]


def snapshot_period_runs(runs: list[dict[str, Any]], period: str, cross_month_policy: str) -> dict[str, list[dict[str, Any]]]:
    """按跨月规则把实际炉次分配到归属月份（一炉次只归属一个月，杜绝重复统计）。"""
    buckets: dict[str, list[dict[str, Any]]] = {}
    for run in runs:
        start_month = month_key(run["start_at"])
        end_month = month_key(run["end_at"])
        owner = start_month if cross_month_policy == "start_month" else end_month
        buckets.setdefault(owner, []).append(run)
    return {period: buckets.get(period, [])}


def _meter_for_resource(resource_code: str, meter_map: dict[str, Any]) -> str | None:
    return meter_map.get(resource_code)


def _select_reading(readings: list[dict[str, Any]], target: str, tie_break: str) -> dict[str, Any] | None:
    if not readings:
        return None
    if tie_break == "latest_recorded":
        return max(readings, key=lambda r: (r["reading_at"], r["recorded_at"], r["id"]))
    return min(readings, key=lambda r: (r["reading_at"], r["recorded_at"], r["id"]))


def _purification_efficiency(
    resource_code: str, start_at: str, end_at: str,
    purification: list[dict[str, Any]], state_efficiency: dict[str, float],
    uncovered_efficiency: float,
) -> tuple[float, list[dict[str, Any]], bool, bool]:
    """返回（加权净化效率、命中的净化区间、是否存在覆盖缺口、是否命中故障/旁通）。"""
    total = (parse_ts(end_at) - parse_ts(start_at)).total_seconds()
    if total <= 0:
        return uncovered_efficiency, [], True, False
    relevant = [p for p in purification if p["resource_code"] == resource_code and intervals_overlap(start_at, end_at, p["start_at"], p["end_at"])]
    if not relevant:
        return uncovered_efficiency, [], True, False
    covered = 0.0
    weighted = 0.0
    fault = False
    cursor = parse_ts(start_at)
    end = parse_ts(end_at)
    for record in sorted(relevant, key=lambda p: p["start_at"]):
        rec_start = max(parse_ts(record["start_at"]), cursor)
        rec_end = min(parse_ts(record["end_at"]), end)
        if rec_end <= rec_start:
            continue
        seconds = (rec_end - rec_start).total_seconds()
        weighted += seconds * float(state_efficiency.get(record["state"], uncovered_efficiency))
        covered += seconds
        if record["state"] in {"fault", "bypass"}:
            fault = True
        cursor = max(cursor, rec_end)
    gap = covered < total - 1e-6
    effective_total = total
    if gap:
        weighted += (total - covered) * uncovered_efficiency
    return weighted / effective_total, relevant, gap, fault


def compute_batch(
    period: str,
    snapshot: dict[str, Any],
    rules: dict[str, Any],
    *,
    revoked_case_ids: set[int] | None = None,
    fault_markers: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """核心复算。snapshot 包含 runs/reservations/readings/purification/resources。

    返回 items、anomalies、totals，全部可由输入确定性重放。
    """
    revoked_case_ids = revoked_case_ids or set()
    fault_markers = fault_markers or []
    meter_map: dict[str, Any] = rules.get("meter_resource_map", {})
    factor = float(rules.get("emission_factor", 0.0))
    tie_break = rules.get("fuel_tie_break", "latest_recorded")
    state_efficiency = rules.get("purification_states", {})
    uncovered_efficiency = float(rules.get("uncovered_purification_efficiency", 0.0))
    cross_policy = rules.get("cross_month_policy", "start_month")

    runs = snapshot["runs"]
    reservations = snapshot["reservations"]
    readings = snapshot["readings"]
    purification = snapshot["purification"]

    owner = snapshot_period_runs(runs, period, cross_policy)[period]

    items: list[dict[str, Any]] = []
    anomalies: list[dict[str, Any]] = []

    def add_anomaly(item_key: str | None, code: str, severity: str, refs: dict[str, Any]) -> None:
        anomalies.append({"item_key": item_key, "code": code, "severity": severity,
                          "message": ANOMALY_MESSAGES.get(code, code), "refs": refs})

    # 1) 实际运行区间之间的重叠检测
    for i, run in enumerate(owner):
        for other in owner[i + 1:]:
            if run["resource_code"] == other["resource_code"] and intervals_overlap(run["start_at"], run["end_at"], other["start_at"], other["end_at"]):
                add_anomaly(f"run-{run['id']}", "run_overlap", "error",
                            {"run_ids": [run["id"], other["id"]], "resource_code": run["resource_code"]})
                add_anomaly(f"run-{other['id']}", "run_overlap", "error",
                            {"run_ids": [run["id"], other["id"]], "resource_code": run["resource_code"]})

    # 2) 净化状态记录自身重叠
    for i, pur in enumerate(purification):
        for other in purification[i + 1:]:
            if pur["resource_code"] == other["resource_code"] and intervals_overlap(pur["start_at"], pur["end_at"], other["start_at"], other["end_at"]):
                add_anomaly(None, "purification_overlap", "warning",
                            {"purification_ids": [pur["id"], other["id"]], "resource_code": pur["resource_code"]})

    # 3) 预约与实际运行配对
    runs_matched_reservation: set[int] = set()
    reservation_to_run: dict[int, dict[str, Any]] = {}
    for run in owner:
        candidate = None
        if run.get("reservation_id"):
            candidate = next((r for r in reservations if r["id"] == run["reservation_id"]), None)
        if candidate is None:
            # 用资源 + 时间重叠 + 档案兜底匹配
            for res in reservations:
                if res["resource_code"] != run["resource_code"]:
                    continue
                if not intervals_overlap(run["start_at"], run["end_at"], res["start_at"], res["end_at"]):
                    continue
                if run.get("case_id") and res.get("case_id") and run["case_id"] != res["case_id"]:
                    continue
                candidate = res
                break
        if candidate is None:
            add_anomaly(f"run-{run['id']}", "run_no_reservation", "warning", {"run_id": run["id"]})
        else:
            runs_matched_reservation.add(candidate["id"])
            reservation_to_run[candidate["id"]] = run
            if run.get("case_id") and candidate.get("case_id") and run["case_id"] != candidate["case_id"]:
                add_anomaly(f"run-{run['id']}", "reservation_run_mismatch", "error",
                            {"run_id": run["id"], "reservation_id": candidate["id"]})

    # 4) 燃料读数归属（按仪表映射 + 时间落在运行区间内）
    resource_meters = {code: cfg if isinstance(cfg, list) else [cfg] for code, cfg in meter_map.items()}
    meter_to_resource: dict[str, str] = {}
    for code, meters in resource_meters.items():
        for meter in meters:
            meter_to_resource[meter] = code

    assigned_reading_ids: set[int] = set()
    # 检测同一仪表在同一时刻附近出现重叠读数（重复补录）
    for i, reading in enumerate(readings):
        for other in readings[i + 1:]:
            if reading["meter_code"] == other["meter_code"] and reading["reading_at"] == other["reading_at"]:
                add_anomaly(None, "fuel_overlap", "warning",
                            {"reading_ids": [reading["id"], other["id"]], "meter_code": reading["meter_code"]})

    run_fuel: dict[int, dict[str, Any]] = {}
    for run in owner:
        meters = resource_meters.get(run["resource_code"], [])
        if not meters:
            mapped = _meter_for_resource(run["resource_code"], meter_map)
            meters = [mapped] if mapped else []
        start_candidates: list[dict[str, Any]] = []
        end_candidates: list[dict[str, Any]] = []
        for reading in readings:
            if reading["meter_code"] not in meters:
                continue
            if interval_contains(run["start_at"], run["end_at"], reading["reading_at"]):
                assigned_reading_ids.add(reading["id"])
            if reading["reading_at"] <= run["start_at"]:
                start_candidates.append(reading)
            if reading["reading_at"] >= run["end_at"]:
                end_candidates.append(reading)
        start_reading = _select_reading(start_candidates, run["start_at"], tie_break)
        end_reading = _select_reading(end_candidates, run["end_at"], tie_break)
        if start_reading and end_reading:
            assigned_reading_ids.add(start_reading["id"])
            assigned_reading_ids.add(end_reading["id"])
            run_fuel[run["id"]] = {"start": start_reading, "end": end_reading}
            if end_reading["value"] < start_reading["value"]:
                add_anomaly(f"run-{run['id']}", "fuel_decreasing", "error",
                            {"run_id": run["id"], "start_value": start_reading["value"], "end_value": end_reading["value"]})
        else:
            missing = []
            if not start_reading:
                missing.append("start")
            if not end_reading:
                missing.append("end")
            add_anomaly(f"run-{run['id']}", "fuel_missing", "error",
                        {"run_id": run["id"], "missing": missing, "meters": meters})
            run_fuel[run["id"]] = {"start": start_reading, "end": end_reading}

    # 无法归属的读数：仪表未映射，或时间不落在任何（全量）运行区间
    all_meters = set(meter_to_resource)
    for reading in readings:
        if reading["id"] in assigned_reading_ids:
            continue
        if reading["meter_code"] not in all_meters:
            add_anomaly(None, "fuel_unmapped_meter", "warning",
                        {"reading_id": reading["id"], "meter_code": reading["meter_code"]})
            continue
        target_resource = meter_to_resource[reading["meter_code"]]
        covered = any(
            r["resource_code"] == target_resource and interval_contains(r["start_at"], r["end_at"], reading["reading_at"])
            for r in runs
        )
        if not covered:
            add_anomaly(None, "fuel_unassignable", "warning",
                        {"reading_id": reading["id"], "meter_code": reading["meter_code"], "reading_at": reading["reading_at"]})

    # 5) 构造运行明细
    for run in owner:
        reservation = next((r for r in reservations if reservation_to_run.get(r["id"], {}).get("id") == run["id"]), None)
        fuel = run_fuel.get(run["id"], {})
        start_reading = fuel.get("start")
        end_reading = fuel.get("end")
        fuel_amount = None
        if start_reading and end_reading and end_reading["value"] >= start_reading["value"]:
            fuel_amount = round(end_reading["value"] - start_reading["value"], 6)

        efficiency, pur_hits, pur_gap, pur_fault = _purification_efficiency(
            run["resource_code"], run["start_at"], run["end_at"], purification, state_efficiency, uncovered_efficiency)
        codes: list[str] = []
        if not pur_hits:
            add_anomaly(f"run-{run['id']}", "purification_missing", "error", {"run_id": run["id"]})
            codes.append("purification_missing")
        elif pur_gap:
            add_anomaly(f"run-{run['id']}", "purification_missing", "warning", {"run_id": run["id"], "reason": "partial_coverage"})
            codes.append("purification_missing")
        if pur_fault:
            add_anomaly(f"run-{run['id']}", "purification_fault", "error", {"run_id": run["id"]})
            codes.append("purification_fault")

        revoked = bool(run.get("case_id") and run["case_id"] in revoked_case_ids)
        if revoked:
            add_anomaly(f"run-{run['id']}", "revoked_case", "warning",
                        {"run_id": run["id"], "case_id": run["case_id"]})
            codes.append("revoked_case")
        if month_key(run["start_at"]) != month_key(run["end_at"]):
            codes.append("cross_month")
            add_anomaly(f"run-{run['id']}", "cross_month", "info",
                        {"run_id": run["id"], "start_month": month_key(run["start_at"]), "end_month": month_key(run["end_at"])})

        # 设备故障标记命中
        for marker in fault_markers:
            if marker["resource_code"] == run["resource_code"] and intervals_overlap(run["start_at"], run["end_at"], marker["start_at"], marker["end_at"]):
                codes.append("equipment_fault")

        emission_amount = None
        included = True
        if fuel_amount is not None:
            emission_amount = round(fuel_amount * factor * efficiency, 6)
        if revoked and rules.get("revoked_case_policy") == "exclude":
            included = False
            emission_amount = 0.0 if emission_amount is not None else None

        item = {
            "item_key": f"run-{run['id']}",
            "kind": "run",
            "ref_id": run["id"],
            "run_id": run["id"],
            "reservation_id": reservation["id"] if reservation else run.get("reservation_id"),
            "case_id": run.get("case_id") or (reservation.get("case_id") if reservation else None),
            "resource_code": run["resource_code"],
            "planned_start_at": reservation["start_at"] if reservation else None,
            "planned_end_at": reservation["end_at"] if reservation else None,
            "actual_start_at": run["start_at"],
            "actual_end_at": run["end_at"],
            "fuel_start_value": start_reading["value"] if start_reading else None,
            "fuel_end_value": end_reading["value"] if end_reading else None,
            "fuel_amount": fuel_amount,
            "purification_efficiency": round(efficiency, 6),
            "emission_amount": emission_amount,
            "included": included,
            "revoked": revoked,
            "anomaly_codes": codes,
            "detail": {
                "shift_code": run.get("shift_code", ""),
                "source": run.get("source", "shift-log"),
                "meter_codes": sorted({r["meter_code"] for r in [start_reading, end_reading] if r}),
                "purification_records": [p["id"] for p in pur_hits],
                "fault_marker_ids": [m["id"] for m in fault_markers if m["resource_code"] == run["resource_code"] and intervals_overlap(run["start_at"], run["end_at"], m["start_at"], m["end_at"])],
            },
        }
        items.append(item)

    # 6) 有预约但本期无运行（按预约起始月归属判断）
    for reservation in reservations:
        if reservation["id"] in runs_matched_reservation:
            continue
        if month_key(reservation["start_at"]) != period:
            continue
        add_anomaly(f"reservation-{reservation['id']}", "reservation_no_run", "warning",
                    {"reservation_id": reservation["id"], "resource_code": reservation["resource_code"]})
        items.append({
            "item_key": f"reservation-{reservation['id']}",
            "kind": "reservation",
            "ref_id": reservation["id"],
            "run_id": None,
            "reservation_id": reservation["id"],
            "case_id": reservation.get("case_id"),
            "resource_code": reservation["resource_code"],
            "planned_start_at": reservation["start_at"],
            "planned_end_at": reservation["end_at"],
            "actual_start_at": None,
            "actual_end_at": None,
            "fuel_start_value": None, "fuel_end_value": None, "fuel_amount": None,
            "purification_efficiency": None, "emission_amount": None,
            "included": False, "revoked": False,
            "anomaly_codes": ["reservation_no_run"],
            "detail": {"purpose": reservation.get("purpose", "")},
        })

    included_items = [i for i in items if i["included"] and i["emission_amount"] is not None]
    totals = {
        "period": period,
        "run_count": len(owner),
        "included_run_count": len(included_items),
        "fuel_amount": round(sum(i["fuel_amount"] for i in included_items), 6),
        "emission_amount": round(sum(i["emission_amount"] for i in included_items), 6),
        "anomaly_count": len(anomalies),
        "error_count": sum(1 for a in anomalies if a["severity"] == "error"),
        "revoked_count": sum(1 for i in items if i["revoked"]),
        "cross_month_count": sum(1 for i in items if "cross_month" in i["anomaly_codes"]),
    }
    return {"items": items, "anomalies": anomalies, "totals": totals}
