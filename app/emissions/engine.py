"""排放批次计算引擎（纯函数）。

引擎不访问数据库、不读取墙钟：所有输入由服务层快照后传入，
因此同一输入快照 + 同一规则版本必然得到同一结果，可复算、可重放。

燃料表按累计读数处理：炉次区间内的燃料消耗用起止时刻的线性插值
估算，跨月炉次因此能按月界切分为多个批次。
"""
from __future__ import annotations

from bisect import bisect_right
from datetime import datetime, timedelta, timezone
from typing import Any

# 预约与实际区间允许的偏差（秒）
RESERVATION_START_TOLERANCE_SECONDS = 30 * 60
RESERVATION_END_TOLERANCE_SECONDS = 60 * 60
# 炉次边界外可作为插值端点的读数最大间隔（分钟）；
# 规则未给 meter_tolerance 时使用该默认值
DEFAULT_METER_GAP_MINUTES = 120

ANOMALY_CODES = {
    "run_missing_end": "炉次缺少实际结束时间，无法结算",
    "run_overlap": "同一火化炉运行区间重叠",
    "run_too_long": "炉次运行时长超过校准规则上限",
    "run_cross_month": "炉次跨月，已按月界切分",
    "reservation_missing": "炉次找不到火化预约",
    "reservation_mismatch": "预约区间与实际运行区间偏差超限",
    "purifier_missing": "运行区间缺少净化设施状态记录",
    "purifier_fault": "净化设施故障期间运行",
    "purifier_bypassed": "净化设施旁通期间运行",
    "purifier_overlap": "净化设施状态区间相互重叠",
    "calibration_missing": "找不到适用的校准规则",
    "reading_missing": "运行区间缺少可结算的燃料读数",
    "reading_non_monotonic": "燃料累计读数倒转",
    "reading_disputed": "读数落入多个重叠炉次，归属存在争议",
    "equipment_flag": "运行区间与设备故障/维保标记重叠",
    "reading_unattributed": "燃料读数无法归属到任何炉次",
}


