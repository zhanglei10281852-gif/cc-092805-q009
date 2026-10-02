from __future__ import annotations

import json

import pytest


def _rule(client, code: str = "RULE-A", factor: float = 2.0, uplift: float = 0.5) -> dict:
    response = client.post(
        "/api/emissions/calibration-rules?actor=env-admin",
        json={"code": code, "name": "燃油排放系数", "fuel_factor": factor,
              "purifier_uplift": uplift, "meter_tolerance": 180, "max_run_minutes": 600,
              "valid_from": "2026-01-01T00:00:00Z"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _cremator(client, code: str = "CR-1", meter: str = "M-1") -> dict:
    response = client.post(
        "/api/emissions/cremators?actor=device-admin",
        json={"code": code, "name": "一号火化炉", "site_code": "SITE-1", "meter_code": meter},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _reading(client, meter: str, read_at: str, reading: float, key: str) -> dict:
    response = client.post("/api/emissions/fuel-readings", json={
        "meter_code": meter, "read_at": read_at, "reading": reading,
        "idempotency_key": key, "recorded_by": "meter-upload"})
    assert response.status_code == 201, response.text
    return response.json()


def _purifier(client, cremator: str, start_at: str, end_at: str | None, state: str, key: str) -> dict:
    response = client.post("/api/emissions/purifier-states", json={
        "cremator_code": cremator, "start_at": start_at, "end_at": end_at,
        "state": state, "idempotency_key": key, "recorded_by": "purifier-scada"})
    assert response.status_code == 201, response.text
    return response.json()


def _run(client, cremator: str, start_at: str, end_at: str | None, key: str,
         case_ref: str | None = None, reservation_id: int | None = None) -> dict:
    response = client.post("/api/emissions/runs", json={
        "cremator_code": cremator, "actual_start_at": start_at, "actual_end_at": end_at,
        "case_ref": case_ref, "reservation_id": reservation_id,
        "shift_code": "A", "idempotency_key": key, "recorded_by": "shift-leader"})
    assert response.status_code == 201, response.text
    return response.json()


def _seed_normal_month(client, month: str = "2026-09") -> dict:
    _rule(client)
    _cremator(client)
    _purifier(client, "CR-1", f"{month[:4]}-{month[5:]}-01T00:00:00Z", None, "normal", "pur-key-0001")
    _reading(client, "M-1", "2026-09-10T09:50:00Z", 100.0, "read-key-0001")
    _reading(client, "M-1", "2026-09-10T12:10:00Z", 130.0, "read-key-0002")
    run = _run(client, "CR-1", "2026-09-10T10:00:00Z", "2026-09-10T12:00:00Z",
               "run-key-0001", case_ref="CASE-EM-001")
    return run


def test_batch_links_appointment_run_fuel_and_purifier(client):
    _rule(client)
    _cremator(client)
    _purifier(client, "CR-1", "2026-09-01T00:00:00Z", None, "normal", "pur-key-0001")
    _reading(client, "M-1", "2026-09-10T09:50:00Z", 100.0, "read-key-0001")
    _reading(client, "M-1", "2026-09-10T12:10:00Z", 130.0, "read-key-0002")
    # 关联真实火化预约
    case = client.post("/api/mortuary/cases?actor=intake", json={
        "external_ref": "CASE-EM-001", "decedent_name": "王安", "identity_number": None,
        "death_time": "2026-09-09T08:00:00Z", "received_from": "市一医院",
        "family_contact": "王家属", "family_phone": "13800000000"}).json()
    client.post("/api/mortuary/resources?actor=scheduler", json={
        "code": "CR-1", "name": "一号火化炉", "kind": "cremator", "site_code": "SITE-1",
        "capacity": 2, "attributes": {}})
    reservation = client.post("/api/mortuary/reservations", json={
        "resource_code": "CR-1", "case_id": case["id"],
        "start_at": "2026-09-10T10:00:00Z", "end_at": "2026-09-10T12:00:00Z",
        "purpose": "火化", "created_by": "scheduler", "idempotency_key": "res-key-0001"}).json()
    run = _run(client, "CR-1", "2026-09-10T10:00:00Z", "2026-09-10T12:00:00Z",
               "run-key-0002", case_ref="CASE-EM-001", reservation_id=reservation["id"])

    report = client.post(
        "/api/emissions/reports/compute?period_month=2026-09&rule_code=RULE-A&created_by=device-mgr",
    ).json()
    assert report["version_no"] == 1
    run_batch = next(b for b in report["batches"] if b["run_id"] == run["id"])
    # 插值：10:00 => 100+30*10/140≈102.143；12:00 => 130-30*10/140≈127.857；用量 25.714
    assert abs(run_batch["fuel_used"] - 25.714) < 0.01
    assert run_batch["estimated_emission"] == round(25.714 * 2.0, 3)
    assert run_batch["purifier_state"] == "normal"
    assert run_batch["calibration_code"] == "RULE-A"
    assert run_batch["reservation_window"]["resource_code"] == "CR-1"
    assert report["input_digest"] and report["result_digest"]
    assert report["totals"]["anomaly_count"] == 0
    # 同输入重算幂等，不产生新版本
    again = client.post(
        "/api/emissions/reports/compute?period_month=2026-09&rule_code=RULE-A&created_by=device-mgr").json()
    assert again["id"] == report["id"]


def test_detects_missing_overlap_unattributed_and_fault_anomalies(client):
    _rule(client)
    _cremator(client)
    # 1) 缺少净化状态与预约的炉次
    _reading(client, "M-1", "2026-09-02T08:00:00Z", 10.0, "read-key-0010")
    _reading(client, "M-1", "2026-09-02T09:00:00Z", 20.0, "read-key-0011")
    _run(client, "CR-1", "2026-09-02T08:00:00Z", "2026-09-02T09:00:00Z", "run-key-0010")
    # 2) 净化故障 + 设备故障标记
    _purifier(client, "CR-1", "2026-09-03T08:00:00Z", "2026-09-03T09:00:00Z", "fault", "pur-key-0010")
    client.post("/api/emissions/equipment-flags", json={
        "equipment_code": "CR-1", "flag_type": "fault",
        "start_at": "2026-09-03T08:30:00Z", "end_at": None, "reason": "燃烧器报警",
        "raised_by": "repair-chen", "idempotency_key": "flag-key-0001"})
    _reading(client, "M-1", "2026-09-03T08:00:00Z", 20.0, "read-key-0012")
    _reading(client, "M-1", "2026-09-03T09:00:00Z", 35.0, "read-key-0013")
    _run(client, "CR-1", "2026-09-03T08:00:00Z", "2026-09-03T09:00:00Z", "run-key-0011")
    # 3) 同炉重叠炉次
    _purifier(client, "CR-1", "2026-09-04T08:00:00Z", "2026-09-04T12:00:00Z", "normal", "pur-key-0011")
    _run(client, "CR-1", "2026-09-04T09:00:00Z", "2026-09-04T10:00:00Z", "run-key-0012")
    _run(client, "CR-1", "2026-09-04T09:30:00Z", "2026-09-04T10:30:00Z", "run-key-0013")
    # 4) 无法归属的读数
    _reading(client, "M-1", "2026-09-05T03:00:00Z", 99.0, "read-key-0014")
    # 5) 开口炉次
    _run(client, "CR-1", "2026-09-06T08:00:00Z", None, "run-key-0014")

    report = client.post(
        "/api/emissions/reports/compute?period_month=2026-09&rule_code=RULE-A&created_by=device-mgr").json()
    codes = sorted({a["code"] for b in report["batches"] for a in b["anomalies"]})
    assert "purifier_missing" in codes
    assert "reservation_missing" in codes
    assert "purifier_fault" in codes
    assert "equipment_flag" in codes
    assert "run_overlap" in codes
    assert "reading_unattributed" in codes
    assert "run_missing_end" in report["totals"]["anomaly_codes"]
    fault_batch = next(b for b in report["batches"]
                       if any(a["code"] == "purifier_fault" for a in b["anomalies"]))
    # 故障炉次排放按净化补偿系数上浮 15 * 2.0 * 1.5
    assert fault_batch["estimated_emission"] == round(15.0 * 2.0 * 1.5, 3)
    unattributed = next(b for b in report["batches"] if b["batch_key"].startswith("unattributed:"))
    assert unattributed["fuel_allocation"][0]["reading"] == 99.0


def test_issued_report_immutable_and_correction_explains_diff(client):
    _seed_normal_month(client)
    first = client.post(
        "/api/emissions/reports/compute?period_month=2026-09&rule_code=RULE-A&created_by=device-mgr").json()
    issued = client.post(f"/api/emissions/reports/{first['id']}/issue?actor=regulator-liu")
    assert issued.status_code == 200 and issued.json()["status"] == "issued"

    # 已签发后直接重算被拒绝
    blocked = client.post(
        "/api/emissions/reports/compute?period_month=2026-09&rule_code=RULE-A&created_by=device-mgr")
    assert blocked.status_code == 409

    # 晚到的补录读数
    _reading(client, "M-1", "2026-09-15T10:00:00Z", 200.0, "read-key-0020")
    _purifier(client, "CR-1", "2026-09-15T00:00:00Z", None, "normal", "pur-key-0020")
    _run(client, "CR-1", "2026-09-15T10:00:00Z", "2026-09-15T11:00:00Z", "run-key-0020")
    _reading(client, "M-1", "2026-09-15T11:00:00Z", 212.0, "read-key-0021")

    correction = client.post("/api/emissions/reports/corrections", json={
        "period_month": "2026-09", "rule_code": "RULE-A", "reason": "补录9月15日炉次",
        "created_by": "device-mgr"}).json()
    assert correction["version_no"] == 2
    assert correction["correction_of_id"] == first["id"]
    added = [c for c in correction["changes"] if c["change_type"] == "added"]
    assert any(c["detail"]["after"]["run_id"] for c in added)
    assert correction["diff"]["changed"] and "batch_count" in correction["diff"]["totals_changes"]
    # 原签发版本保持不变
    old = client.get(f"/api/emissions/reports/{first['id']}").json()
    assert old["status"] == "issued"
    assert old["totals"]["batch_count"] == first["totals"]["batch_count"]


def test_revoke_reading_and_case_produce_impact_lists(client):
    run = _seed_normal_month(client)
    report = client.post(
        "/api/emissions/reports/compute?period_month=2026-09&rule_code=RULE-A&created_by=device-mgr").json()
    client.post(f"/api/emissions/reports/{report['id']}/issue?actor=regulator-liu")

    # 撤销一条被报表使用的读数：返回受影响清单，不静默改数
    revoked = client.post("/api/emissions/fuel-readings/1/revoke",
                          json={"reason": "表具故障读数作废", "actor": "device-mgr"})
    assert revoked.status_code == 200, revoked.text
    body = revoked.json()
    assert body["affected"]["requires_correction"] is True
    assert body["affected"]["issued_reports"][0]["report_id"] == report["id"]
    impacts = client.get("/api/emissions/impacts?subject_type=reading&subject_id=1").json()
    assert impacts and impacts[0]["action"] == "revoked"

    # 撤销业务档案：列出关联炉次与已签发报表
    case_revocation = client.post("/api/emissions/cases/CASE-EM-001/revoke",
                                  json={"reason": "档案录入错误", "actor": "intake-supervisor"})
    assert case_revocation.status_code == 200
    case_body = case_revocation.json()
    assert run["id"] in case_body["affected"]["run_ids"]
    assert case_body["affected"]["issued_reports"]
    # 再次撤销是幂等的，返回同一份清单
    again = client.post("/api/emissions/cases/CASE-EM-001/revoke",
                        json={"reason": "档案录入错误", "actor": "intake-supervisor"})
    assert again.status_code == 200 and again.json()["impact_id"] == case_body["impact_id"]

    # 撤销后重算：已签发版本仍不变；更正版中该炉次批次消失
    correction = client.post("/api/emissions/reports/corrections", json={
        "period_month": "2026-09", "rule_code": "RULE-A", "reason": "撤销错误档案后更正",
        "created_by": "device-mgr"}).json()
    removed = [c for c in correction["changes"] if c["change_type"] == "removed"]
    assert any(f"run:{run['id']}:" in c["batch_key"] for c in removed)
    old = client.get(f"/api/emissions/reports/{report['id']}").json()
    assert old["status"] == "issued"


def test_fixed_clock_replay_cross_month_run(client):
    from datetime import datetime as dt
    from datetime import timezone

    from app.core.clock import FrozenClock
    from app.emissions.service import EmissionsService

    arrival = FrozenClock(dt(2026, 10, 1, 12, 0, tzinfo=timezone.utc))
    service = EmissionsService(clock=arrival)
    service.create_rule({"code": "RULE-A", "name": "燃油排放系数", "fuel_factor": 2.0,
                         "purifier_uplift": 0.5, "max_run_minutes": 600,
                         "valid_from": dt(2026, 1, 1, tzinfo=timezone.utc)}, "env-admin")
    service.create_cremator({"code": "CR-1", "name": "一号火化炉", "site_code": "SITE-1",
                             "meter_code": "M-1"}, "device-admin")
    service.add_purifier_state({"cremator_code": "CR-1",
                                "start_at": dt(2026, 9, 30, tzinfo=timezone.utc), "end_at": None,
                                "state": "normal", "idempotency_key": "pur-key-0030"})
    # 跨月炉次：9-30 23:00 -> 10-01 02:00
    service.add_fuel_reading({"meter_code": "M-1",
                              "read_at": dt(2026, 9, 30, 23, 0, tzinfo=timezone.utc),
                              "reading": 500.0, "idempotency_key": "read-key-0030"})
    run = service.record_run({"cremator_code": "CR-1",
                              "actual_start_at": dt(2026, 9, 30, 23, 0, tzinfo=timezone.utc),
                              "actual_end_at": dt(2026, 10, 1, 2, 0, tzinfo=timezone.utc),
                              "case_ref": "CASE-CROSS-1", "idempotency_key": "run-key-0030"})
    # 第二条读数次月 3 日才补录到达
    arrival.advance(hours=39)
    service.add_fuel_reading({"meter_code": "M-1",
                              "read_at": dt(2026, 10, 1, 2, 0, tzinfo=timezone.utc),
                              "reading": 560.0, "idempotency_key": "read-key-0031"})

    # 补录前的固定时钟（10-01 13:00）：跨月切片缺月界后端点，无法结算
    early_clock = dt(2026, 10, 1, 13, 0, tzinfo=timezone.utc)
    early = service.compute_report("2026-09", "RULE-A", "device-mgr", fixed_clock=early_clock)
    early_batch = next(b for b in early["batches"] if b["run_id"] == run["id"])
    assert any(a["code"] == "reading_missing" for a in early_batch["anomalies"])

    # 补录后固定时钟重放：九月片 1 小时 20 单位、十月片 2 小时 40 单位
    replay_clock = dt(2026, 10, 3, 12, 0, tzinfo=timezone.utc)
    replay_sept = service.compute_report("2026-09", "RULE-A", "device-mgr", fixed_clock=replay_clock)
    sept_batch = next(b for b in replay_sept["batches"] if b["run_id"] == run["id"])
    assert sept_batch["actual_window"]["start_at"] == "2026-09-30T23:00:00+00:00"
    assert sept_batch["actual_window"]["end_at"] == "2026-10-01T00:00:00+00:00"
    assert sept_batch["fuel_used"] == 20.0
    assert "run_cross_month" in [a["code"] for a in sept_batch["anomalies"]]

    replay_oct = service.compute_report("2026-10", "RULE-A", "device-mgr", fixed_clock=replay_clock)
    oct_batch = next(b for b in replay_oct["batches"] if b["run_id"] == run["id"])
    assert oct_batch["actual_window"]["start_at"] == "2026-10-01T00:00:00+00:00"
    assert oct_batch["fuel_used"] == 40.0

    # 固定时钟重放版本不能签发
    with pytest.raises(Exception) as excinfo:
        service.issue_report(replay_sept["id"], "regulator-liu")
    assert excinfo.value.status_code == 409


def test_export_contains_detail_reasons_changes_and_input_summary(client):
    _seed_normal_month(client)
    report = client.post(
        "/api/emissions/reports/compute?period_month=2026-09&rule_code=RULE-A&created_by=device-mgr").json()
    client.post(f"/api/emissions/reports/{report['id']}/issue?actor=regulator-liu")
    _reading(client, "M-1", "2026-09-20T08:00:00Z", 300.0, "read-key-0040")
    correction = client.post("/api/emissions/reports/corrections", json={
        "period_month": "2026-09", "rule_code": "RULE-A",
        "reason": "新增无法归属读数", "created_by": "device-mgr"}).json()

    export = client.get(f"/api/emissions/reports/{correction['id']}/export").json()
    assert export["report"]["version_no"] == 2
    assert export["input_summary"]["input_digest"] == correction["input_digest"]
    assert export["input_summary"]["counts"]["readings"] >= 3
    assert isinstance(export["batches"], list)
    anomaly_messages = {a["message"] for b in export["batches"] for a in b["anomalies"]}
    assert any("无法归属" in message for message in anomaly_messages)
    assert export["changes"]
    # 导出内容可 JSON 序列化（监管接口直接交换）
    json.dumps(export, ensure_ascii=False)


def test_reading_idempotency_conflict_and_non_monotonic(client):
    _rule(client)
    _cremator(client)
    payload = {"meter_code": "M-1", "read_at": "2026-09-08T08:00:00Z", "reading": 100.0,
               "idempotency_key": "read-key-0050"}
    assert client.post("/api/emissions/fuel-readings", json=payload).status_code == 201
    changed = dict(payload, reading=99.0)
    assert client.post("/api/emissions/fuel-readings", json=changed).status_code == 409
    _purifier(client, "CR-1", "2026-09-08T00:00:00Z", None, "normal", "pur-key-0050")
    _reading(client, "M-1", "2026-09-08T10:00:00Z", 90.0, "read-key-0051")
    _run(client, "CR-1", "2026-09-08T08:00:00Z", "2026-09-08T10:00:00Z", "run-key-0050")
    report = client.post(
        "/api/emissions/reports/compute?period_month=2026-09&rule_code=RULE-A&created_by=device-mgr").json()
    batch = next(b for b in report["batches"] if b["run_id"])
    assert any(a["code"] == "reading_non_monotonic" for a in batch["anomalies"])
