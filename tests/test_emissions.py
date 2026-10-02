from __future__ import annotations


def _make_cremation_setup(client, ref: str = "EM-CASE-001", resource_code: str = "CREM-1") -> dict:
    case = client.post("/api/mortuary/cases?actor=intake", json={
        "external_ref": ref, "decedent_name": "韩秀兰", "identity_number": "ID-440100-1942",
        "death_time": "2026-09-28T06:00:00Z", "received_from": "市第一医院",
        "family_contact": "韩磊", "family_phone": "13800000001", "special_notes": ""})
    assert case.status_code == 201, case.text
    resource = client.post("/api/mortuary/resources?actor=scheduler", json={
        "code": resource_code, "name": "一号火化炉", "kind": "cremator",
        "site_code": "SITE-1", "capacity": 2, "attributes": {"fuel_meter": "FM-1"}})
    assert resource.status_code in (201, 409), resource.text
    return {"case": case.json(), "resource_code": resource_code}


def _reserve_and_run(client, case_id: int, resource_code: str, start: str, end: str, key_run: str, meter: str = "FM-1") -> dict:
    reservation = client.post("/api/mortuary/reservations", json={
        "resource_code": resource_code, "case_id": case_id, "start_at": start, "end_at": end,
        "purpose": "遗体火化", "created_by": "scheduler", "idempotency_key": f"res-{key_run}"})
    assert reservation.status_code == 201, reservation.text
    run = client.post("/api/emissions/furnace-runs", json={
        "resource_code": resource_code, "case_id": case_id, "reservation_id": reservation.json()["id"],
        "start_at": start, "end_at": end, "shift_code": "A", "idempotency_key": f"run-{key_run}"})
    assert run.status_code == 201, run.text
    return {"reservation_id": reservation.json()["id"], "run_id": run.json()["id"]}


def _fuel(client, meter: str, reading_at: str, value: float, key: str) -> None:
    response = client.post("/api/emissions/fuel-readings", json={
        "meter_code": meter, "reading_at": reading_at, "value": value, "idempotency_key": key})
    assert response.status_code == 201, response.text


def _purify(client, resource_code: str, start: str, end: str, state: str, key: str) -> None:
    response = client.post("/api/emissions/purification-records", json={
        "resource_code": resource_code, "start_at": start, "end_at": end,
        "state": state, "idempotency_key": key})
    assert response.status_code == 201, response.text


def _register_rules(client, meter_map: dict) -> int:
    response = client.post("/api/emissions/rules", json={
        "name": "测试规则", "description": "仪表映射", "actor": "env-admin",
        "rules": {
            "cross_month_policy": "start_month", "revoked_case_policy": "exclude",
            "emission_factor": 2.0, "fuel_tie_break": "latest_recorded",
            "purification_states": {"normal": 0.9, "maintenance": 0.5, "fault": 0.0, "bypass": 0.0},
            "uncovered_purification_efficiency": 0.0,
            "meter_resource_map": meter_map,
        }})
    assert response.status_code == 201, response.text
    return response.json()["version"]


def test_full_batch_reproducible_and_immutable(client):
    setup = _make_cremation_setup(client)
    case_id = setup["case"]["id"]
    code = setup["resource_code"]
    _register_rules(client, {code: "FM-1"})
    _reserve_and_run(client, case_id, code, "2026-09-28T08:00:00Z", "2026-09-28T09:00:00Z", "0001")
    _fuel(client, "FM-1", "2026-09-28T08:00:00Z", 100.0, "fuel-0001")
    _fuel(client, "FM-1", "2026-09-28T09:00:00Z", 130.0, "fuel-0002")
    _purify(client, code, "2026-09-28T08:00:00Z", "2026-09-28T09:00:00Z", "normal", "pur-0001")

    first = client.post("/api/emissions/batches/compute", json={"period": "2026-09", "actor": "env-admin"})
    assert first.status_code == 201, first.text
    batch = first.json()
    assert batch["status"] == "draft"
    assert batch["totals"]["run_count"] == 1
    # fuel 30 * factor 2.0 * efficiency 0.9 = 54
    assert batch["totals"]["emission_amount"] == 54.0
    run_item = next(i for i in batch["items"] if i["kind"] == "run")
    assert run_item["fuel_amount"] == 30.0
    assert run_item["anomaly_codes"] == []

    digest_first = batch["input_digest"]
    # 重新计算（同一草稿、同一输入）应完全可复算
    again = client.post("/api/emissions/batches/compute", json={"period": "2026-09", "actor": "env-admin"})
    assert again.json()["id"] == batch["id"]
    assert again.json()["input_digest"] == digest_first
    assert again.json()["totals"]["emission_amount"] == 54.0

    issued = client.post(f"/api/emissions/batches/{batch['id']}/issue?actor=supervisor")
    assert issued.status_code == 200 and issued.json()["status"] == "issued"
    # 已签发不能再次签发
    duplicate = client.post(f"/api/emissions/batches/{batch['id']}/issue?actor=supervisor")
    assert duplicate.status_code == 409


