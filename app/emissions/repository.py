from __future__ import annotations

import json
import sqlite3
from typing import Any


SCHEMA = r'''
CREATE TABLE IF NOT EXISTS emission_rule_versions (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 version INTEGER NOT NULL UNIQUE,
 name TEXT NOT NULL,
 rules_json TEXT NOT NULL,
 rules_digest TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','retired')),
 description TEXT NOT NULL DEFAULT '',
 created_by TEXT NOT NULL,
 created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS furnace_runs (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 resource_code TEXT NOT NULL,
 case_id INTEGER REFERENCES mortuary_cases(id),
 reservation_id INTEGER REFERENCES facility_reservations(id),
 start_at TEXT NOT NULL,
 end_at TEXT NOT NULL,
 shift_code TEXT NOT NULL DEFAULT '',
 source TEXT NOT NULL DEFAULT 'shift-log',
 status TEXT NOT NULL DEFAULT 'recorded' CHECK(status IN ('recorded','void')),
 idempotency_key TEXT NOT NULL UNIQUE,
 recorded_at TEXT NOT NULL,
 created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_furnace_runs_window ON furnace_runs(resource_code,start_at,end_at,status);
CREATE TABLE IF NOT EXISTS fuel_readings (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 meter_code TEXT NOT NULL,
 reading_at TEXT NOT NULL,
 value REAL NOT NULL,
 status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','void')),
 note TEXT NOT NULL DEFAULT '',
 idempotency_key TEXT NOT NULL UNIQUE,
 recorded_at TEXT NOT NULL,
 created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fuel_readings_meter ON fuel_readings(meter_code,reading_at,status);
CREATE TABLE IF NOT EXISTS purification_records (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 resource_code TEXT NOT NULL,
 start_at TEXT NOT NULL,
 end_at TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('normal','fault','bypass','maintenance')),
 idempotency_key TEXT NOT NULL UNIQUE,
 recorded_at TEXT NOT NULL,
 created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_purification_window ON purification_records(resource_code,start_at,end_at);
CREATE TABLE IF NOT EXISTS emission_fault_markers (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 resource_code TEXT NOT NULL,
 start_at TEXT NOT NULL,
 end_at TEXT NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('furnace_fault','purifier_fault')),
 note TEXT NOT NULL DEFAULT '',
 marked_by TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','resolved')),
 created_at TEXT NOT NULL,
 resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_fault_window ON emission_fault_markers(resource_code,start_at,end_at,status);
CREATE TABLE IF NOT EXISTS emission_batches (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 period TEXT NOT NULL,
 seq INTEGER NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('draft','issued','superseded','replay')),
 rule_version_id INTEGER NOT NULL REFERENCES emission_rule_versions(id),
 corrects_batch_id INTEGER REFERENCES emission_batches(id),
 is_replay INTEGER NOT NULL DEFAULT 0 CHECK(is_replay IN (0,1)),
 clock_fixed_at TEXT,
 input_summary_json TEXT NOT NULL,
 input_digest TEXT NOT NULL,
 totals_json TEXT NOT NULL DEFAULT '{}',
 created_by TEXT NOT NULL,
 created_at TEXT NOT NULL,
 issued_by TEXT,
 issued_at TEXT,
 superseded_reason TEXT NOT NULL DEFAULT '',
 UNIQUE(period,seq)
);
CREATE INDEX IF NOT EXISTS idx_emission_batches_period ON emission_batches(period,status,is_replay);
CREATE TABLE IF NOT EXISTS emission_batch_items (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 batch_id INTEGER NOT NULL REFERENCES emission_batches(id) ON DELETE CASCADE,
 item_key TEXT NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('run','reservation','reading')),
 ref_id INTEGER,
 run_id INTEGER,
 reservation_id INTEGER,
 case_id INTEGER,
 resource_code TEXT NOT NULL DEFAULT '',
 planned_start_at TEXT,
 planned_end_at TEXT,
 actual_start_at TEXT,
 actual_end_at TEXT,
 fuel_start_value REAL,
 fuel_end_value REAL,
 fuel_amount REAL,
 purification_efficiency REAL,
 emission_amount REAL,
 included INTEGER NOT NULL DEFAULT 1 CHECK(included IN (0,1)),
 revoked INTEGER NOT NULL DEFAULT 0 CHECK(revoked IN (0,1)),
 anomaly_codes_json TEXT NOT NULL DEFAULT '[]',
 detail_json TEXT NOT NULL DEFAULT '{}',
 UNIQUE(batch_id,item_key)
);
CREATE TABLE IF NOT EXISTS emission_batch_anomalies (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 batch_id INTEGER NOT NULL REFERENCES emission_batches(id) ON DELETE CASCADE,
 item_key TEXT,
 code TEXT NOT NULL,
 severity TEXT NOT NULL CHECK(severity IN ('error','warning','info')),
 message TEXT NOT NULL,
 refs_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_emission_anomalies_batch ON emission_batch_anomalies(batch_id,id);
CREATE TABLE IF NOT EXISTS emission_batch_deltas (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 batch_id INTEGER NOT NULL REFERENCES emission_batches(id) ON DELETE CASCADE,
 item_key TEXT NOT NULL,
 field TEXT NOT NULL,
 before_value TEXT,
 after_value TEXT,
 reason TEXT NOT NULL,
 UNIQUE(batch_id,item_key,field)
);
CREATE TABLE IF NOT EXISTS emission_case_revocations (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 case_id INTEGER NOT NULL UNIQUE,
 reason TEXT NOT NULL,
 revoked_by TEXT NOT NULL,
 created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS emission_impacts (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 trigger_type TEXT NOT NULL CHECK(trigger_type IN ('case_revoked','equipment_fault')),
 trigger_id INTEGER NOT NULL,
 batch_id INTEGER NOT NULL REFERENCES emission_batches(id),
 item_key TEXT NOT NULL,
 reason TEXT NOT NULL,
 detail_json TEXT NOT NULL DEFAULT '{}',
 status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','resolved')),
 created_by TEXT NOT NULL,
 created_at TEXT NOT NULL,
 resolved_at TEXT,
 resolution_note TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_emission_impacts_batch ON emission_impacts(batch_id,id);
CREATE INDEX IF NOT EXISTS idx_emission_impacts_open ON emission_impacts(status,trigger_type,id);
'''


