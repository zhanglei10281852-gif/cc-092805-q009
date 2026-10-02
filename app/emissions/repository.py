from __future__ import annotations

import json
import sqlite3
from typing import Any


SCHEMA = r'''
CREATE TABLE IF NOT EXISTS emission_cremators (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    site_code TEXT NOT NULL,
    meter_code TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS emission_calibration_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL,
    version INTEGER NOT NULL,
    name TEXT NOT NULL,
    fuel_factor REAL NOT NULL CHECK(fuel_factor > 0),
    purifier_uplift REAL NOT NULL DEFAULT 0 CHECK(purifier_uplift >= 0),
    meter_tolerance REAL NOT NULL DEFAULT 0 CHECK(meter_tolerance >= 0),
    max_run_minutes INTEGER NOT NULL DEFAULT 600 CHECK(max_run_minutes > 0),
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(code, version)
);

CREATE TABLE IF NOT EXISTS emission_fuel_readings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    meter_code TEXT NOT NULL,
    read_at TEXT NOT NULL,
    reading REAL NOT NULL,
    idempotency_key TEXT NOT NULL,
    batch_key TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active'
        CHECK(status IN ('active','revoked','superseded')),
    revoked_reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(meter_code, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_fuel_reading_clock ON emission_fuel_readings(meter_code,read_at,status);

CREATE TABLE IF NOT EXISTS emission_purifier_states (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cremator_code TEXT NOT NULL,
    start_at TEXT NOT NULL,
    end_at TEXT,
    state TEXT NOT NULL CHECK(state IN ('normal','bypassed','fault')),
    note TEXT NOT NULL DEFAULT '',
    idempotency_key TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK(status IN ('active','revoked')),
    revoked_reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(cremator_code, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_purifier_window ON emission_purifier_states(cremator_code,start_at,end_at,status);

CREATE TABLE IF NOT EXISTS emission_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cremator_code TEXT NOT NULL,
    case_ref TEXT,
    reservation_id INTEGER,
    actual_start_at TEXT NOT NULL,
    actual_end_at TEXT,
    shift_code TEXT NOT NULL DEFAULT '',
    idempotency_key TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK(status IN ('active','revoked')),
    revoked_reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(cremator_code, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_run_window ON emission_runs(cremator_code,actual_start_at,actual_end_at,status);

CREATE TABLE IF NOT EXISTS emission_equipment_flags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    equipment_code TEXT NOT NULL,
    flag_type TEXT NOT NULL CHECK(flag_type IN ('fault','maintenance')),
    start_at TEXT NOT NULL,
    end_at TEXT,
    reason TEXT NOT NULL,
    raised_by TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','resolved')),
    idempotency_key TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_equipment_flag_window
    ON emission_equipment_flags(equipment_code,start_at,end_at,status);

CREATE TABLE IF NOT EXISTS emission_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    period_month TEXT NOT NULL,
    version_no INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'computed'
        CHECK(status IN ('computed','issued','superseded')),
    rule_code TEXT NOT NULL,
    rule_version INTEGER NOT NULL,
    clock_fixed_at TEXT,
    input_digest TEXT NOT NULL,
    input_snapshot_json TEXT NOT NULL DEFAULT '{}',
    result_digest TEXT NOT NULL,
    totals_json TEXT NOT NULL,
    anomaly_count INTEGER NOT NULL,
    correction_of_id INTEGER REFERENCES emission_reports(id),
    reason TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    issued_at TEXT,
    issued_by TEXT,
    UNIQUE(period_month, version_no)
);
CREATE INDEX IF NOT EXISTS idx_emission_report_chain ON emission_reports(correction_of_id);

CREATE TABLE IF NOT EXISTS emission_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id INTEGER NOT NULL REFERENCES emission_reports(id),
    batch_key TEXT NOT NULL,
    cremator_code TEXT NOT NULL,
    run_id INTEGER,
    case_ref TEXT,
    month TEXT NOT NULL,
    reservation_window_json TEXT NOT NULL DEFAULT '{}',
    actual_window_json TEXT NOT NULL,
    fuel_allocation_json TEXT NOT NULL DEFAULT '{}',
    purifier_state TEXT NOT NULL DEFAULT '',
    calibration_code TEXT NOT NULL,
    fuel_used REAL NOT NULL DEFAULT 0,
    estimated_emission REAL NOT NULL DEFAULT 0,
    anomalies_json TEXT NOT NULL DEFAULT '[]',
    UNIQUE(report_id, batch_key)
);
CREATE INDEX IF NOT EXISTS idx_emission_batch_run ON emission_batches(run_id);

CREATE TABLE IF NOT EXISTS emission_unattributed_readings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id INTEGER NOT NULL REFERENCES emission_reports(id),
    reading_id INTEGER NOT NULL REFERENCES emission_fuel_readings(id),
    meter_code TEXT NOT NULL,
    read_at TEXT NOT NULL,
    reading REAL NOT NULL,
    reason TEXT NOT NULL,
    UNIQUE(report_id, reading_id)
);

CREATE TABLE IF NOT EXISTS emission_batch_readings (
    batch_id INTEGER NOT NULL REFERENCES emission_batches(id) ON DELETE CASCADE,
    reading_id INTEGER NOT NULL REFERENCES emission_fuel_readings(id),
    PRIMARY KEY(batch_id, reading_id)
);
CREATE INDEX IF NOT EXISTS idx_emission_batch_reading ON emission_batch_readings(reading_id);

CREATE TABLE IF NOT EXISTS emission_changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id INTEGER NOT NULL REFERENCES emission_reports(id),
    batch_key TEXT,
    run_id INTEGER,
    reading_id INTEGER,
    change_type TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_emission_change_report ON emission_changes(report_id,id);

CREATE TABLE IF NOT EXISTS emission_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    aggregate_type TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_emission_event ON emission_events(aggregate_type,aggregate_id,id);

CREATE TABLE IF NOT EXISTS emission_impacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_type TEXT NOT NULL CHECK(subject_type IN ('reading','run','case','flag')),
    subject_id TEXT NOT NULL,
    action TEXT NOT NULL,
    reason TEXT NOT NULL,
    affected_json TEXT NOT NULL,
    acted_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_emission_impact_subject ON emission_impacts(subject_type,subject_id,id);

CREATE TABLE IF NOT EXISTS emission_case_revocations (
    case_ref TEXT NOT NULL PRIMARY KEY,
    reason TEXT NOT NULL,
    acted_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
'''


class EmissionsRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def ensure_schema(self) -> None:
        self.connection.executescript(SCHEMA)

    @staticmethod
    def one(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return None if row is None else dict(row)

    def event(self, kind: str, aggregate_id: int | str, event_type: str, actor: str, payload: dict[str, Any], now: str) -> None:
        self.connection.execute(
            "INSERT INTO emission_events(aggregate_type,aggregate_id,event_type,actor,payload_json,created_at) VALUES(?,?,?,?,?,?)",
            (kind, str(aggregate_id), event_type, actor,
             json.dumps(payload, ensure_ascii=False, sort_keys=True), now),
        )

    # -- 主数据 ----------------------------------------------------------

    def cremator(self, code: str) -> dict[str, Any] | None:
        return self.one(self.connection.execute(
            "SELECT * FROM emission_cremators WHERE code=?", (code,)).fetchone())

    def create_cremator(self, values: dict[str, Any], now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            "INSERT INTO emission_cremators(code,name,site_code,meter_code,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (values["code"], values["name"], values["site_code"], values.get("meter_code", ""), now, now))
        return self.one(self.connection.execute(
            "SELECT * FROM emission_cremators WHERE id=?", (cursor.lastrowid,)).fetchone()) or {}

    def list_cremators(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM emission_cremators ORDER BY code").fetchall()]

    def rule(self, rule_id: int) -> dict[str, Any] | None:
        return self.one(self.connection.execute(
            "SELECT * FROM emission_calibration_rules WHERE id=?", (rule_id,)).fetchone())

    def rule_code(self, code: str) -> dict[str, Any] | None:
        return self.one(self.connection.execute(
            "SELECT * FROM emission_calibration_rules WHERE code=? ORDER BY version DESC LIMIT 1", (code,)).fetchone())

    def rule_version(self, code: str, version: int) -> dict[str, Any] | None:
        return self.one(self.connection.execute(
            "SELECT * FROM emission_calibration_rules WHERE code=? AND version=?", (code, version)).fetchone())

    def create_rule(self, values: dict[str, Any], now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            "INSERT INTO emission_calibration_rules(code,version,name,fuel_factor,purifier_uplift,meter_tolerance,max_run_minutes,valid_from,valid_to,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (values["code"], values["version"], values["name"], values["fuel_factor"], values["purifier_uplift"],
             values["meter_tolerance"], values["max_run_minutes"], values["valid_from"],
             values.get("valid_to"), values["created_by"], now))
        return self.one(self.connection.execute(
            "SELECT * FROM emission_calibration_rules WHERE id=?", (cursor.lastrowid,)).fetchone()) or {}

    def next_rule_version(self, code: str) -> int:
        row = self.connection.execute(
            "SELECT COALESCE(MAX(version),0)+1 FROM emission_calibration_rules WHERE code=?", (code,)).fetchone()
        return int(row[0])

    def next_rule_version(self, code: str) -> int:
        row = self.connection.execute(
            "SELECT COALESCE(MAX(version),0)+1 FROM emission_calibration_rules WHERE code=?", (code,)).fetchone()
        return int(row[0])

    # -- 输入数据 ----------------------------------------------------------

    def reading_key(self, meter_code: str, key: str) -> dict[str, Any] | None:
        return self.one(self.connection.execute(
            "SELECT * FROM emission_fuel_readings WHERE meter_code=? AND idempotency_key=?",
            (meter_code, key)).fetchone())

    def create_reading(self, values: dict[str, Any], now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            "INSERT INTO emission_fuel_readings(meter_code,read_at,reading,batch_key,idempotency_key,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
            (values["meter_code"], values["read_at"], values["reading"],
             values.get("batch_key", ""), values["idempotency_key"], now, now))
        return self.one(self.connection.execute(
            "SELECT * FROM emission_fuel_readings WHERE id=?", (cursor.lastrowid,)).fetchone()) or {}

    def active_readings(self, meter_code: str) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM emission_fuel_readings WHERE meter_code=? AND status='active' ORDER BY read_at,id",
            (meter_code,)).fetchall()]

    def purifier_key(self, cremator_code: str, key: str) -> dict[str, Any] | None:
        return self.one(self.connection.execute(
            "SELECT * FROM emission_purifier_states WHERE cremator_code=? AND idempotency_key=?",
            (cremator_code, key)).fetchone())

    def create_purifier(self, values: dict[str, Any], now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            "INSERT INTO emission_purifier_states(cremator_code,start_at,end_at,state,note,idempotency_key,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (values["cremator_code"], values["start_at"], values.get("end_at"), values["state"],
             values.get("note", ""), values["idempotency_key"], now, now))
        return self.one(self.connection.execute(
            "SELECT * FROM emission_purifier_states WHERE id=?", (cursor.lastrowid,)).fetchone()) or {}

    def active_purifier_windows(self, cremator_code: str) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM emission_purifier_states WHERE cremator_code=? AND status='active' ORDER BY start_at,id",
            (cremator_code,)).fetchall()]

    def run_key(self, cremator_code: str, key: str) -> dict[str, Any] | None:
        return self.one(self.connection.execute(
            "SELECT * FROM emission_runs WHERE cremator_code=? AND idempotency_key=?",
            (cremator_code, key)).fetchone())

    def create_run(self, values: dict[str, Any], now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            "INSERT INTO emission_runs(cremator_code,case_ref,reservation_id,actual_start_at,actual_end_at,shift_code,idempotency_key,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (values["cremator_code"], values.get("case_ref"), values.get("reservation_id"),
             values["actual_start_at"], values.get("actual_end_at"), values.get("shift_code", ""),
             values["idempotency_key"], now, now))
        return self.one(self.connection.execute(
            "SELECT * FROM emission_runs WHERE id=?", (cursor.lastrowid,)).fetchone()) or {}

    def active_runs(self, cremator_code: str | None = None) -> list[dict[str, Any]]:
        if cremator_code:
            rows = self.connection.execute(
                "SELECT * FROM emission_runs WHERE cremator_code=? AND status='active' ORDER BY actual_start_at,id",
                (cremator_code,)).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT * FROM emission_runs WHERE status='active' ORDER BY actual_start_at,id").fetchall()
        return [dict(row) for row in rows]

    def reservation(self, reservation_id: int) -> dict[str, Any] | None:
        return self.one(self.connection.execute(
            "SELECT r.*, f.code resource_code FROM facility_reservations r "
            "JOIN facility_resources f ON f.id=r.resource_id WHERE r.id=?",
            (reservation_id,)).fetchone())

    def flag_key(self, key: str) -> dict[str, Any] | None:
        return self.one(self.connection.execute(
            "SELECT * FROM emission_equipment_flags WHERE idempotency_key=?", (key,)).fetchone())

    def create_flag(self, values: dict[str, Any], now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            "INSERT INTO emission_equipment_flags(equipment_code,flag_type,start_at,end_at,reason,raised_by,idempotency_key,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (values["equipment_code"], values["flag_type"], values["start_at"], values.get("end_at"),
             values["reason"], values["raised_by"], values["idempotency_key"], now, now))
        return self.one(self.connection.execute(
            "SELECT * FROM emission_equipment_flags WHERE id=?", (cursor.lastrowid,)).fetchone()) or {}

    def active_flags(self, equipment_code: str) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM emission_equipment_flags WHERE equipment_code=? AND status='active' ORDER BY start_at,id",
            (equipment_code,)).fetchall()]

    # -- 撤销 --------------------------------------------------------------

    def reading(self, reading_id: int) -> dict[str, Any] | None:
        return self.one(self.connection.execute(
            "SELECT * FROM emission_fuel_readings WHERE id=?", (reading_id,)).fetchone())

    def run(self, run_id: int) -> dict[str, Any] | None:
        return self.one(self.connection.execute(
            "SELECT * FROM emission_runs WHERE id=?", (run_id,)).fetchone())

    def flag(self, flag_id: int) -> dict[str, Any] | None:
        return self.one(self.connection.execute(
            "SELECT * FROM emission_equipment_flags WHERE id=?", (flag_id,)).fetchone())

    def revoke_reading(self, reading_id: int, reason: str, now: str) -> None:
        self.connection.execute(
            "UPDATE emission_fuel_readings SET status='revoked',revoked_reason=?,updated_at=? WHERE id=?",
            (reason, now, reading_id))

    def revoke_run(self, run_id: int, reason: str, now: str) -> None:
        self.connection.execute(
            "UPDATE emission_runs SET status='revoked',revoked_reason=?,updated_at=? WHERE id=?",
            (reason, now, run_id))

    def resolve_flag(self, flag_id: int, now: str) -> None:
        self.connection.execute(
            "UPDATE emission_equipment_flags SET status='resolved',end_at=COALESCE(end_at,?),updated_at=? WHERE id=?",
            (now, now, flag_id))

    # -- 报表 --------------------------------------------------------------

    def report(self, report_id: int) -> dict[str, Any] | None:
        return self.one(self.connection.execute(
            "SELECT * FROM emission_reports WHERE id=?", (report_id,)).fetchone())

    def report_version(self, period_month: str, version_no: int) -> dict[str, Any] | None:
        return self.one(self.connection.execute(
            "SELECT * FROM emission_reports WHERE period_month=? AND version_no=?",
            (period_month, version_no)).fetchone())

    def latest_report(self, period_month: str) -> dict[str, Any] | None:
        return self.one(self.connection.execute(
            "SELECT * FROM emission_reports WHERE period_month=? ORDER BY version_no DESC LIMIT 1",
            (period_month,)).fetchone())

    def latest_official_report(self, period_month: str) -> dict[str, Any] | None:
        """最新的非固定时钟重放版本（更正版差异链以此为基准）。"""
        return self.one(self.connection.execute(
            "SELECT * FROM emission_reports WHERE period_month=? AND clock_fixed_at IS NULL "
            "ORDER BY version_no DESC LIMIT 1", (period_month,)).fetchone())

    def find_report_by_input(self, period_month: str, rule_code: str,
                             input_digest: str, clock_fixed_at: str | None) -> dict[str, Any] | None:
        return self.one(self.connection.execute(
            "SELECT * FROM emission_reports WHERE period_month=? AND rule_code=? "
            "AND input_digest=? AND COALESCE(clock_fixed_at,'')=COALESCE(?,'') "
            "ORDER BY version_no DESC LIMIT 1",
            (period_month, rule_code, input_digest, clock_fixed_at)).fetchone())

    def list_reports(self, period_month: str | None = None) -> list[dict[str, Any]]:
        if period_month:
            rows = self.connection.execute(
                "SELECT * FROM emission_reports WHERE period_month=? ORDER BY version_no",
                (period_month,)).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT * FROM emission_reports ORDER BY period_month, version_no").fetchall()
        return [dict(row) for row in rows]

    def active_cremators(self, as_of: str | None = None) -> list[dict[str, Any]]:
        if as_of:
            rows = self.connection.execute(
                "SELECT * FROM emission_cremators WHERE active=1 AND created_at<=? ORDER BY code",
                (as_of,)).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT * FROM emission_cremators WHERE active=1 ORDER BY code").fetchall()
        return [dict(row) for row in rows]

    def reports_using_run(self, run_id: int) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute(
            "SELECT r.* FROM emission_reports r JOIN emission_batches b ON b.report_id=r.id "
            "WHERE b.run_id=? ORDER BY r.period_month, r.version_no", (run_id,)).fetchall()]

    def reports_using_reading(self, reading_id: int) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute(
            "SELECT r.* FROM emission_reports r WHERE r.id IN ("
            "SELECT br2.report_id FROM emission_batches br2 JOIN emission_batch_readings brq "
            "ON brq.batch_id=br2.id WHERE brq.reading_id=?) "
            "OR r.id IN (SELECT report_id FROM emission_unattributed_readings WHERE reading_id=?) "
            "ORDER BY r.period_month, r.version_no",
            (reading_id, reading_id)).fetchall()]

    def link_batch_reading(self, batch_id: int, reading_id: int) -> None:
        self.connection.execute(
            "INSERT OR IGNORE INTO emission_batch_readings(batch_id,reading_id) VALUES(?,?)",
            (batch_id, reading_id))

    def insert_report(self, values: dict[str, Any]) -> int:
        cursor = self.connection.execute(
            "INSERT INTO emission_reports(period_month,version_no,status,rule_code,rule_version,clock_fixed_at,input_digest,input_snapshot_json,result_digest,totals_json,anomaly_count,correction_of_id,reason,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (values["period_month"], values["version_no"], values.get("status", "computed"),
             values["rule_code"], values["rule_version"], values.get("clock_fixed_at"),
             values["input_digest"], values["input_snapshot_json"], values["result_digest"],
             values["totals_json"], values["anomaly_count"], values.get("correction_of_id"),
             values.get("reason", ""), values["created_by"], values["created_at"]))
        return int(cursor.lastrowid)

    def issue_report(self, report_id: int, actor: str, now: str) -> None:
        self.connection.execute(
            "UPDATE emission_reports SET status='issued',issued_at=?,issued_by=? WHERE id=?",
            (now, actor, report_id))

    def supersede_report(self, report_id: int) -> None:
        self.connection.execute(
            "UPDATE emission_reports SET status='superseded' WHERE id=? AND status='issued'",
            (report_id,))

    def insert_batch(self, values: dict[str, Any]) -> int:
        cursor = self.connection.execute(
            "INSERT INTO emission_batches(report_id,batch_key,cremator_code,run_id,case_ref,month,reservation_window_json,actual_window_json,fuel_allocation_json,purifier_state,calibration_code,fuel_used,estimated_emission,anomalies_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (values["report_id"], values["batch_key"], values["cremator_code"], values.get("run_id"),
             values.get("case_ref"), values["month"], values["reservation_window_json"],
             values["actual_window_json"], values["fuel_allocation_json"],
             values.get("purifier_state", ""), values["calibration_code"],
             values["fuel_used"], values["estimated_emission"], values["anomalies_json"]))
        return int(cursor.lastrowid)

    def insert_unattributed(self, values: dict[str, Any]) -> None:
        self.connection.execute(
            "INSERT INTO emission_unattributed_readings(report_id,reading_id,meter_code,read_at,reading,reason) VALUES(?,?,?,?,?,?)",
            (values["report_id"], values["reading_id"], values["meter_code"],
             values["read_at"], values["reading"], values["reason"]))

    def insert_change(self, values: dict[str, Any], now: str) -> None:
        self.connection.execute(
            "INSERT INTO emission_changes(report_id,batch_key,run_id,reading_id,change_type,detail_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (values["report_id"], values.get("batch_key"), values.get("run_id"),
             values.get("reading_id"), values["change_type"], values["detail_json"],
             now))

    def batches(self, report_id: int) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM emission_batches WHERE report_id=? ORDER BY id", (report_id,)).fetchall()]

    def unattributed(self, report_id: int) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM emission_unattributed_readings WHERE report_id=? ORDER BY id",
            (report_id,)).fetchall()]

    def changes(self, report_id: int) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM emission_changes WHERE report_id=? ORDER BY id", (report_id,)).fetchall()]

    def timeline(self, kind: str, aggregate_id: int | str) -> list[dict[str, Any]]:
        import json
        rows = self.connection.execute(
            "SELECT * FROM emission_events WHERE aggregate_type=? AND aggregate_id=? ORDER BY id",
            (kind, str(aggregate_id))).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            result.append(item)
        return result

    # -- 快照与重放 --------------------------------------------------------

    def list_rules(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM emission_calibration_rules ORDER BY code, version").fetchall()]

    def snapshot(self, window_start: str, window_end: str, as_of: str | None = None) -> dict[str, Any]:
        """取计算窗口内的输入快照。

        as_of 不为空时按固定时钟重放：只返回该时刻之前创建、且当时尚未
        被撤销的记录（撤销发生在 as_of 之后的仍视为有效）。
        """
        if as_of:
            visibility = "created_at<=? AND (status='active' OR updated_at>?)"
            params_read = [window_start, window_end, as_of, as_of]
            params_window = [window_end, window_start, as_of, as_of]
            params_case = [as_of]
        else:
            visibility = "status='active'"
            params_read = [window_start, window_end]
            params_window = [window_end, window_start]
            params_case = []
        readings = [dict(row) for row in self.connection.execute(
            "SELECT id,meter_code,read_at,reading FROM emission_fuel_readings "
            f"WHERE read_at>=? AND read_at<=? AND {visibility} ORDER BY read_at,id",
            params_read).fetchall()]
        purifier = [dict(row) for row in self.connection.execute(
            "SELECT id,cremator_code,start_at,end_at,state FROM emission_purifier_states "
            f"WHERE start_at<=? AND (end_at IS NULL OR end_at>=?) AND {visibility} ORDER BY start_at,id",
            params_window).fetchall()]
        runs = [dict(row) for row in self.connection.execute(
            "SELECT id,cremator_code,case_ref,reservation_id,actual_start_at,actual_end_at,shift_code "
            "FROM emission_runs "
            f"WHERE actual_start_at<? AND (actual_end_at IS NULL OR actual_end_at>?) AND {visibility} "
            "ORDER BY actual_start_at,id",
            params_window).fetchall()]
        flags = [dict(row) for row in self.connection.execute(
            "SELECT id,equipment_code,flag_type,start_at,end_at,reason FROM emission_equipment_flags "
            f"WHERE start_at<? AND (end_at IS NULL OR end_at>?) AND {visibility} ORDER BY start_at,id",
            params_window).fetchall()]
        revoked_cases = {row[0] for row in self.connection.execute(
            "SELECT case_ref FROM emission_case_revocations" +
            (" WHERE created_at<=?" if as_of else ""), params_case).fetchall()}
        # 撤销档案的炉次不参与计算（撤销后只能通过更正版反映差异）
        runs = [run for run in runs if run.get("case_ref") not in revoked_cases]
        reservation_ids = [run["reservation_id"] for run in runs if run.get("reservation_id")]
        reservations: list[dict[str, Any]] = []
        if reservation_ids:
            placeholders = ",".join("?" for _ in reservation_ids)
            reservation_sql = (
                "SELECT r.id,r.start_at,r.end_at,f.code resource_code FROM facility_reservations r "
                "JOIN facility_resources f ON f.id=r.resource_id "
                f"WHERE r.id IN ({placeholders}) AND r.status='confirmed'")
            if as_of:
                reservation_sql += " AND r.created_at<=?"
                reservations = [dict(row) for row in self.connection.execute(
                    reservation_sql, reservation_ids + [as_of]).fetchall()]
            else:
                reservations = [dict(row) for row in self.connection.execute(
                    reservation_sql, reservation_ids).fetchall()]
        rules = [dict(row) for row in self.connection.execute(
            "SELECT id,code,version,name,fuel_factor,purifier_uplift,meter_tolerance,max_run_minutes,valid_from,valid_to "
            "FROM emission_calibration_rules" +
            (" WHERE created_at<=?" if as_of else "") + " ORDER BY code, version",
            params_case).fetchall()]
        return {
            "readings": readings,
            "purifier": purifier,
            "runs": runs,
            "flags": flags,
            "reservations": reservations,
            "rules": rules,
        }

    # -- 受影响清单与档案撤销 ----------------------------------------------

    def insert_impact(self, values: dict[str, Any], now: str) -> int:
        cursor = self.connection.execute(
            "INSERT INTO emission_impacts(subject_type,subject_id,action,reason,affected_json,acted_by,created_at) VALUES(?,?,?,?,?,?,?)",
            (values["subject_type"], str(values["subject_id"]), values["action"],
             values["reason"], values["affected_json"], values["acted_by"], now))
        return int(cursor.lastrowid)

    def impacts(self, subject_type: str | None = None, subject_id: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM emission_impacts"
        clauses = []
        params: list[Any] = []
        if subject_type:
            clauses.append("subject_type=?")
            params.append(subject_type)
        if subject_id is not None:
            clauses.append("subject_id=?")
            params.append(str(subject_id))
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id DESC"
        return [dict(row) for row in self.connection.execute(sql, params).fetchall()]

    def impact(self, impact_id: int) -> dict[str, Any] | None:
        return self.one(self.connection.execute(
            "SELECT * FROM emission_impacts WHERE id=?", (impact_id,)).fetchone())

    def revoke_case(self, case_ref: str, reason: str, actor: str, now: str) -> None:
        self.connection.execute(
            "INSERT INTO emission_case_revocations(case_ref,reason,acted_by,created_at) VALUES(?,?,?,?)",
            (case_ref, reason, actor, now))

    def case_revoked(self, case_ref: str) -> dict[str, Any] | None:
        return self.one(self.connection.execute(
            "SELECT * FROM emission_case_revocations WHERE case_ref=?", (case_ref,)).fetchone())

    def active_runs_for_case(self, case_ref: str) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM emission_runs WHERE case_ref=? AND status='active' ORDER BY id",
            (case_ref,)).fetchall()]

