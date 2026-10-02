from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import timedelta
from typing import Any

from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.database import get_connection, transaction
from app.emissions import engine
from app.emissions.engine import iso, month_bounds, parse_dt
from app.emissions.repository import EmissionsRepository

# 快照向月界两侧扩展的天数，保证跨月炉次在月界处有插值端点
SNAPSHOT_MARGIN_DAYS = 3


def digest(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


class EmissionsService:
    """火化炉排放批次的录入、可复算月结、版本签发与撤销影响管理。"""

    def __init__(self, connection: sqlite3.Connection | None = None, clock: Clock | None = None) -> None:
        self.connection = connection or get_connection()
        self.clock = clock or SystemClock()
        self.repository = EmissionsRepository(self.connection)
        self.repository.ensure_schema()

    def now(self) -> str:
        return to_storage(self.clock.now())

    # -- 主数据 ------------------------------------------------------------

    def create_cremator(self, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        now = self.now()
        with transaction(immediate=True) as connection:
            repo = EmissionsRepository(connection)
            if repo.cremator(payload["code"]):
                raise ConflictError("火化炉编码已存在")
            if payload.get("meter_code"):
                duplicated = connection.execute(
                    "SELECT code FROM emission_cremators WHERE meter_code=? AND active=1",
                    (payload["meter_code"],)).fetchone()
                if duplicated:
                    raise ConflictError("燃料表已绑定其他火化炉", context={"cremator_code": duplicated[0]})
            cremator = repo.create_cremator(payload, now)
            repo.event("cremator", cremator["id"], "cremator.created", actor,
                       {"code": cremator["code"], "meter_code": cremator["meter_code"]}, now)
            return cremator

    def list_cremators(self) -> list[dict[str, Any]]:
        return self.repository.list_cremators()

    def create_rule(self, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        """登记校准规则；同一代码再次登记即产生新的规则版本。"""
        now = self.now()
        valid_from = to_storage(payload["valid_from"])
        valid_to = to_storage(payload["valid_to"]) if payload.get("valid_to") else None
        if valid_to and valid_to <= valid_from:
            raise ValidationError("规则失效时间必须晚于生效时间")
        with transaction(immediate=True) as connection:
            repo = EmissionsRepository(connection)
            version = repo.next_rule_version(payload["code"])
            rule = repo.create_rule({
                "code": payload["code"], "version": version, "name": payload["name"],
                "fuel_factor": payload["fuel_factor"], "purifier_uplift": payload.get("purifier_uplift", 0.0),
                "meter_tolerance": payload.get("meter_tolerance", 0.0),
                "max_run_minutes": payload.get("max_run_minutes", 600),
                "valid_from": valid_from, "valid_to": valid_to, "created_by": actor,
            }, now)
            repo.event("rule", rule["id"], "calibration_rule.created", actor,
                       {"code": rule["code"], "version": version}, now)
            return rule

    def list_rules(self) -> list[dict[str, Any]]:
        return self.repository.list_rules()

    # -- 输入数据录入 -------------------------------------------------------

    def add_fuel_reading(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = self.now()
        read_at = to_storage(payload["read_at"])
        with transaction(immediate=True) as connection:
            repo = EmissionsRepository(connection)
            existing = repo.reading_key(payload["meter_code"], payload["idempotency_key"])
            if existing:
                if existing["read_at"] != read_at or float(existing["reading"]) != float(payload["reading"]):
                    raise ConflictError("同一幂等键对应了不同的燃料读数")
                return existing
            if payload["reading"] < 0:
                raise ValidationError("累计读数不能为负")
            reading = repo.create_reading({
                "meter_code": payload["meter_code"], "read_at": read_at,
                "reading": float(payload["reading"]),
                "batch_key": payload.get("batch_key", ""),
                "idempotency_key": payload["idempotency_key"],
            }, now)
            repo.event("fuel_reading", reading["id"], "fuel_reading.recorded",
                       payload.get("recorded_by", "system"),
                       {"meter_code": reading["meter_code"], "read_at": read_at,
                        "reading": reading["reading"]}, now)
            return reading

    def add_purifier_state(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = self.now()
        start_at = to_storage(payload["start_at"])
        end_at = to_storage(payload["end_at"]) if payload.get("end_at") else None
        if end_at and end_at <= start_at:
            raise ValidationError("净化状态结束时间必须晚于开始时间")
        with transaction(immediate=True) as connection:
            repo = EmissionsRepository(connection)
            self._require_cremator(repo, payload["cremator_code"])
            existing = repo.purifier_key(payload["cremator_code"], payload["idempotency_key"])
            if existing:
                if existing["start_at"] != start_at or existing.get("end_at") != end_at \
                        or existing["state"] != payload["state"]:
                    raise ConflictError("同一幂等键对应了不同的净化状态记录")
                return existing
            state = repo.create_purifier({
                "cremator_code": payload["cremator_code"], "start_at": start_at,
                "end_at": end_at, "state": payload["state"], "note": payload.get("note", ""),
                "idempotency_key": payload["idempotency_key"],
            }, now)
            repo.event("purifier", state["id"], "purifier.recorded",
                       payload.get("recorded_by", "system"),
                       {"cremator_code": state["cremator_code"], "state": state["state"],
                        "start_at": start_at, "end_at": end_at}, now)
            return state

    def record_run(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = self.now()
        start_at = to_storage(payload["actual_start_at"])
        end_at = to_storage(payload["actual_end_at"]) if payload.get("actual_end_at") else None
        if end_at and end_at <= start_at:
            raise ValidationError("炉次实际结束时间必须晚于开始时间")
        with transaction(immediate=True) as connection:
            repo = EmissionsRepository(connection)
            self._require_cremator(repo, payload["cremator_code"])
            if payload.get("reservation_id") is not None:
                reservation = repo.reservation(payload["reservation_id"])
                if reservation is not None and reservation["resource_code"] != payload["cremator_code"]:
                    raise ValidationError("预约资源与火化炉不一致", context={
                        "reservation_resource": reservation["resource_code"],
                        "cremator_code": payload["cremator_code"]})
            existing = repo.run_key(payload["cremator_code"], payload["idempotency_key"])
            if existing:
                if existing["actual_start_at"] != start_at or existing.get("actual_end_at") != end_at:
                    raise ConflictError("同一幂等键对应了不同的实际运行区间")
                return existing
            run = repo.create_run({
                "cremator_code": payload["cremator_code"], "case_ref": payload.get("case_ref"),
                "reservation_id": payload.get("reservation_id"),
                "actual_start_at": start_at, "actual_end_at": end_at,
                "shift_code": payload.get("shift_code", ""),
                "idempotency_key": payload["idempotency_key"],
            }, now)
            repo.event("run", run["id"], "run.recorded", payload.get("recorded_by", "system"),
                       {"cremator_code": run["cremator_code"], "case_ref": run.get("case_ref"),
                        "start_at": start_at, "end_at": end_at}, now)
            return run

    def raise_equipment_flag(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = self.now()
        start_at = to_storage(payload["start_at"])
        end_at = to_storage(payload["end_at"]) if payload.get("end_at") else None
        with transaction(immediate=True) as connection:
            repo = EmissionsRepository(connection)
            existing = repo.flag_key(payload["idempotency_key"])
            if existing:
                return existing
            flag = repo.create_flag({
                "equipment_code": payload["equipment_code"], "flag_type": payload["flag_type"],
                "start_at": start_at, "end_at": end_at, "reason": payload["reason"],
                "raised_by": payload["raised_by"], "idempotency_key": payload["idempotency_key"],
            }, now)
            repo.event("equipment_flag", flag["id"], "equipment_flag.raised", payload["raised_by"],
                       {"equipment_code": flag["equipment_code"], "flag_type": flag["flag_type"],
                        "start_at": start_at}, now)
            return flag

    def resolve_equipment_flag(self, flag_id: int, actor: str) -> dict[str, Any]:
        now = self.now()
        with transaction(immediate=True) as connection:
            repo = EmissionsRepository(connection)
            flag = repo.flag(flag_id)
            if flag is None:
                raise NotFoundError("设备故障标记不存在")
            if flag["status"] == "resolved":
                return flag
            repo.resolve_flag(flag_id, now)
            affected = self._runs_reports_for_flag(repo, flag)
            impact = {
                "subject_type": "flag", "subject_id": flag_id, "action": "resolved",
                "reason": flag["reason"], "affected_json": json.dumps(affected, ensure_ascii=False, sort_keys=True),
                "acted_by": actor,
            }
            impact_id = repo.insert_impact(impact, now)
            repo.event("equipment_flag", flag_id, "equipment_flag.resolved", actor,
                       {"impact_id": impact_id, "affected": affected}, now)
            return {"flag": repo.flag(flag_id), "impact_id": impact_id, "affected": affected}

    # -- 月结算与版本 -------------------------------------------------------

    def compute_report(self, period_month: str, rule_code: str, created_by: str, *,
                       fixed_clock: Any = None, force: bool = False) -> dict[str, Any]:
        """按月计算排放批次版本。

        fixed_clock 给出时按固定时钟重放（只采用当时已到达的输入）；
        force=True 表示即使存在已签发版本也追加计算结果（签发/更正流程另走
        issue/correction），已签发版本的内容始终不变。
        """
        self._validate_month(period_month)
        as_of = to_storage(fixed_clock) if fixed_clock else None
        now = self.now()
        with transaction(immediate=True) as connection:
            repo = EmissionsRepository(connection)
            latest = repo.latest_report(period_month)
            snapshot = self._build_snapshot(repo, period_month, as_of)
            snapshot["period_month"] = period_month
            snapshot["rule_code"] = rule_code
            input_digest = digest(snapshot)

            # 相同输入、相同规则与截止时刻已计算过：直接复用，保证可复算幂等；
            # 但已签发版本不可变，重算请求（非固定时钟副本）一律拒绝
            existing = repo.find_report_by_input(period_month, rule_code, input_digest, as_of)
            if existing is not None:
                if existing["status"] == "issued" and as_of is None:
                    raise ConflictError("该月版本已签发且输入未变化，无需重算；后续数据请创建更正版",
                                        context={"issued_version": existing["version_no"],
                                                 "report_id": existing["id"]})
                return self._report_view(repo, existing["id"])

            # 已签发版本不可变：普通重算被拒绝；固定时钟重放只另存审计副本，
            # 不会覆盖已签发版本
            if latest and latest["status"] == "issued" and not force and as_of is None:
                raise ConflictError("该月已有签发版本，新增数据请创建更正版",
                                    context={"issued_version": latest["version_no"], "report_id": latest["id"]})

            result = engine.compute_period(snapshot, rule_code)
            result_digest = digest(result)
            rule_version = self._rule_version(snapshot, rule_code)
            version_no = (latest["version_no"] + 1) if latest else 1
            report_id = repo.insert_report({
                "period_month": period_month, "version_no": version_no,
                "rule_code": rule_code, "rule_version": rule_version,
                "clock_fixed_at": as_of, "input_digest": input_digest,
                "input_snapshot_json": json.dumps(snapshot, ensure_ascii=False, sort_keys=True),
                "result_digest": result_digest,
                "totals_json": json.dumps(result["totals"], ensure_ascii=False, sort_keys=True),
                "anomaly_count": result["totals"]["anomaly_count"],
                "reason": "固定时钟重放" if as_of else "",
                "created_by": created_by, "created_at": now,
            })
            self._persist_result(repo, report_id, result, now)
            repo.event("report", report_id, "report.computed", created_by,
                       {"period_month": period_month, "version_no": version_no,
                        "input_digest": input_digest, "result_digest": result_digest,
                        "clock_fixed_at": as_of}, now)
            return self._report_view(repo, report_id)

    def create_correction(self, period_month: str, rule_code: str, created_by: str, reason: str, *,
                          fixed_clock: Any = None) -> dict[str, Any]:
        """基于最新输入生成更正版，解释与上一版（通常已签发）的差异。"""
        self._validate_month(period_month)
        as_of = to_storage(fixed_clock) if fixed_clock else None
        if not reason.strip():
            raise ValidationError("更正版必须说明更正原因")
        now = self.now()
        with transaction(immediate=True) as connection:
            repo = EmissionsRepository(connection)
            previous = repo.latest_official_report(period_month)
            if previous is None:
                raise NotFoundError("该月尚无正式报表版本，应先计算初版")
            snapshot = self._build_snapshot(repo, period_month, as_of)
            snapshot["period_month"] = period_month
            snapshot["rule_code"] = rule_code
            input_digest = digest(snapshot)
            result = engine.compute_period(snapshot, rule_code)
            result_digest = digest(result)
            previous_result = self._stored_result(repo, previous["id"])
            delta = engine.diff_reports(previous_result, result)
            newest = repo.latest_report(period_month)
            version_no = (newest["version_no"] + 1) if newest else 1
            rule_version = self._rule_version(snapshot, rule_code)
            report_id = repo.insert_report({
                "period_month": period_month, "version_no": version_no,
                "rule_code": rule_code, "rule_version": rule_version,
                "clock_fixed_at": as_of, "input_digest": input_digest,
                "input_snapshot_json": json.dumps(snapshot, ensure_ascii=False, sort_keys=True),
                "result_digest": result_digest,
                "totals_json": json.dumps(result["totals"], ensure_ascii=False, sort_keys=True),
                "anomaly_count": result["totals"]["anomaly_count"],
                "correction_of_id": previous["id"], "reason": reason,
                "created_by": created_by, "created_at": now,
            })
            self._persist_result(repo, report_id, result, now)
            for change in delta["batch_changes"]:
                repo.insert_change({
                    "report_id": report_id, "batch_key": change["batch_key"],
                    "run_id": (change.get("after") or change.get("before") or {}).get("run_id"),
                    "change_type": change["change_type"],
                    "detail_json": json.dumps(change, ensure_ascii=False, sort_keys=True),
                }, now)
            if delta["totals_changes"]:
                repo.insert_change({
                    "report_id": report_id, "change_type": "totals",
                    "detail_json": json.dumps(delta["totals_changes"], ensure_ascii=False, sort_keys=True),
                }, now)
            repo.event("report", report_id, "report.correction_created", created_by,
                       {"period_month": period_month, "version_no": version_no,
                        "correction_of": previous["id"], "reason": reason,
                        "changed": delta["changed"]}, now)
            view = self._report_view(repo, report_id)
            view["changes"] = self._hydrate_changes(repo, report_id)
            view["diff"] = delta
            return view

    def issue_report(self, report_id: int, actor: str) -> dict[str, Any]:
        now = self.now()
        with transaction(immediate=True) as connection:
            repo = EmissionsRepository(connection)
            report = repo.report(report_id)
            if report is None:
                raise NotFoundError("排放报表不存在")
            if report["status"] == "issued":
                return self._report_view(repo, report_id)
            if report["status"] != "computed":
                raise ConflictError("当前版本不能签发")
            if report.get("clock_fixed_at"):
                raise ConflictError("固定时钟重放版本仅供复算核对，不能签发")
            repo.issue_report(report_id, actor, now)
            # 同月此前已签发版本被替代，但其明细行不做任何修改
            predecessor = connection.execute(
                "SELECT id FROM emission_reports WHERE period_month=? AND status='issued' AND id<>?",
                (report["period_month"], report_id)).fetchall()
            for row in predecessor:
                repo.supersede_report(row[0])
            repo.event("report", report_id, "report.issued", actor,
                       {"period_month": report["period_month"], "version_no": report["version_no"]}, now)
            return self._report_view(repo, report_id)

    # -- 撤销 / 故障：生成受影响清单而不是改数 ------------------------------

    def revoke_fuel_reading(self, reading_id: int, reason: str, actor: str) -> dict[str, Any]:
        if not reason.strip():
            raise ValidationError("撤销燃料读数必须填写原因")
        now = self.now()
        with transaction(immediate=True) as connection:
            repo = EmissionsRepository(connection)
            reading = repo.reading(reading_id)
            if reading is None:
                raise NotFoundError("燃料读数不存在")
            if reading["status"] == "revoked":
                return self._existing_revocation(repo, "reading", reading_id)
            repo.revoke_reading(reading_id, reason, now)
            affected = self._reports_digest(repo, repo.reports_using_reading(reading_id))
            impact_id = repo.insert_impact({
                "subject_type": "reading", "subject_id": reading_id, "action": "revoked",
                "reason": reason, "affected_json": json.dumps(affected, ensure_ascii=False, sort_keys=True),
                "acted_by": actor,
            }, now)
            repo.event("fuel_reading", reading_id, "fuel_reading.revoked", actor,
                       {"reason": reason, "impact_id": impact_id,
                        "issued_reports": affected["issued_reports"]}, now)
            return {"impact_id": impact_id, "subject": {"type": "reading", "id": reading_id},
                    "affected": affected}

    def revoke_run(self, run_id: int, reason: str, actor: str) -> dict[str, Any]:
        if not reason.strip():
            raise ValidationError("撤销炉次必须填写原因")
        now = self.now()
        with transaction(immediate=True) as connection:
            repo = EmissionsRepository(connection)
            run = repo.run(run_id)
            if run is None:
                raise NotFoundError("炉次记录不存在")
            if run["status"] == "revoked":
                return self._existing_revocation(repo, "run", run_id)
            repo.revoke_run(run_id, reason, now)
            affected = self._reports_digest(repo, repo.reports_using_run(run_id))
            impact_id = repo.insert_impact({
                "subject_type": "run", "subject_id": run_id, "action": "revoked",
                "reason": reason, "affected_json": json.dumps(affected, ensure_ascii=False, sort_keys=True),
                "acted_by": actor,
            }, now)
            repo.event("run", run_id, "run.revoked", actor,
                       {"reason": reason, "impact_id": impact_id,
                        "issued_reports": affected["issued_reports"]}, now)
            return {"impact_id": impact_id, "subject": {"type": "run", "id": run_id},
                    "affected": affected}

    def revoke_case(self, case_ref: str, reason: str, actor: str) -> dict[str, Any]:
        """撤销火化业务档案：不删除炉次，登记撤销并列受影响清单。"""
        if not reason.strip():
            raise ValidationError("撤销业务档案必须填写原因")
        now = self.now()
        with transaction(immediate=True) as connection:
            repo = EmissionsRepository(connection)
            if repo.case_revoked(case_ref):
                return self._existing_revocation(repo, "case", case_ref)
            runs = repo.active_runs_for_case(case_ref)
            report_rows: list[dict[str, Any]] = []
            for run in runs:
                report_rows.extend(repo.reports_using_run(run["id"]))
            dedup = {row["id"]: row for row in report_rows}
            affected = self._reports_digest(repo, list(dedup.values()))
            affected["run_ids"] = [run["id"] for run in runs]
            repo.revoke_case(case_ref, reason, actor, now)
            impact_id = repo.insert_impact({
                "subject_type": "case", "subject_id": case_ref, "action": "revoked",
                "reason": reason, "affected_json": json.dumps(affected, ensure_ascii=False, sort_keys=True),
                "acted_by": actor,
            }, now)
            repo.event("case", case_ref, "case.revoked", actor,
                       {"reason": reason, "impact_id": impact_id, "run_ids": affected["run_ids"],
                        "issued_reports": affected["issued_reports"]}, now)
            return {"impact_id": impact_id, "subject": {"type": "case", "id": case_ref},
                    "affected": affected}

    # -- 查询与监管导出 -----------------------------------------------------

    def list_reports(self, period_month: str | None = None) -> list[dict[str, Any]]:
        return self.repository.list_reports(period_month)

    def get_report(self, report_id: int) -> dict[str, Any]:
        view = self._report_view(self.repository, report_id)
        view["changes"] = self._hydrate_changes(self.repository, report_id)
        return view

    def export_report(self, report_id: int) -> dict[str, Any]:
        """导出指定版本的明细、异常原因、前后差异与输入摘要。"""
        repo = self.repository
        report = repo.report(report_id)
        if report is None:
            raise NotFoundError("排放报表不存在")
        batches = self._hydrate_batches(repo, report_id)
        snapshot = json.loads(report["input_snapshot_json"]) if report.get("input_snapshot_json") else {}
        input_summary = {
            "input_digest": report["input_digest"],
            "rule_code": report["rule_code"],
            "rule_version": report["rule_version"],
            "clock_fixed_at": report["clock_fixed_at"],
            "counts": {
                "cremators": len(snapshot.get("cremators", [])),
                "runs": len(snapshot.get("runs", [])),
                "readings": len(snapshot.get("readings", [])),
                "purifier_windows": len(snapshot.get("purifier", [])),
                "equipment_flags": len(snapshot.get("flags", [])),
                "reservations": len(snapshot.get("reservations", [])),
                "rules": len(snapshot.get("rules", [])),
            },
        }
        return {
            "report": {key: report[key] for key in (
                "id", "period_month", "version_no", "status", "rule_code", "rule_version",
                "clock_fixed_at", "input_digest", "result_digest", "totals_json",
                "anomaly_count", "correction_of_id", "reason", "created_by",
                "created_at", "issued_at", "issued_by")},
            "input_summary": input_summary,
            "batches": batches,
            "unattributed": repo.unattributed(report_id),
            "changes": self._hydrate_changes(repo, report_id),
            "input_snapshot": snapshot,
        }

    def list_impacts(self, subject_type: str | None = None, subject_id: str | None = None) -> list[dict[str, Any]]:
        return self._hydrate_impacts(self.repository.impacts(subject_type, subject_id))

    # -- 内部辅助 -----------------------------------------------------------

    def _build_snapshot(self, repo: EmissionsRepository, period_month: str, as_of: str | None) -> dict[str, Any]:
        month_start, month_end = month_bounds(period_month)
        window_start = iso(month_start - timedelta(days=SNAPSHOT_MARGIN_DAYS))
        window_end = iso(month_end + timedelta(days=SNAPSHOT_MARGIN_DAYS))
        snapshot = repo.snapshot(window_start, window_end, as_of)
        snapshot["cremators"] = repo.active_cremators(as_of)
        if "rules" not in snapshot:
            snapshot["rules"] = repo.list_rules()
        return snapshot

    @staticmethod
    def _rule_version(snapshot: dict[str, Any], rule_code: str) -> int:
        versions = [int(r["version"]) for r in snapshot.get("rules", []) if r["code"] == rule_code]
        return max(versions, default=0)

    def _persist_result(self, repo: EmissionsRepository, report_id: int, result: dict[str, Any], now: str) -> None:
        for batch in result["batches"]:
            batch_id = repo.insert_batch({
                "report_id": report_id,
                "batch_key": batch["batch_key"],
                "cremator_code": batch.get("cremator_code", ""),
                "run_id": batch.get("run_id"),
                "case_ref": batch.get("case_ref"),
                "month": batch["month"],
                "reservation_window_json": json.dumps(batch.get("reservation_window", {}), ensure_ascii=False, sort_keys=True),
                "actual_window_json": json.dumps(batch.get("actual_window", {}), ensure_ascii=False, sort_keys=True),
                "fuel_allocation_json": json.dumps(batch.get("fuel_allocation", []), ensure_ascii=False, sort_keys=True),
                "purifier_state": batch.get("purifier_state", ""),
                "calibration_code": batch.get("calibration_code", ""),
                "fuel_used": batch["fuel_used"],
                "estimated_emission": batch["estimated_emission"],
                "anomalies_json": json.dumps(batch.get("anomalies", []), ensure_ascii=False, sort_keys=True),
            })
            for item in batch.get("fuel_allocation", []):
                repo.link_batch_reading(batch_id, item["reading_id"])
        for item in result.get("unattributed", []):
            repo.insert_unattributed({"report_id": report_id, **item})

    def _stored_result(self, repo: EmissionsRepository, report_id: int) -> dict[str, Any]:
        report = repo.report(report_id) or {}
        batches = []
        for row in repo.batches(report_id):
            batches.append({
                "batch_key": row["batch_key"],
                "fuel_used": row["fuel_used"],
                "estimated_emission": row["estimated_emission"],
                "purifier_state": row["purifier_state"],
                "calibration_code": row["calibration_code"],
                "anomalies": json.loads(row["anomalies_json"]),
            })
        return {"period_month": report.get("period_month"),
                "batches": batches,
                "totals": json.loads(report.get("totals_json") or "{}")}

    def _report_view(self, repo: EmissionsRepository, report_id: int) -> dict[str, Any]:
        report = repo.report(report_id)
        if report is None:
            raise NotFoundError("排放报表不存在")
        view = dict(report)
        view["totals"] = json.loads(report["totals_json"])
        view["batches"] = self._hydrate_batches(repo, report_id)
        view["unattributed"] = repo.unattributed(report_id)
        return view

    @staticmethod
    def _hydrate_batches(repo: EmissionsRepository, report_id: int) -> list[dict[str, Any]]:
        batches = []
        for row in repo.batches(report_id):
            item = dict(row)
            for column in ("reservation_window_json", "actual_window_json",
                           "fuel_allocation_json", "anomalies_json"):
                item[column.removesuffix("_json")] = json.loads(item.pop(column))
            batches.append(item)
        return batches

    @staticmethod
    def _hydrate_changes(repo: EmissionsRepository, report_id: int) -> list[dict[str, Any]]:
        changes = []
        for row in repo.changes(report_id):
            item = dict(row)
            item["detail"] = json.loads(item.pop("detail_json"))
            changes.append(item)
        return changes

    @staticmethod
    def _hydrate_impacts(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        result = []
        for row in rows:
            item = dict(row)
            item["affected"] = json.loads(item.pop("affected_json"))
            result.append(item)
        return result

    @staticmethod
    def _reports_digest(repo: EmissionsRepository, rows: list[dict[str, Any]]) -> dict[str, Any]:
        del repo
        versions = [{
            "report_id": row["id"], "period_month": row["period_month"],
            "version_no": row["version_no"], "status": row["status"],
            "correction_required": row["status"] == "issued",
        } for row in rows]
        issued = [v for v in versions if v["status"] == "issued"]
        return {
            "report_versions": versions,
            "issued_reports": issued,
            "requires_correction": bool(issued),
        }

    @staticmethod
    def _runs_reports_for_flag(repo: EmissionsRepository, flag: dict[str, Any]) -> dict[str, Any]:
        start = parse_dt(flag["start_at"])
        end = parse_dt(flag["end_at"]) if flag.get("end_at") else start + timedelta(seconds=1)
        run_ids: list[int] = []
        for run in repo.active_runs():
            if run["cremator_code"] != flag["equipment_code"]:
                continue
            r_start = parse_dt(run["actual_start_at"])
            r_end = parse_dt(run["actual_end_at"]) if run.get("actual_end_at") else r_start + timedelta(seconds=1)
            if engine.overlap_seconds(r_start, r_end, start, end) > 0:
                run_ids.append(run["id"])
        reports: dict[int, dict[str, Any]] = {}
        for run_id in run_ids:
            for row in repo.reports_using_run(run_id):
                reports[row["id"]] = row
        digest = EmissionsService._reports_digest(repo, list(reports.values()))
        digest["run_ids"] = run_ids
        return digest

    def _existing_revocation(self, repo: EmissionsRepository, subject_type: str, subject_id: Any) -> dict[str, Any]:
        rows = repo.impacts(subject_type, str(subject_id))
        if not rows:
            raise ConflictError("记录已撤销但找不到影响清单")
        latest = self._hydrate_impacts(rows)[0]
        return {"impact_id": latest["id"], "subject": {"type": subject_type, "id": subject_id},
                "affected": latest["affected"], "already_revoked": True}

    @staticmethod
    def _require_cremator(repo: EmissionsRepository, code: str) -> None:
        cremator = repo.cremator(code)
        if cremator is None or not cremator["active"]:
            raise NotFoundError("火化炉不存在或已停用")

    @staticmethod
    def _validate_month(period_month: str) -> None:
        try:
            month_bounds(period_month)
        except (ValueError, TypeError):
            raise ValidationError("月份格式必须为 YYYY-MM")
