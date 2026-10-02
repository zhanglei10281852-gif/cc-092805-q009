# 可复算排放批次设计说明

设备管理员按月向监管部门汇总火化炉运行与净化设施记录。燃料表、设备班次、
火化业务档案三类数据到达时间不同，同一炉次补录后容易被重复统计。本模块用
**不可变版本 + 输入摘要 + 可重放计算**解决这一问题。

## 数据模型（`app/emissions`）

| 表 | 作用 |
| --- | --- |
| `emission_cremators` | 火化炉主数据，绑定燃料表 `meter_code` |
| `emission_calibration_rules` | 校准规则，按 `(code, version)` 版本化，含生效区间 |
| `emission_fuel_readings` | 燃料表**累计读数**，幂等键录入，可撤销（不删除） |
| `emission_purifier_states` | 净化设施状态区间：normal/bypassed/fault |
| `emission_runs` | 炉次实际运行区间，可挂火化预约与业务档案编号 |
| `emission_equipment_flags` | 设备故障/维保标记 |
| `emission_reports` | 月度报表版本：输入摘要、结果摘要、规则版本、固定时钟 |
| `emission_batches` | 排放批次明细（每个炉次每月一条，跨月按月界切分） |
| `emission_batch_readings` | 批次与读数的多对多归属 |
| `emission_unattributed_readings` | 该版本下无法归属的读数 |
| `emission_changes` | 更正版相对前版的前后差异 |
| `emission_impacts` | 撤销/故障处置生成的受影响清单 |
| `emission_case_revocations` | 火化业务档案撤销登记 |
| `emission_events` | 领域时间线 |

## 计算口径（`app/emissions/engine.py`，纯函数）

- 燃料表按累计读数处理：炉次用量 = 结束时刻插值 − 开始时刻插值；
  端点与最近读数的间隔超过校准规则 `meter_tolerance`（默认 120 分钟）时
  不插值，该炉次记 `reading_missing`。
- **跨月炉次**按月界切分为独立批次，用量按时间比例分配，切片带
  `run_cross_month` 异常，九月与十月月报各自只结算自己一侧。
- 估算排放 = 燃料用量 × `fuel_factor`；净化状态为 fault/bypassed 时
  再乘 `1 + purifier_uplift`。
- 校准规则按炉次结束时刻选择当时有效的同码最新版本；无适用规则记
  `calibration_missing`，该批次不估算排放。
- 异常覆盖：缺实际结束时间、同炉区间重叠、运行超长、缺预约/预约偏差、
  缺净化状态、净化故障/旁通、净化状态区间重叠、设备故障标记、
  读数倒转、读数争议（落入多个重叠炉次）、读数无法归属。

## 版本与可复算

- 每次计算保存 `input_digest`（全部输入快照的 SHA-256）、
  `result_digest`、规则代码与版本号、输入快照 JSON。同输入重算幂等。
- 报表状态：`computed → issued → superseded`。
- **已签发报表的明细永不修改**；签发后普通重算返回 409，晚到数据通过
  `POST /api/emissions/reports/corrections` 生成更正版，更正版保存与前版
  的逐批次前后差异（added/removed/modified 及合计变化）。
- 固定时钟重放：`fixed_clock` 参数只采用该时刻前已创建且当时未撤销的
  输入（含规则、火化炉、预约）。重放副本标记 `clock_fixed_at`，不能签发，
  只用于复算核对，不影响正式版本链。

## 撤销与故障：不静默改数

撤销燃料读数、撤销炉次、撤销业务档案、解除设备故障标记都只改变数据状态，
并生成 `emission_impacts` 受影响清单：列出受影响炉次、引用过该数据的全部
报表版本，以及其中**已签发、必须出更正版**的报表。已签发报表的数字不会被
悄悄改动；下一次更正版通过差异解释变化。撤销操作幂等，重复提交返回同一份
清单。

## 监管接口

- `GET /api/emissions/reports/{id}/export` 导出指定版本：报表头、
  输入摘要（摘要值与各类输入计数）、批次明细、异常代码与中文原因、
  无法归属读数、前后差异、完整输入快照。
- `GET /api/emissions/impacts` 查询撤销/故障处置的受影响清单。

## 主要接口

```
POST /api/emissions/cremators
POST /api/emissions/calibration-rules
POST /api/emissions/fuel-readings
POST /api/emissions/purifier-states
POST /api/emissions/runs
POST /api/emissions/equipment-flags
POST /api/emissions/reports/compute[?fixed_clock=...]
POST /api/emissions/reports/corrections
POST /api/emissions/reports/{id}/issue
GET  /api/emissions/reports/{id}/export
POST /api/emissions/fuel-readings/{id}/revoke
POST /api/emissions/runs/{id}/revoke
POST /api/emissions/cases/{case_ref}/revoke
```