def test_missing_overlap_and_unassignable_readings(client):
    setup = _make_cremation_setup(client, "EM-CASE-002")
    code = setup["resource_code"]
    _register_rules(client, {code: "FM-1"})
    # 有运行但缺少燃料读数与净化记录
    _reserve_and_run(client, setup["case"]["id"], code, "2026-09-10T08:00:00Z", "2026-09-10T09:00:00Z", "0010")
    # 无法归属的读数（仪表已映射但时间不在任何区间）
    _fuel(client, "FM-1", "2026-09-20T08:00:00Z", 500.0, "fuel-stray")
    # 未映射仪表的读数
    _fuel(client, "FM-UNKNOWN", "2026-09-10T08:30:00Z", 5.0, "fuel-unmapped")
    # 只有预约没有实际运行（另一炉次）
    client.post("/api/mortuary/resources?actor=scheduler", json={
        "code": "CREM-2", "name": "二号火化炉", "kind": "cremator",
        "site_code": "SITE-1", "capacity": 2, "attributes": {}})
    client.post("/api/mortuary/reservations", json={
        "resource_code": "CREM-2", "case_id": setup["case"]["id"],
        "start_at": "2026-09-11T08:00:00Z", "end_at": "2026-09-11T09:00:00Z",
        "purpose": "火化", "created_by": "scheduler", "idempotency_key": "res-norun"})

    batch = client.post("/api/emissions/batches/compute", json={"period": "2026-09", "actor": "env-admin"})
    assert batch.status_code == 201, batch.text
    codes = {a["code"] for a in batch.json()["anomalies"]}
    assert "fuel_missing" in codes
    assert "fuel_unassignable" in codes
    assert "fuel_unmapped_meter" in codes
    assert "purification_missing" in codes
    assert "reservation_no_run" in codes


def test_run_overlap_and_purification_fault(client):
    setup = _make_cremation_setup(client, "EM-CASE-003")
    code = setup["resource_code"]
    _register_rules(client, {code: "FM-1"})
    # 同炉时间重叠的两个炉次（不同档案）
    other = _make_cremation_setup(client, "EM-CASE-004", "CREM-1")
    _reserve_and_run(client, setup["case"]["id"], code, "2026-09-05T08:00:00Z", "2026-09-05T09:30:00Z", "0020")
    _reserve_and_run(client, other["case"]["id"], code, "2026-09-05T09:00:00Z", "2026-09-05T10:00:00Z", "0021")
    for i, (t, v) in enumerate([("08:00", 10), ("09:00", 20), ("09:30", 30), ("10:00", 40)]):
        _fuel(client, "FM-1", f"2026-09-05T{t}:00Z", float(v), f"fuel-ov-{i}")
    _purify(client, code, "2026-09-05T08:00:00Z", "2026-09-05T10:00:00Z", "fault", "pur-fault")

    batch = client.post("/api/emissions/batches/compute", json={"period": "2026-09", "actor": "env-admin"}).json()
    codes = {a["code"] for a in batch["anomalies"]}
    assert "run_overlap" in codes
    assert "purification_fault" in codes
    # 故障状态净化效率为 0，排放应为 0
    assert batch["totals"]["emission_amount"] == 0.0