def parse_dt(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def month_bounds(period_month: str) -> tuple[datetime, datetime]:
    year, month = (int(part) for part in period_month.split("-"))
    start = datetime(year, month, 1, tzinfo=timezone.utc)
    end = datetime(year + 1, 1, 1, tzinfo=timezone.utc) if month == 12 else datetime(year, month + 1, 1, tzinfo=timezone.utc)
    return start, end


def overlap_seconds(a_start: datetime, a_end: datetime,
                    b_start: datetime, b_end: datetime) -> float:
    return max(0.0, (min(a_end, b_end) - max(a_start, b_start)).total_seconds())


def intervals_overlap(a_start: datetime, a_end: datetime,
                      b_start: datetime, b_end: datetime) -> bool:
    return overlap_seconds(a_start, a_end, b_start, b_end) > 0


def pick_rule(rules: list[dict[str, Any]], rule_code: str, at: datetime) -> dict[str, Any] | None:
    """选择 at 时刻有效的指定代码规则；同一代码取 id 最大（最新）版本。"""
    valid = []
    for rule in rules:
        if rule["code"] != rule_code:
            continue
        valid_from = parse_dt(rule["valid_from"])
        valid_to = parse_dt(rule["valid_to"]) if rule.get("valid_to") else None
        if valid_from <= at and (valid_to is None or valid_to > at):
            valid.append(rule)
    return max(valid, key=lambda r: int(r["id"])) if valid else None


def _anomaly(code: str, detail: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"code": code, "message": ANOMALY_CODES[code], "detail": detail or {}}


class MeterSeries:
    """某块燃料表的累计读数序列与区间结算。"""

    def __init__(self, readings: list[dict[str, Any]]) -> None:
        ordered = sorted(readings, key=lambda r: (r["read_at"], r["id"]))
        self.points = [(parse_dt(r["read_at"]), float(r["reading"]), r["id"]) for r in ordered]
        self.times = [p[0] for p in self.points]
        self.decreasing_ids = {
            self.points[i][2]
            for i in range(1, len(self.points))
            if self.points[i][1] < self.points[i - 1][1]
        }

    def value_at(self, at: datetime, max_gap_minutes: float) -> tuple[float | None, list[int]]:
        """返回插值累计值与用到的读数 id；无读数或端点超出表具容差时为 None。"""
        if not self.points:
            return None, []
        pos = bisect_right(self.times, at)
        if pos == 0:
            return None, []
        left = self.points[pos - 1]
        if left[0] == at:
            return left[1], [left[2]]
        if pos == len(self.points):
            return None, []
        right = self.points[pos]
        gap = timedelta(minutes=max_gap_minutes)
        # 端点离炉次边界过远时不插值：该读数不能用于本炉次结算
        if at - left[0] > gap or right[0] - at > gap:
            return None, []
        span = (right[0] - left[0]).total_seconds()
        ratio = (at - left[0]).total_seconds() / span if span else 0.0
        return left[1] + (right[1] - left[1]) * ratio, [left[2], right[2]]

    def usage(self, start: datetime, end: datetime,
              max_gap_minutes: float = DEFAULT_METER_GAP_MINUTES) -> tuple[float | None, list[int]]:
        start_value, start_ids = self.value_at(start, max_gap_minutes)
        end_value, end_ids = self.value_at(end, max_gap_minutes)
        if start_value is None or end_value is None:
            return None, sorted(set(start_ids + end_ids))
        return round(end_value - start_value, 3), sorted(set(start_ids + end_ids))

    def readings_between(self, start: datetime, end: datetime) -> list[int]:
        return [pid for t, _v, pid in self.points if start <= t <= end]


def _worst_purifier(piece_start: datetime, piece_end: datetime,
                    windows: list[dict[str, Any]]) -> tuple[str, list[int], bool]:
    """返回最差净化状态、命中的状态记录 id，以及状态区间本身是否重叠。"""
    order = {"normal": 0, "bypassed": 1, "fault": 2}
    touched: list[int] = []
    worst = ""
    covered: list[tuple[datetime, datetime]] = []
    for window in windows:
        w_start = parse_dt(window["start_at"])
        # 开口状态区间（尚无结束时间）视为持续有效，覆盖到当前炉次段末尾
        w_end = parse_dt(window["end_at"]) if window.get("end_at") else piece_end
        if overlap_seconds(piece_start, piece_end, w_start, w_end) > 0:
            touched.append(window["id"])
            if not worst or order[window["state"]] > order[worst]:
                worst = window["state"]
            covered.append((max(w_start, piece_start), min(w_end, piece_end)))
    state_overlap = any(
        intervals_overlap(a_start, a_end, b_start, b_end)
        for i, (a_start, a_end) in enumerate(covered)
        for b_start, b_end in covered[i + 1:]
    )
    return worst, touched, state_overlap


def compute_period(snapshot: dict[str, Any], rule_code: str) -> dict[str, Any]:
    """按月计算排放批次。

    snapshot 键：period_month / cremators / runs / readings / purifier /
    reservations / flags / rules，时间均为 UTC ISO 字符串；只含当前有效
    （未撤销）记录，使撤销在重算时表现为读数/炉次的移除而非静默改数。
    """
    period_month = snapshot["period_month"]
    month_start, month_end = month_bounds(period_month)
    rules = snapshot.get("rules", [])

    cremators = {c["code"]: c for c in snapshot.get("cremators", [])}
    meter_of_cremator = {code: c.get("meter_code") for code, c in cremators.items()}
    cremator_of_meter = {c.get("meter_code"): code for code, c in cremators.items() if c.get("meter_code")}

    runs = sorted(snapshot.get("runs", []), key=lambda r: (r["actual_start_at"], r["id"]))
    reservations = {r["id"]: r for r in snapshot.get("reservations", [])}
    flags = snapshot.get("flags", [])

    series_by_meter = {meter: MeterSeries(items)
                       for meter, items in _group(snapshot.get("readings", []), "meter_code").items()}

    purifier_by_cremator = _group(snapshot.get("purifier", []), "cremator_code")

    # 同炉运行区间重叠
    overlaps: dict[int, list[int]] = {}
    for code, indices in _group_indices(runs, "cremator_code").items():
        del code
        for pos, index in enumerate(indices):
            a = runs[index]
            a_start = parse_dt(a["actual_start_at"])
            a_end = parse_dt(a["actual_end_at"]) if a.get("actual_end_at") else a_start + timedelta(seconds=1)
            peers = []
            for other_pos, other_index in enumerate(indices):
                if other_pos == pos:
                    continue
                b = runs[other_index]
                b_start = parse_dt(b["actual_start_at"])
                b_end = parse_dt(b["actual_end_at"]) if b.get("actual_end_at") else b_start + timedelta(seconds=1)
                if intervals_overlap(a_start, a_end, b_start, b_end):
                    peers.append(other_index)
            if peers:
                overlaps[index] = peers

    batches: list[dict[str, Any]] = []
    # 被炉次使用（区间内或作为插值端点）的读数，用于反查无法归属读数
    consumed_reading_ids: set[int] = set()
    open_ended_runs: list[dict[str, Any]] = []
    closed_runs_in_month = 0

    for index, run in enumerate(runs):
        run_start = parse_dt(run["actual_start_at"])
        raw_end = parse_dt(run["actual_end_at"]) if run.get("actual_end_at") else None
        if raw_end is None:
            open_ended_runs.append(run)
            continue
        run_end = raw_end
        # 只处理与本月相交的炉次
        if run_end <= month_start or run_start >= month_end:
            continue
        closed_runs_in_month += 1

        meter_code = meter_of_cremator.get(run["cremator_code"])
        series = series_by_meter.get(meter_code) if meter_code else None

        rule = pick_rule(rules, rule_code, run_end)

        reservation = reservations.get(run["reservation_id"]) if run.get("reservation_id") else None
        reservation_window: dict[str, Any] = {}
        reservation_anomaly = None
        if not run.get("reservation_id"):
            reservation_anomaly = _anomaly("reservation_missing", {"run_id": run["id"], "reason": "炉次未关联预约"})
        elif reservation is None:
            reservation_anomaly = _anomaly("reservation_missing", {"reservation_id": run["reservation_id"]})
        else:
            reservation_window = {"start_at": reservation["start_at"], "end_at": reservation["end_at"],
                                  "resource_code": reservation.get("resource_code")}

        # 月界切分
        pieces = [(max(run_start, month_start), min(run_end, month_end))]
        cross_month = run_start < month_start or run_end > month_end

        for piece_start, piece_end in pieces:
            anomalies: list[dict[str, Any]] = []

            if index in overlaps:
                anomalies.append(_anomaly("run_overlap", {
                    "run_id": run["id"],
                    "overlap_run_ids": [runs[peer]["id"] for peer in overlaps[index]]}))
            if cross_month:
                anomalies.append(_anomaly("run_cross_month", {
                    "run_id": run["id"], "actual_start_at": iso(run_start), "actual_end_at": iso(run_end)}))
            if rule is None:
                anomalies.append(_anomaly("calibration_missing", {
                    "run_id": run["id"], "rule_code": rule_code, "at": iso(piece_end)}))
            elif (run_end - run_start).total_seconds() > int(rule["max_run_minutes"]) * 60:
                anomalies.append(_anomaly("run_too_long", {
                    "run_id": run["id"], "max_run_minutes": int(rule["max_run_minutes"]),
                    "actual_minutes": round((run_end - run_start).total_seconds() / 60, 1)}))

            if reservation_anomaly is not None:
                anomalies.append(reservation_anomaly)
            elif reservation is not None:
                res_start = parse_dt(reservation["start_at"])
                res_end = parse_dt(reservation["end_at"])
                resource_ok = not reservation.get("resource_code") or reservation["resource_code"] == run["cremator_code"]
                if (not resource_ok
                        or abs((run_start - res_start).total_seconds()) > RESERVATION_START_TOLERANCE_SECONDS
                        or abs((run_end - res_end).total_seconds()) > RESERVATION_END_TOLERANCE_SECONDS):
                    anomalies.append(_anomaly("reservation_mismatch", {
                        "run_id": run["id"], "reservation_window": reservation_window,
                        "actual_window": {"start_at": iso(run_start), "end_at": iso(run_end)}}))

            windows = purifier_by_cremator.get(run["cremator_code"], [])
            state, touched, state_overlap = _worst_purifier(piece_start, piece_end, windows)
            if state_overlap:
                anomalies.append(_anomaly("purifier_overlap", {"run_id": run["id"], "state_ids": touched}))
            if not touched:
                anomalies.append(_anomaly("purifier_missing", {"run_id": run["id"]}))
            elif state == "fault":
                anomalies.append(_anomaly("purifier_fault", {"run_id": run["id"], "state_ids": touched}))
            elif state == "bypassed":
                anomalies.append(_anomaly("purifier_bypassed", {"run_id": run["id"], "state_ids": touched}))

            for flag in flags:
                f_start = parse_dt(flag["start_at"])
                f_end = parse_dt(flag["end_at"]) if flag.get("end_at") else f_start + timedelta(seconds=1)
                if flag["equipment_code"] == run["cremator_code"] and overlap_seconds(piece_start, piece_end, f_start, f_end) > 0:
                    anomalies.append(_anomaly("equipment_flag", {
                        "run_id": run["id"], "flag_id": flag["id"],
                        "flag_type": flag["flag_type"], "reason": flag["reason"]}))

            fuel_used: float | None = None
            used_reading_ids: list[int] = []
            allocation: list[dict[str, Any]] = []
            gap_minutes = float(rule["meter_tolerance"]) if rule and rule.get("meter_tolerance") \
                else DEFAULT_METER_GAP_MINUTES
            if series is None:
                anomalies.append(_anomaly("reading_missing", {"run_id": run["id"], "reason": "火化炉未绑定燃料表"}))
            else:
                fuel_used, used_reading_ids = series.usage(piece_start, piece_end, gap_minutes)
                # 区间内读数与插值端点都视为已归属：端点读数支撑了该炉次结算，
                # 不应再被标为无法归属
                consumed_reading_ids.update(used_reading_ids)
                consumed_reading_ids.update(series.readings_between(piece_start, piece_end))
                allocation = [_allocation_item(series, rid) for rid in used_reading_ids]
                if fuel_used is None:
                    anomalies.append(_anomaly("reading_missing", {
                        "run_id": run["id"], "piece_start_at": iso(piece_start), "piece_end_at": iso(piece_end)}))
                elif fuel_used < 0:
                    anomalies.append(_anomaly("reading_non_monotonic", {
                        "run_id": run["id"], "reading_ids": used_reading_ids, "fuel_used": fuel_used}))
                # 读数严格落在多个重叠炉次内
                disputed = _disputed_readings(series, piece_start, piece_end, runs, index, meter_code, meter_of_cremator)
                for rid in disputed:
                    anomalies.append(_anomaly("reading_disputed", {"reading_id": rid, "run_id": run["id"]}))
                for rid in used_reading_ids:
                    if rid in series.decreasing_ids:
                        anomalies.append(_anomaly("reading_non_monotonic", {"reading_id": rid}))

            factor = float(rule["fuel_factor"]) if rule else None
            uplift = float(rule["purifier_uplift"]) if rule else 0.0
            if fuel_used is None or factor is None:
                emission = 0.0
            else:
                multiplier = 1.0 + uplift if state in {"fault", "bypassed"} else 1.0
                emission = round(max(0.0, fuel_used) * factor * multiplier, 3)

            batches.append({
                "batch_key": f"run:{run['id']}:{period_month}",
                "cremator_code": run["cremator_code"],
                "meter_code": meter_code,
                "run_id": run["id"],
                "case_ref": run.get("case_ref"),
                "month": period_month,
                "reservation_window": reservation_window,
                "actual_window": {"start_at": iso(piece_start), "end_at": iso(piece_end),
                                  "run_start_at": iso(run_start), "run_end_at": iso(run_end)},
                "fuel_allocation": allocation,
                "purifier_state": state,
                "calibration_code": rule["code"] if rule else "",
                "calibration_rule_id": rule["id"] if rule else None,
                "fuel_used": round(fuel_used or 0.0, 3),
                "estimated_emission": emission,
                "anomalies": anomalies,
            })

    # 无法归属的有效读数：时刻在本月但没有进入任何炉次；
    # 未绑定火化炉的表同样列出，原因中注明未绑定
    unattributed: list[dict[str, Any]] = []
    unattributed_unbound: list[dict[str, Any]] = []
    for meter_code, series in series_by_meter.items():
        bound_cremator = cremator_of_meter.get(meter_code)
        for t, _value, rid in series.points:
            if month_start <= t < month_end and rid not in consumed_reading_ids:
                source = next(r for r in snapshot["readings"] if r["id"] == rid)
                if bound_cremator:
                    unattributed.append(source)
                else:
                    unattributed_unbound.append(source)

    for meter_code, items in _group(unattributed, "meter_code").items():
        reading_ids = sorted(r["id"] for r in items)
        batches.append({
            "batch_key": f"unattributed:{meter_code}:{period_month}",
            "cremator_code": cremator_of_meter.get(meter_code, ""),
            "meter_code": meter_code,
            "run_id": None,
            "case_ref": None,
            "month": period_month,
            "reservation_window": {},
            "actual_window": {},
            "fuel_allocation": [_allocation_item(series_by_meter[meter_code], r["id"]) for r in
                                sorted(items, key=lambda x: x["id"])],
            "purifier_state": "",
            "calibration_code": "",
            "calibration_rule_id": None,
            "fuel_used": 0.0,
            "estimated_emission": 0.0,
            "anomalies": [_anomaly("reading_unattributed", {"meter_code": meter_code, "reading_ids": reading_ids})],
        })

    for meter_code, items in _group(unattributed_unbound, "meter_code").items():
        reading_ids = sorted(r["id"] for r in items)
        batches.append({
            "batch_key": f"unattributed:{meter_code}:{period_month}",
            "cremator_code": "",
            "meter_code": meter_code,
            "run_id": None,
            "case_ref": None,
            "month": period_month,
            "reservation_window": {},
            "actual_window": {},
            "fuel_allocation": [_allocation_item(series_by_meter[meter_code], r["id"]) for r in
                                sorted(items, key=lambda x: x["id"])],
            "purifier_state": "",
            "calibration_code": "",
            "calibration_rule_id": None,
            "fuel_used": 0.0,
            "estimated_emission": 0.0,
            "anomalies": [_anomaly("reading_unattributed", {
                "meter_code": meter_code, "reading_ids": reading_ids,
                "reason": "燃料表未绑定任何在册火化炉"})],
        })

    anomaly_records = [a for b in batches for a in b["anomalies"]]
    open_end_anomalies = [_anomaly("run_missing_end", {"run_id": r["id"], "cremator_code": r["cremator_code"]})
                          for r in open_ended_runs
                          if parse_dt(r["actual_start_at"]) < month_end]
    anomaly_records.extend(open_end_anomalies)

    all_unattributed = unattributed + unattributed_unbound
    total_fuel = round(sum(b["fuel_used"] for b in batches), 3)
    total_emission = round(sum(b["estimated_emission"] for b in batches), 3)
    anomaly_codes = sorted({a["code"] for a in anomaly_records})
    return {
        "period_month": period_month,
        "rule_code": rule_code,
        "batches": sorted(batches, key=lambda b: b["batch_key"]),
        "unattributed": [{
            "reading_id": r["id"], "meter_code": r["meter_code"],
            "read_at": r["read_at"], "reading": r["reading"],
            "reason": ANOMALY_CODES["reading_unattributed"],
        } for r in sorted(all_unattributed, key=lambda r: (r["read_at"], r["id"]))],
        "open_ended_run_ids": [r["id"] for r in open_ended_runs],
        "totals": {
            "run_count": closed_runs_in_month,
            "batch_count": len(batches),
            "fuel_used": total_fuel,
            "estimated_emission": total_emission,
            "anomaly_count": len(anomaly_records),
            "anomaly_codes": anomaly_codes,
            "unattributed_count": len(all_unattributed),
        },
    }


def _group(items: list[dict[str, Any]], key: str) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        result.setdefault(item[key], []).append(item)
    return result


def _group_indices(items: list[dict[str, Any]], key: str) -> dict[str, list[int]]:
    result: dict[str, list[int]] = {}
    for index, item in enumerate(items):
        result.setdefault(item[key], []).append(index)
    return result


def _allocation_item(series: MeterSeries, reading_id: int) -> dict[str, Any]:
    point = next(p for p in series.points if p[2] == reading_id)
    return {"reading_id": reading_id, "read_at": iso(point[0]), "reading": point[1]}


def _disputed_readings(series: MeterSeries, piece_start: datetime, piece_end: datetime,
                       runs: list[dict[str, Any]], owner_index: int,
                       meter_code: str | None, meter_of_cremator: dict[str, str | None]) -> list[int]:
    result = []
    for t, _v, rid in series.points:
        if not (piece_start < t < piece_end):
            continue
        owners = []
        for index, run in enumerate(runs):
            if meter_of_cremator.get(run["cremator_code"]) != meter_code:
                continue
            r_start = parse_dt(run["actual_start_at"])
            r_end = parse_dt(run["actual_end_at"]) if run.get("actual_end_at") else None
            if r_end is not None and r_start < t < r_end:
                owners.append(index)
        if len(owners) > 1 and owner_index in owners:
            result.append(rid)
    return sorted(set(result))


def diff_reports(previous: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    """对比两个版本的计算结果，返回按批次与合计的前后差异。"""
    before = {b["batch_key"]: b for b in previous.get("batches", [])}
    after = {b["batch_key"]: b for b in current.get("batches", [])}
    batch_changes: list[dict[str, Any]] = []
    for key in sorted(set(before) | set(after)):
        old = before.get(key)
        new = after.get(key)
        if old is None:
            batch_changes.append({"batch_key": key, "change_type": "added", "after": _batch_summary(new)})
        elif new is None:
            batch_changes.append({"batch_key": key, "change_type": "removed", "before": _batch_summary(old)})
        else:
            metrics = {}
            for field in ("fuel_used", "estimated_emission", "purifier_state", "calibration_code"):
                if old.get(field) != new.get(field):
                    metrics[field] = {"before": old.get(field), "after": new.get(field)}
            old_codes = sorted(a["code"] for a in old.get("anomalies", []))
            new_codes = sorted(a["code"] for a in new.get("anomalies", []))
            if old_codes != new_codes:
                metrics["anomaly_codes"] = {"before": old_codes, "after": new_codes}
            if metrics:
                batch_changes.append({"batch_key": key, "change_type": "modified", "changes": metrics})
    totals_changes = {}
    for field in ("fuel_used", "estimated_emission", "batch_count", "anomaly_count", "unattributed_count"):
        old_value = previous.get("totals", {}).get(field)
        new_value = current.get("totals", {}).get(field)
        if old_value != new_value:
            totals_changes[field] = {"before": old_value, "after": new_value}
    return {
        "period_month": current.get("period_month"),
        "batch_changes": batch_changes,
        "totals_changes": totals_changes,
        "changed": bool(batch_changes or totals_changes),
    }


def _batch_summary(batch: dict[str, Any] | None) -> dict[str, Any]:
    if batch is None:
        return {}
    return {
        "run_id": batch.get("run_id"),
        "case_ref": batch.get("case_ref"),
        "cremator_code": batch.get("cremator_code"),
        "meter_code": batch.get("meter_code"),
        "fuel_used": batch.get("fuel_used"),
        "estimated_emission": batch.get("estimated_emission"),
        "anomaly_codes": sorted(a["code"] for a in batch.get("anomalies", [])),
    }
