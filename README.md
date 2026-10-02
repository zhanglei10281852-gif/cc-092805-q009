# 安宁礼仪与公墓运营服务

这是一个供殡仪馆、公墓和合作医疗机构使用的 Python 后端服务，统一管理逝者业务档案、遗体保管交接、送别厅与火化设备预约、服务订单、墓位权属、账单收款和审计时间线。系统把容易产生争议的交接、排程与收费动作保存在本地 SQLite 中，支持在单个 Linux 应用容器内离线运行。

## 运行环境

- Python 3.11
- FastAPI 与 Uvicorn
- SQLite 3，由 Python 标准库提供

## 安装

依次执行 python -m venv .venv、source .venv/bin/activate、python -m pip install -e ".[dev]"。可通过 PEACEFUL_CARE_DATABASE_PATH 指定数据库文件，默认写入项目的 data 目录。

## 初始化与启动

先执行 python -m app.cli init-db 和 python -m app.cli check-db，再用 uvicorn app.main:app --host 0.0.0.0 --port 8432 启动。健康检查为 GET /api/system/health。殡葬业务接口位于 /api/mortuary，涵盖档案、交接、资源、预约、服务订单、墓位权属、账单和时间线。火化炉排放批次监管接口位于 /api/emissions，按月汇总火化炉运行、燃料读数、净化状态与校准规则。

## 测试与编译检查

测试命令：python -m pytest

编译命令：python -m compileall -q app tests

API 与 CLI 冒烟命令：python -m app.cli smoke、python -m app.cli mortuary-demo

## 目录结构

- app/mortuary：档案、保管交接、资源排程、权属和账单领域
- app/emissions：火化炉排放批次的可复算核算、签发冻结、更正差异、影响清单与监管导出
- app/api：登录、角色、审计及系统管理接口
- app/core：时钟、安全、异常、隐私与分页能力
- app/repositories：通用身份和审计数据访问
- app/services：会话、权限、后台任务及维护服务
- tests：领域、接口、异常路径和身份回归测试

## 一致性约定

SQLite 连接启用外键、WAL、忙等待和即时事务。业务档案采用外部编号去重，保管交接与预约保留幂等键，服务订单开票后不可再次开票，支付流水不能重复分配。关键状态变化同时写入领域时间线；会话令牌仅保存摘要，审计记录不会保存明文密码或令牌。

## 排放批次核算约定

排放核算是纯函数式的可复算流程：每次计算都固化输入摘要（运行区间、火化预约、燃料读数、净化状态、撤销档案与故障标记的标识）、输入摘要摘要值与规则版本。同一输入与规则版本必然得到相同明细、异常与合计。

- 炉次按规则的跨月策略只归属一个月份（默认按起始月），跨月炉次不会在下月重复统计。
- 自动识别运行区间重叠、有运行无预约、有预约无运行、燃料读数缺失/重叠/止码倒退、读数无法归属或仪表未映射、净化状态缺失/重叠/故障等异常。
- 批次草稿可反复复算；签发（issued）后报表内容冻结不可变更。后续到达的数据通过“更正版”（correct）生成新版本，并逐字段记录与原版本的前后差异。
- 撤销业务档案或登记设备故障不会静默改动已签发数字，而是为命中的已签发明细生成受影响清单（impacts），待人工处理。
- 监管接口 GET /api/emissions/batches/{id}/export 导出某一版本的明细、异常原因与前后差异；POST /batches/replay 用固定时钟重放跨月炉次，重放批次不可签发。
- 源数据登记（炉次、燃料读数、净化记录）均带幂等键，迟到补录不会产生重复记录。