def test_issued_report_unchanged_correction_explains_delta(client):
    setup = _make_cremation_setup(client, "EM-CASE-005")
    code = setup["resource_code"]
    _register_rules(client, {code: "FM-1"})
    _reserve_and_run(client, setup["case"]["id"], code, "2026-09-12T08:00:00Z", "2026-09-12T09:00:00Z", "0030")
    _fuel(client, "FM-1", "2026-09-12T08:00:00Z", 0.0, "fuel-c-1")
    _fuel(client, "FM-1", "2026-09-12T09:00:00Z", 10.0, "fuel-c-2")
    _purify(client, code, "2026-09-12T08:00:00Z", "2026-09-12T09:00:00Z", "normal", "pur-corr-1")

    original = client.post("/api/emissions/batches/compute", json={"period": "2026-09", "actor": "env-admin"}).json()
    assert original["totals"]["emission_amount"] == 18.0  # 10*2*0.9
    client.post(f"/api/emissions/batches/{original['id']}/issue?actor=supervisor")

    # 后续补录燃料读数（止码修正为 20）
    _fuel(client, "FM-1", "2026-09-12T09:00:00Z", 20.0, "fuel-c-fixed")
    corrected = client.post("/api/emissions/batches/correct", json={
        "period": "2026-09", "actor": "env-admin",
        "corrects_batch_id": original["id"], "reason": "补录燃料止码"})
    assert corrected.status_code == 201, corrected.text
    correction = corrected.json()
    assert correction["corrects"]["id"] == original["id"]
    assert correction["totals"]["emission_amount"] == 36.0  # 20*2*0.9
    delta_fields = {(d["item_key"], d["field"]) for d in correction["deltas"]}
    assert any(field == "fuel_end_value" for _, field in delta_fields)
    assert any(field == "emission_amount" for _, field in delta_fields)

    # 原已签发报表保持不变
    frozen = client.get(f"/api/emissions/batches/{original['id']}").json()
    assert frozen["status"] == "issued"
    assert frozen["totals"]["emission_amount"] == 18.0


def test_revoke_case_produces_impact_list_not_silent_change(client):
    setup = _make_cremation_setup(client, "EM-CASE-006")
    code = setup["resource_code"]
    _register_rules(client, {code: "FM-1"})
    _reserve_and_run(client, setup["case"]["id"], code, "2026-09-13T08:00:00Z", "2026-09-13T09:00:00Z", "0040")
    _fuel(client, "FM-1", "2026-09-13T08:00:00Z", 0.0, "fuel-r-1")
    _fuel(client, "FM-1", "2026-09-13T09:00:00Z", 10.0, "fuel-r-2")
    _purify(client, code, "2026-09-13T08:00:00Z", "2026-09-13T09:00:00Z", "normal", "pur-rev-1")
    batch = client.post("/api/emissions/batches/compute", json={"period": "2026-09", "actor": "env-admin"}).json()
    client.post(f"/api/emissions/batches/{batch['id']}/issue?actor=supervisor")

    revoke = client.post(f"/api/emissions/cases/{setup['case']['id']}/revoke",
                         json={"actor": "env-admin", "reason": "业务档案重复登记，撤销"})
    assert revoke.status_code == 201, revoke.text
    result = revoke.json()
    assert result["affected_count"] == 1
    assert result["affected"][0]["batch_id"] == batch["id"]

    impacts = client.get("/api/emissions/impacts?status=open").json()["items"]
    assert any(i["trigger_type"] == "case_revoked" and i["item_key"] == f"run-{result['affected'][0]['item_key'].split('-')[-1]}" or i["batch_id"] == batch["id"] for i in impacts)

    # 已签发报表数字没有被静默改动
    frozen = client.get(f"/api/emissions/batches/{batch['id']}").json()
    assert frozen["totals"]["emission_amount"] == 18.0
    # 重新计算的更正版会把撤销档案排除
    corrected = client.post("/api/emissions/batches/correct", json={
        "period": "2026-09", "actor": "env-admin",
        "corrects_batch_id": batch["id"], "reason": "档案撤销，剔除炉次"}).json()
    assert corrected["totals"]["included_run_count"] == 0
    assert corrected["totals"]["emission_amount"] == 0.0


