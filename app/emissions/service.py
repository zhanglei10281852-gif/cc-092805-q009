from __future__ import annotations

import json
import sqlite3
from typing import Any

from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.database import get_connection, transaction
from app.emissions.engine import RULES_VERSION, compute_batch, digest
from app.emissions.repository import DEFAULT_RULES, EmissionsRepository

_DIFF_FIELDS = (
    "resource_code", "planned_start_at", "planned_end_at", "actual_start_at", "actual_end_at",
    "fuel_start_value", "fuel_end_value", "fuel_amount", "purification_efficiency",
    "emission_amount", "included", "revoked",
)


class EmissionsBatchService:
    """按月生成可复算的火化排放批次，并管理签发、更正、影响清单与监管导出。"""

    def __init__(self, connection: sqlite3.Connection | None = None, clock: Clock | None = None) -> None:
        self.connection = connection or get_connection()
        self.clock = clock or SystemClock()
        self.repository = EmissionsRepository(self.connection)
        self.repository.ensure_schema()

    def now(self) -> str:
        return to_storage(self.clock.now())

    # ---- 规则版本 --------------------------------------------------------
    def ensure_default_rules(self, actor: str = "system") -> dict[str, Any]:
        active = self.repository.active_rule_version()
        if active is not None:
            return active
        now = self.now()
        with transaction(immediate=True) as connection:
            repo = EmissionsRepository(connection)
            return self._create_rules(repo, DEFAULT_RULES, "默认排放核算规则", "系统内置首版规则", actor, now)

    def register_rules(self, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        rules = payload["rules"]
        self._validate_rules(rules)
        now = self.now()
        with transaction(immediate=True) as connection:
            repo = EmissionsRepository(connection)
            created = self._create_rules(repo, rules, payload.get("name", "自定义排放核算规则"), payload.get("description", ""), actor, now)
            repo.retire_rule_versions_other_than(created["id"])
            return created

    @staticmethod
    def _create_rules(repo: EmissionsRepository, rules: dict[str, Any], name: str, description: str, actor: str, now: str) -> dict[str, Any]:
        sealed = {"rules_version": RULES_VERSION, "rules": rules}
        return repo.create_rule_version(sealed, digest(sealed), name, description, actor, now)

    @staticmethod
    def _validate_rules(rules: Any) -> None:
        if not isinstance(rules, dict) or not rules:
            raise ValidationError("规则内容不能为空")
        if rules.get("cross_month_policy") not in {"start_month", "end_month"}:
            raise ValidationError("跨月归属策略必须是 start_month 或 end_month")
        if not isinstance(rules.get("purification_states"), dict):
            raise ValidationError("净化状态效率映射缺失")
        factor = rules.get("emission_factor")
        if not isinstance(factor, (int, float)) or factor < 0:
            raise ValidationError("排放因子必须是非负数值")

    def list_rule_versions(self) -> list[dict[str, Any]]:
        return self.repository.list_rule_versions()

    def _resolve_rules(self, repo: EmissionsRepository, rule_version: int | None) -> dict[str, Any]:
        row = repo.rule_version_by_version(rule_version) if rule_version else repo.active_rule_version()
        if row is None:
            raise NotFoundError("排放规则版本不存在，请先登记规则")
        return row

    # ---- 源数据登记 ------------------------------------------------------
    def register_run(self, payload: dict[str, Any]) -> dict[str, Any]:
        values = dict(payload)
        values["start_at"] = to_storage(values["start_at"]) if not isinstance(values["start_at"], str) else values["start_at"]
        values["end_at"] = to_storage(values["end_at"]) if not isinstance(values["end_at"], str) else values["end_at"]
        if values["end_at"] <= values["start_at"]:
            raise ValidationError("实际运行结束时间必须晚于开始时间")
        recorded = values.get("recorded_at")
        values["recorded_at"] = to_storage(recorded) if recorded is not None and not isinstance(recorded, str) else (recorded or self.now())
        now = self.now()
        with transaction(immediate=True) as connection:
            repo = EmissionsRepository(connection)
            existing = repo.run_by_key(values["idempotency_key"])
            if existing:
                return existing
            if values.get("reservation_id") is not None and connection.execute("SELECT 1 FROM facility_reservations WHERE id=?", (values["reservation_id"],)).fetchone() is None:
                raise NotFoundError("关联的火化预约不存在")
            if values.get("case_id") is not None and repo.case(values["case_id"]) is None:
                raise NotFoundError("关联的业务档案不存在")
            return repo.create_run(values, now)

    def register_reading(self, payload: dict[str, Any]) -> dict[str, Any]:
        values = dict(payload)
        values["reading_at"] = to_storage(values["reading_at"]) if not isinstance(values["reading_at"], str) else values["reading_at"]
        recorded = values.get("recorded_at")
        values["recorded_at"] = to_storage(recorded) if recorded is not None and not isinstance(recorded, str) else (recorded or self.now())
        with transaction(immediate=True) as connection:
            repo = EmissionsRepository(connection)
            existing = repo.reading_by_key(values["idempotency_key"])
            if existing:
                return existing
            return repo.create_reading(values, self.now())

    def register_purification(self, payload: dict[str, Any]) -> dict[str, Any]:
        values = dict(payload)
        values["start_at"] = to_storage(values["start_at"]) if not isinstance(values["start_at"], str) else values["start_at"]
        values["end_at"] = to_storage(values["end_at"]) if not isinstance(values["end_at"], str) else values["end_at"]
        if values["end_at"] <= values["start_at"]:
            raise ValidationError("净化状态区间结束时间必须晚于开始时间")
        recorded = values.get("recorded_at")
        values["recorded_at"] = to_storage(recorded) if recorded is not None and not isinstance(recorded, str) else (recorded or self.now())
        with transaction(immediate=True) as connection:
            repo = EmissionsRepository(connection)
            existing = repo.purification_by_key(values["idempotency_key"])
            if existing:
                return existing
            return repo.create_purification(values, self.now())

    # ---- 批次计算 --------------------------------------------------------
    def _gather_snapshot(self, repo: EmissionsRepository) -> dict[str, Any]:
        return {
            "runs": repo.list_runs(),
            "reservations": repo.cremator_reservations(),
            "readings": repo.list_readings(),
            "purification": repo.list_purification(),
            "resources": repo.cremator_resources(),
        }

    @staticmethod
    def _input_summary(snapshot: dict[str, Any], revoked_case_ids: set[int], fault_markers: list[dict[str, Any]]) -> dict[str, Any]:
        def slim(rows: list[dict[str, Any]], fields: tuple[str, ...]) -> list[dict[str, Any]]:
            return [{key: row.get(key) for key in fields} for row in rows]
        return {
            "runs": slim(snapshot["runs"], ("id", "resource_code", "case_id", "reservation_id", "start_at", "end_at", "shift_code", "idempotency_key")),
            "reservations": slim(snapshot["reservations"], ("id", "resource_code", "case_id", "start_at", "end_at", "status")),
            "readings": slim(snapshot["readings"], ("id", "meter_code", "reading_at", "value", "idempotency_key")),
            "purification": slim(snapshot["purification"], ("id", "resource_code", "start_at", "end_at", "state")),
            "resources": slim(snapshot["resources"], ("id", "code", "kind")),
            "revoked_case_ids": sorted(revoked_case_ids),
            "open_fault_marker_ids": [m["id"] for m in fault_markers if m["status"] == "open"],
        }

    def compute(
        self, period: str, actor: str, *,
        rule_version: int | None = None,
        fixed_clock: str | None = None,
        replay: bool = False,
        corrects_batch_id: int | None = None,
        reason: str = "",
    ) -> dict[str, Any]:
        self._validate_period(period)
        now = self.now()
        with transaction(immediate=True) as connection:
            repo = EmissionsRepository(connection)
            rule_row = self._resolve_rules(repo, rule_version)
            rules = json.loads(rule_row["rules_json"])["rules"]
            snapshot = self._gather_snapshot(repo)
            revoked = repo.revoked_case_ids()
            fault_markers = repo.list_fault_markers()
            summary = self._input_summary(snapshot, revoked, fault_markers)
            input_digest = digest({"period": period, "rules_version_id": rule_row["id"], "input": summary})
            result = compute_batch(period, snapshot, rules, revoked_case_ids=revoked, fault_markers=fault_markers)

            if replay:
                seq = repo.next_seq(period)
                batch_id = repo.create_batch({
                    "period": period, "seq": seq, "status": "replay", "rule_version_id": rule_row["id"],
                    "corrects_batch_id": corrects_batch_id, "is_replay": True, "clock_fixed_at": fixed_clock or now,
                    "input_summary": summary, "input_digest": input_digest, "totals": result["totals"],
                    "created_by": actor, "created_at": fixed_clock or now,
                })
            else:
                target = corrects_batch_id
                if target is not None:
                    base = repo.batch(target)
                    if base is None or base["is_replay"]:
                        raise NotFoundError("被更正的批次不存在或为重放批次")
                    if base["period"] != period:
                        raise ValidationError("更正批次必须与原批次属于同一月份")
                draft = repo.open_draft(period)
                if draft is None:
                    seq = repo.next_seq(period)
                    batch_id = repo.create_batch({
                        "period": period, "seq": seq, "status": "draft", "rule_version_id": rule_row["id"],
                        "corrects_batch_id": target, "is_replay": False, "clock_fixed_at": fixed_clock,
                        "input_summary": summary, "input_digest": input_digest, "totals": result["totals"],
                        "created_by": actor, "created_at": now,
                    })
                else:
                    if target is None:
                        target = draft["corrects_batch_id"]
                    batch_id = draft["id"]
                    repo.clear_batch_children(batch_id)
                    repo.update_batch_snapshot(batch_id, rule_row["id"], fixed_clock, summary, input_digest, result["totals"], target)

            self._persist_result(repo, batch_id, result)
            if not replay and (corrects_batch_id is not None or repo.batch(batch_id)["corrects_batch_id"]):
                base_id = repo.batch(batch_id)["corrects_batch_id"]
                self._persist_deltas(repo, batch_id, base_id, result["items"], reason)
            return self.get_batch(batch_id, repo)

    @staticmethod
    def _persist_result(repo: EmissionsRepository, batch_id: int, result: dict[str, Any]) -> None:
        for item in result["items"]:
            repo.add_item(batch_id, item)
        for anomaly in result["anomalies"]:
            repo.add_anomaly(batch_id, anomaly["item_key"], anomaly["code"], anomaly["severity"], anomaly["message"], anomaly["refs"])

    def _persist_deltas(self, repo: EmissionsRepository, batch_id: int, base_id: int, new_items: list[dict[str, Any]], reason: str) -> None:
        base_items = {item["item_key"]: item for item in repo.items(base_id)}
        new_by_key = {item["item_key"]: item for item in new_items}
        note = reason or "更正版重新核算"
        for key, new_item in new_by_key.items():
            old_item = base_items.get(key)
            if old_item is None:
                repo.add_delta(batch_id, key, "membership", None, "present", "更正版新增炉次/预约明细")
                continue
            for field in _DIFF_FIELDS:
                before = old_item.get(field)
                after = new_item.get(field)
                if before != after:
                    repo.add_delta(batch_id, key, field, before, after, note)
        for key in base_items.keys() - new_by_key.keys():
            repo.add_delta(batch_id, key, "membership", "present", None, "更正版移除明细")

    def issue(self, batch_id: int, actor: str) -> dict[str, Any]:
        now = self.now()
        with transaction(immediate=True) as connection:
            repo = EmissionsRepository(connection)
            batch = repo.batch(batch_id)
            if batch is None:
                raise NotFoundError("排放批次不存在")
            if batch["is_replay"]:
                raise ConflictError("重放批次不能签发")
            if batch["status"] == "issued":
                raise ConflictError("批次已经签发，签发报表不可变更")
            if batch["status"] != "draft":
                raise ConflictError("当前批次状态不能签发")
            repo.mark_issued(batch_id, actor, now)
            return self.get_batch(batch_id, repo)

    def list_batches(self, period: str | None = None) -> list[dict[str, Any]]:
        return self.repository.list_batches(period)

    def get_batch(self, batch_id: int, repo: EmissionsRepository | None = None) -> dict[str, Any]:
        repo = repo or self.repository
        batch = repo.batch(batch_id)
        if batch is None:
            raise NotFoundError("排放批次不存在")
        rule = repo.rule_version(batch["rule_version_id"]) or {}
        result = dict(batch)
        result["is_replay"] = bool(batch["is_replay"])
        result["input_summary"] = json.loads(batch["input_summary_json"])
        result["totals"] = json.loads(batch["totals_json"])
        result["rule_version"] = {"id": rule.get("id"), "version": rule.get("version"), "rules_digest": rule.get("rules_digest"), "status": rule.get("status")}
        result["items"] = repo.items(batch_id)
        result["anomalies"] = repo.anomalies(batch_id)
        result["deltas"] = repo.deltas(batch_id)
        result["impacts"] = repo.impacts_for_batch(batch_id)
        corrects = repo.batch(batch["corrects_batch_id"]) if batch["corrects_batch_id"] else None
        result["corrects"] = None if corrects is None else {"id": corrects["id"], "period": corrects["period"], "seq": corrects["seq"], "status": corrects["status"]}
        return result

    # ---- 撤销档案 / 设备故障：生成受影响清单 ------------------------------
    def revoke_case(self, case_id: int, actor: str, reason: str) -> dict[str, Any]:
        now = self.now()
        with transaction(immediate=True) as connection:
            repo = EmissionsRepository(connection)
            case = repo.case(case_id)
            if case is None:
                raise NotFoundError("逝者业务档案不存在")
            if repo.case_revocation(case_id) is not None:
                raise ConflictError("业务档案已标记撤销")
            repo.revoke_case(case_id, reason, actor, now)
            affected = repo.issued_items_case(case_id)
            impact_ids: list[int] = []
            for row in affected:
                impact_ids.append(repo.add_impact({
                    "trigger_type": "case_revoked", "trigger_id": case_id,
                    "batch_id": row["batch_id"], "item_key": row["item_key"],
                    "reason": reason, "detail": {"period": row["period"], "seq": row["seq"], "resource_code": row["resource_code"]},
                    "created_by": actor,
                }, now))
            repo.event(case_id, "emission.case_revoked", actor, {"reason": reason, "affected_batch_items": [{"batch_id": x["batch_id"], "item_key": x["item_key"]} for x in affected]}, now)
            impact_records = [repo.impact(impact_id) for impact_id in impact_ids]
            return {"case_id": case_id, "reason": reason, "affected_count": len(affected), "affected": affected, "impact_records": impact_records}

    def mark_equipment_fault(self, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        now = self.now()
        start_at = payload["start_at"] if isinstance(payload["start_at"], str) else to_storage(payload["start_at"])
        end_at = payload["end_at"] if isinstance(payload["end_at"], str) else to_storage(payload["end_at"])
        if end_at <= start_at:
            raise ValidationError("故障区间结束时间必须晚于开始时间")
        with transaction(immediate=True) as connection:
            repo = EmissionsRepository(connection)
            marker = repo.create_fault_marker({
                "resource_code": payload["resource_code"], "start_at": start_at, "end_at": end_at,
                "kind": payload["kind"], "note": payload.get("note", ""), "marked_by": actor,
            }, now)
            affected = repo.issued_items_overlap(payload["resource_code"], start_at, end_at)
            impact_ids: list[int] = []
            for row in affected:
                impact_ids.append(repo.add_impact({
                    "trigger_type": "equipment_fault", "trigger_id": marker["id"],
                    "batch_id": row["batch_id"], "item_key": row["item_key"],
                    "reason": payload.get("note", "设备故障标记"),
                    "detail": {"period": row["period"], "seq": row["seq"], "case_id": row["case_id"], "fault_marker_id": marker["id"]},
                    "created_by": actor,
                }, now))
            return {"fault_marker_id": marker["id"], "affected_count": len(affected), "affected": affected, "impact_ids": impact_ids}

    def list_impacts(self, status: str | None = None) -> list[dict[str, Any]]:
        return self.repository.list_impacts(status)

    def resolve_impact(self, impact_id: int, actor: str, note: str) -> dict[str, Any]:
        now = self.now()
        with transaction(immediate=True) as connection:
            repo = EmissionsRepository(connection)
            impact = repo.impact(impact_id)
            if impact is None:
                raise NotFoundError("受影响记录不存在")
            if impact["status"] == "resolved":
                return dict(impact)
            repo.resolve_impact(impact_id, note, now)
            return repo.impact(impact_id) or {}

    # ---- 监管导出 --------------------------------------------------------
    def export_version(self, batch_id: int) -> dict[str, Any]:
        batch = self.get_batch(batch_id)
        rule = self.repository.rule_version(batch["rule_version_id"]) or {}
        return {
            "report": {
                "batch_id": batch["id"], "period": batch["period"], "seq": batch["seq"],
                "status": batch["status"], "is_replay": bool(batch["is_replay"]),
                "clock_fixed_at": batch["clock_fixed_at"], "created_by": batch["created_by"],
                "created_at": batch["created_at"], "issued_by": batch["issued_by"], "issued_at": batch["issued_at"],
                "corrects": batch["corrects"],
            },
            "rule_version": {"version": rule.get("version"), "rules_digest": rule.get("rules_digest"), "rules": json.loads(rule["rules_json"]) if rule else None},
            "input_summary": batch["input_summary"],
            "input_digest": batch["input_digest"],
            "totals": batch["totals"],
            "details": batch["items"],
            "anomalies": [{"item_key": a["item_key"], "code": a["code"], "severity": a["severity"], "message": a["message"], "refs": a["refs"]} for a in batch["anomalies"]],
            "differences": [
                {"item_key": d["item_key"], "field": d["field"], "before": d["before_value"], "after": d["after_value"], "reason": d["reason"]}
                for d in batch["deltas"]
            ],
            "impacts": [
                {"id": i["id"], "trigger_type": i["trigger_type"], "item_key": i["item_key"], "reason": i["reason"], "status": i["status"], "detail": i["detail"]}
                for i in batch["impacts"]
            ],
        }

    @staticmethod
    def _validate_period(period: str) -> None:
        if len(period) != 7 or period[4] != "-" or not period[:4].isdigit() or not period[5:7].isdigit():
            raise ValidationError("月份格式必须为 YYYY-MM")
        month = int(period[5:7])
        if not 1 <= month <= 12:
            raise ValidationError("月份必须在 01 到 12 之间")