DEFAULT_RULES: dict[str, Any] = {
    "cross_month_policy": "start_month",
    "revoked_case_policy": "exclude",
    "emission_factor": 2.05,
    "fuel_tie_break": "latest_recorded",
    "purification_states": {"normal": 0.9, "maintenance": 0.5, "fault": 0.0, "bypass": 0.0},
    "uncovered_purification_efficiency": 0.0,
    "meter_resource_map": {},
}


class EmissionsRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def ensure_schema(self) -> None:
        self.connection.executescript(SCHEMA)

    @staticmethod
    def one(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return None if row is None else dict(row)

    @staticmethod
    def rows(sql: str, params: list[Any] | tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        raise RuntimeError("use connection directly")

    # ---- rule versions -------------------------------------------------
    def create_rule_version(self, rules: dict[str, Any], digest: str, name: str, description: str, actor: str, now: str) -> dict[str, Any]:
        version = int(self.connection.execute("SELECT COALESCE(MAX(version),0)+1 FROM emission_rule_versions").fetchone()[0])
        cursor = self.connection.execute(
            "INSERT INTO emission_rule_versions(version,name,rules_json,rules_digest,status,description,created_by,created_at) VALUES(?,?,?,?, 'active',?,?,?)",
            (version, name, json.dumps(rules, ensure_ascii=False, sort_keys=True), digest, description, actor, now),
        )
        return self.rule_version(int(cursor.lastrowid)) or {}

    def rule_version(self, rule_id: int) -> dict[str, Any] | None:
        return self.one(self.connection.execute("SELECT * FROM emission_rule_versions WHERE id=?", (rule_id,)).fetchone())

    def rule_version_by_version(self, version: int) -> dict[str, Any] | None:
        return self.one(self.connection.execute("SELECT * FROM emission_rule_versions WHERE version=?", (version,)).fetchone())

    def active_rule_version(self) -> dict[str, Any] | None:
        return self.one(self.connection.execute("SELECT * FROM emission_rule_versions WHERE status='active' ORDER BY version DESC LIMIT 1").fetchone())

    def list_rule_versions(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute("SELECT * FROM emission_rule_versions ORDER BY version DESC").fetchall()]

    def retire_rule_versions_other_than(self, keep_id: int) -> None:
        self.connection.execute("UPDATE emission_rule_versions SET status='retired' WHERE id<>? AND status='active'", (keep_id,))

    # ---- source records --------------------------------------------------
    def run_by_key(self, key: str) -> dict[str, Any] | None:
        return self.one(self.connection.execute("SELECT * FROM furnace_runs WHERE idempotency_key=?", (key,)).fetchone())

    def create_run(self, values: dict[str, Any], now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            "INSERT INTO furnace_runs(resource_code,case_id,reservation_id,start_at,end_at,shift_code,source,status,idempotency_key,recorded_at,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (values["resource_code"], values.get("case_id"), values.get("reservation_id"), values["start_at"], values["end_at"], values.get("shift_code", ""), values.get("source", "shift-log"), "recorded", values["idempotency_key"], values["recorded_at"], now),
        )
        return self.one(self.connection.execute("SELECT * FROM furnace_runs WHERE id=?", (cursor.lastrowid,)).fetchone()) or {}

    def list_runs(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute("SELECT * FROM furnace_runs WHERE status='recorded' ORDER BY start_at,id").fetchall()]

    def reading_by_key(self, key: str) -> dict[str, Any] | None:
        return self.one(self.connection.execute("SELECT * FROM fuel_readings WHERE idempotency_key=?", (key,)).fetchone())

    def create_reading(self, values: dict[str, Any], now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            "INSERT INTO fuel_readings(meter_code,reading_at,value,status,note,idempotency_key,recorded_at,created_at) VALUES(?,?,?, 'active',?,?,?,?)",
            (values["meter_code"], values["reading_at"], values["value"], values.get("note", ""), values["idempotency_key"], values["recorded_at"], now),
        )
        return self.one(self.connection.execute("SELECT * FROM fuel_readings WHERE id=?", (cursor.lastrowid,)).fetchone()) or {}

    def list_readings(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute("SELECT * FROM fuel_readings WHERE status='active' ORDER BY reading_at,id").fetchall()]

    def purification_by_key(self, key: str) -> dict[str, Any] | None:
        return self.one(self.connection.execute("SELECT * FROM purification_records WHERE idempotency_key=?", (key,)).fetchone())

    def create_purification(self, values: dict[str, Any], now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            "INSERT INTO purification_records(resource_code,start_at,end_at,state,idempotency_key,recorded_at,created_at) VALUES(?,?,?,?,?,?,?)",
            (values["resource_code"], values["start_at"], values["end_at"], values["state"], values["idempotency_key"], values["recorded_at"], now),
        )
        return self.one(self.connection.execute("SELECT * FROM purification_records WHERE id=?", (cursor.lastrowid,)).fetchone()) or {}

    def list_purification(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute("SELECT * FROM purification_records ORDER BY start_at,id").fetchall()]

    def create_fault_marker(self, values: dict[str, Any], now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            "INSERT INTO emission_fault_markers(resource_code,start_at,end_at,kind,note,marked_by,status,created_at) VALUES(?,?,?,?,?,?,'open',?)",
            (values["resource_code"], values["start_at"], values["end_at"], values["kind"], values.get("note", ""), values["marked_by"], now),
        )
        return self.one(self.connection.execute("SELECT * FROM emission_fault_markers WHERE id=?", (cursor.lastrowid,)).fetchone()) or {}

    def list_fault_markers(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute("SELECT * FROM emission_fault_markers ORDER BY start_at,id").fetchall()]

    def revoke_case(self, case_id: int, reason: str, actor: str, now: str) -> None:
        self.connection.execute(
            "INSERT INTO emission_case_revocations(case_id,reason,revoked_by,created_at) VALUES(?,?,?,?)",
            (case_id, reason, actor, now),
        )

    def case_revocation(self, case_id: int) -> dict[str, Any] | None:
        return self.one(self.connection.execute("SELECT * FROM emission_case_revocations WHERE case_id=?", (case_id,)).fetchone())

    def revoked_case_ids(self) -> set[int]:
        return {int(row[0]) for row in self.connection.execute("SELECT case_id FROM emission_case_revocations").fetchall()}

    # ---- mortuary cross reads -------------------------------------------
    def cremator_resources(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute("SELECT * FROM facility_resources WHERE kind='cremator' AND active=1 ORDER BY code").fetchall()]

    def cremator_reservations(self) -> list[dict[str, Any]]:
        sql = ("SELECT r.*, f.code AS resource_code FROM facility_reservations r "
               "JOIN facility_resources f ON f.id=r.resource_id "
               "WHERE f.kind='cremator' AND r.status='confirmed' ORDER BY r.start_at,r.id")
        return [dict(row) for row in self.connection.execute(sql).fetchall()]

    def case(self, case_id: int) -> dict[str, Any] | None:
        return self.one(self.connection.execute("SELECT * FROM mortuary_cases WHERE id=?", (case_id,)).fetchone())

    def event(self, aggregate_id: int, event_type: str, actor: str, payload: dict[str, Any], now: str) -> None:
        self.connection.execute(
            "INSERT INTO mortuary_events(aggregate_type,aggregate_id,event_type,actor,payload_json,created_at) VALUES('case',?,?,?,?,?)",
            (str(aggregate_id), event_type, actor, json.dumps(payload, ensure_ascii=False, sort_keys=True), now),
        )

    # ---- batches ---------------------------------------------------------
    def create_batch(self, values: dict[str, Any]) -> int:
        cursor = self.connection.execute(
            "INSERT INTO emission_batches(period,seq,status,rule_version_id,corrects_batch_id,is_replay,clock_fixed_at,input_summary_json,input_digest,totals_json,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (values["period"], values["seq"], values["status"], values["rule_version_id"], values.get("corrects_batch_id"), 1 if values.get("is_replay") else 0, values.get("clock_fixed_at"), json.dumps(values["input_summary"], ensure_ascii=False, sort_keys=True), values["input_digest"], json.dumps(values.get("totals", {}), ensure_ascii=False, sort_keys=True), values["created_by"], values["created_at"]),
        )
        return int(cursor.lastrowid)

    def batch(self, batch_id: int) -> dict[str, Any] | None:
        return self.one(self.connection.execute("SELECT * FROM emission_batches WHERE id=?", (batch_id,)).fetchone())

    def list_batches(self, period: str | None = None) -> list[dict[str, Any]]:
        if period:
            rows = self.connection.execute("SELECT * FROM emission_batches WHERE period=? ORDER BY seq", (period,)).fetchall()
        else:
            rows = self.connection.execute("SELECT * FROM emission_batches ORDER BY period,seq").fetchall()
        return [dict(row) for row in rows]

    def open_draft(self, period: str) -> dict[str, Any] | None:
        return self.one(self.connection.execute("SELECT * FROM emission_batches WHERE period=? AND status='draft' AND is_replay=0 ORDER BY seq DESC LIMIT 1", (period,)).fetchone())

    def latest_issued(self, period: str) -> dict[str, Any] | None:
        return self.one(self.connection.execute("SELECT * FROM emission_batches WHERE period=? AND status='issued' ORDER BY seq DESC LIMIT 1", (period,)).fetchone())

    def latest_version_digest(self, period: str) -> dict[str, Any] | None:
        return self.one(self.connection.execute("SELECT * FROM emission_batches WHERE period=? AND is_replay=0 ORDER BY seq DESC LIMIT 1", (period,)).fetchone())

    def next_seq(self, period: str) -> int:
        return int(self.connection.execute("SELECT COALESCE(MAX(seq),0)+1 FROM emission_batches WHERE period=?", (period,)).fetchone()[0])

    def clear_batch_children(self, batch_id: int) -> None:
        self.connection.execute("DELETE FROM emission_batch_items WHERE batch_id=?", (batch_id,))
        self.connection.execute("DELETE FROM emission_batch_anomalies WHERE batch_id=?", (batch_id,))
        self.connection.execute("DELETE FROM emission_batch_deltas WHERE batch_id=?", (batch_id,))

    def update_batch_snapshot(self, batch_id: int, rule_version_id: int, clock_fixed_at: str | None, input_summary: dict[str, Any], input_digest: str, totals: dict[str, Any], corrects_batch_id: int | None) -> None:
        self.connection.execute(
            "UPDATE emission_batches SET rule_version_id=?,clock_fixed_at=?,input_summary_json=?,input_digest=?,totals_json=?,corrects_batch_id=? WHERE id=?",
            (rule_version_id, clock_fixed_at, json.dumps(input_summary, ensure_ascii=False, sort_keys=True), input_digest, json.dumps(totals, ensure_ascii=False, sort_keys=True), corrects_batch_id, batch_id),
        )

    def add_item(self, batch_id: int, item: dict[str, Any]) -> None:
        self.connection.execute(
            "INSERT INTO emission_batch_items(batch_id,item_key,kind,ref_id,run_id,reservation_id,case_id,resource_code,planned_start_at,planned_end_at,actual_start_at,actual_end_at,fuel_start_value,fuel_end_value,fuel_amount,purification_efficiency,emission_amount,included,revoked,anomaly_codes_json,detail_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (batch_id, item["item_key"], item["kind"], item.get("ref_id"), item.get("run_id"), item.get("reservation_id"), item.get("case_id"), item.get("resource_code", ""), item.get("planned_start_at"), item.get("planned_end_at"), item.get("actual_start_at"), item.get("actual_end_at"), item.get("fuel_start_value"), item.get("fuel_end_value"), item.get("fuel_amount"), item.get("purification_efficiency"), item.get("emission_amount"), 1 if item.get("included", True) else 0, 1 if item.get("revoked") else 0, json.dumps(item.get("anomaly_codes", []), ensure_ascii=False), json.dumps(item.get("detail", {}), ensure_ascii=False, sort_keys=True)),
        )

    def items(self, batch_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM emission_batch_items WHERE batch_id=? ORDER BY id", (batch_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["anomaly_codes"] = json.loads(item.pop("anomaly_codes_json"))
            item["detail"] = json.loads(item.pop("detail_json"))
            result.append(item)
        return result

    def add_anomaly(self, batch_id: int, item_key: str | None, code: str, severity: str, message: str, refs: dict[str, Any]) -> None:
        self.connection.execute(
            "INSERT INTO emission_batch_anomalies(batch_id,item_key,code,severity,message,refs_json) VALUES(?,?,?,?,?,?)",
            (batch_id, item_key, code, severity, message, json.dumps(refs, ensure_ascii=False, sort_keys=True)),
        )

    def anomalies(self, batch_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM emission_batch_anomalies WHERE batch_id=? ORDER BY id", (batch_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["refs"] = json.loads(item.pop("refs_json"))
            result.append(item)
        return result

    def add_delta(self, batch_id: int, item_key: str, field: str, before: Any, after: Any, reason: str) -> None:
        self.connection.execute(
            "INSERT INTO emission_batch_deltas(batch_id,item_key,field,before_value,after_value,reason) VALUES(?,?,?,?,?,?)",
            (batch_id, item_key, field, None if before is None else str(before), None if after is None else str(after), reason),
        )

    def deltas(self, batch_id: int) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute("SELECT * FROM emission_batch_deltas WHERE batch_id=? ORDER BY id", (batch_id,)).fetchall()]

    def mark_issued(self, batch_id: int, actor: str, now: str) -> None:
        self.connection.execute("UPDATE emission_batches SET status='issued',issued_by=?,issued_at=? WHERE id=?", (actor, now, batch_id))

    def mark_superseded(self, batch_id: int, reason: str) -> None:
        self.connection.execute("UPDATE emission_batches SET status='superseded',superseded_reason=? WHERE id=?", (reason, batch_id))

    def issued_batches(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute("SELECT * FROM emission_batches WHERE status='issued' ORDER BY id").fetchall()]

    def issued_items_case(self, case_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT b.id AS batch_id,b.period,b.seq,i.item_key,i.resource_code,i.actual_start_at,i.actual_end_at "
            "FROM emission_batch_items i JOIN emission_batches b ON b.id=i.batch_id "
            "WHERE b.status='issued' AND i.case_id=? ORDER BY b.id,i.id", (case_id,)).fetchall()
        return [dict(row) for row in rows]

    def issued_items_overlap(self, resource_code: str, start_at: str, end_at: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT b.id AS batch_id,b.period,b.seq,i.item_key,i.case_id,i.actual_start_at,i.actual_end_at "
            "FROM emission_batch_items i JOIN emission_batches b ON b.id=i.batch_id "
            "WHERE b.status='issued' AND i.resource_code=? AND i.actual_start_at IS NOT NULL "
            "AND i.actual_start_at<? AND i.actual_end_at>? ORDER BY b.id,i.id",
            (resource_code, end_at, start_at)).fetchall()
        return [dict(row) for row in rows]

    def add_impact(self, values: dict[str, Any], now: str) -> int:
        cursor = self.connection.execute(
            "INSERT INTO emission_impacts(trigger_type,trigger_id,batch_id,item_key,reason,detail_json,status,created_by,created_at) VALUES(?,?,?,?,?,?,'open',?,?)",
            (values["trigger_type"], values["trigger_id"], values["batch_id"], values["item_key"], values["reason"], json.dumps(values.get("detail", {}), ensure_ascii=False, sort_keys=True), values["created_by"], now),
        )
        return int(cursor.lastrowid)

    def impacts_for_batch(self, batch_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM emission_impacts WHERE batch_id=? ORDER BY id", (batch_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item.pop("detail_json"))
            result.append(item)
        return result

    def impact(self, impact_id: int) -> dict[str, Any] | None:
        return self.one(self.connection.execute("SELECT * FROM emission_impacts WHERE id=?", (impact_id,)).fetchone())

    def resolve_impact(self, impact_id: int, note: str, now: str) -> None:
        self.connection.execute("UPDATE emission_impacts SET status='resolved',resolution_note=?,resolved_at=? WHERE id=?", (note, now, impact_id))

    def list_impacts(self, status: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM emission_impacts"
        params: tuple[Any, ...] = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY id"
        rows = self.connection.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item.pop("detail_json"))
            result.append(item)
        return result