def test_equipment_fault_marks_issued_items(client):
    setup = _make_cremation_setup(client, "EM-CASE-007")
    code = setup["resource_code"]
    _register_rules(client, {code: "FM-1"})
    _reserve_and_run(client, setup["case"]["id"], code, "2026-09-14T08:00:00Z", "2026-09-14T09:00:00Z", "0050")
    _fuel(client, "FM-1", "2026-09-14T08:00:00Z", 0.0, "fuel-f-1")
    _fuel(client, "FM-1", "2026-09-14T09:00:00Z", 10.0, "fuel-f-2")
    _purify(client, code, "2026-09-14T08:00:00Z", "2026-09-14T09:00:00Z", "normal", "pur-flt-1")
    batch = client.post("/api/emissions/batches/compute", json={"period": "2026-09", "actor": "env-admin"}).json()
    client.post(f"/api/emissions/batches/{batch['id']}/issue?actor=supervisor")

    fault = client.post("/api/emissions/equipment-faults", json={
        "resource_code": code, "start_at": "2026-09-14T08:30:00Z", "end_at": "2026-09-14T09:30:00Z",
        "kind": "furnace_fault", "note": "引风机故障停机", "actor": "maintenance"})
    assert fault.status_code == 201, fault.text
    assert fault.json()["affected_count"] == 1
    impacts = client.get("/api/emissions/impacts").json()["items"]
    assert any(i["trigger_type"] == "equipment_fault" for i in impacts)


def test_export_contains_details_anomalies_and_differences(client):
    setup = _make_cremation_setup(client, "EM-CASE-008")
    code = setup["resource_code"]
    _register_rules(client, {code: "FM-1"})
    _reserve_and_run(client, setup["case"]["id"], code, "2026-09-15T08:00:00Z", "2026-09-15T09:00:00Z", "0060")
    _fuel(client, "FM-1", "2026-09-15T08:00:00Z", 0.0, "fuel-e-1")
    _fuel(client, "FM-1", "2026-09-15T09:00:00Z", 10.0, "fuel-e-2")
    _purify(client, code, "2026-09-15T08:00:00Z", "2026-09-15T09:00:00Z", "normal", "pur-exp-1")
    batch = client.post("/api/emissions/batches/compute", json={"period": "2026-09", "actor": "env-admin"}).json()
    client.post(f"/api/emissions/batches/{batch['id']}/issue?actor=supervisor")

    exported = client.get(f"/api/emissions/batches/{batch['id']}/export")
    assert exported.status_code == 200, exported.text
    payload = exported.json()
    assert payload["report"]["period"] == "2026-09"
    assert payload["rule_version"]["rules_digest"]
    assert payload["input_digest"]
    assert len(payload["details"]) == 1
    assert "anomalies" in payload and "differences" in payload and "impacts" in payload
    assert payload["totals"]["emission_amount"] == 18.0


def test_fixed_clock_replay_cross_month_run(client):
    setup = _make_cremation_setup(client, "EM-CASE-009")
    code = setup["resource_code"]
    _register_rules(client, {code: "FM-1"})
    # 跨月炉次：9 月 30 日 23:00 到 10 月 1 日 01:00，按起始月归入 9 月
    _reserve_and_run(client, setup["case"]["id"], code, "2026-09-30T23:00:00Z", "2026-10-01T01:00:00Z", "0070")
    _fuel(client, "FM-1", "2026-09-30T23:00:00Z", 0.0, "fuel-x-1")
    _fuel(client, "FM-1", "2026-10-01T01:00:00Z", 40.0, "fuel-x-2")
    _purify(client, code, "2026-09-30T23:00:00Z", "2026-10-01T01:00:00Z", "normal", "pur-xmo-1")

    fixed = "2026-10-02T00:00:00Z"
    replay_sep = client.post("/api/emissions/batches/replay", json={
        "period": "2026-09", "actor": "auditor", "fixed_clock": fixed})
    assert replay_sep.status_code == 201, replay_sep.text
    replayed = replay_sep.json()
    assert replayed["is_replay"] is True
    assert replayed["status"] == "replay"
    assert replayed["clock_fixed_at"].startswith("2026-10-02")
    assert replayed["totals"]["run_count"] == 1
    assert replayed["totals"]["cross_month_count"] == 1
    # 40 * 2 * 0.9 = 72
    assert replayed["totals"]["emission_amount"] == 72.0

    # 10 月报表不应重复统计该跨月炉次
    october = client.post("/api/emissions/batches/compute", json={"period": "2026-10", "actor": "env-admin"}).json()
    assert october["totals"]["run_count"] == 0
    # 重放批次不可签发
    assert client.post(f"/api/emissions/batches/{replayed['id']}/issue?actor=auditor").status_code == 409
    # 同一固定时钟重放结果一致（可复算）
    replay_again = client.post("/api/emissions/batches/replay", json={
        "period": "2026-09", "actor": "auditor", "fixed_clock": fixed}).json()
    assert replay_again["input_digest"] == replayed["input_digest"]
    assert replay_again["totals"]["emission_amount"] == replayed["totals"]["emission_amount"]
