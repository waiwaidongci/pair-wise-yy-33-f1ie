# 电网事故应急与恢复调度系统

标准库 Python 3.11+ + SQLite。系统管理停运事故、重要用户、备用容量、恢复步骤及安全依赖；接受现场离线报告并区分已合并、版本冲突和受保护记录，异常遥测单独隔离。停复电凭证独立成层：存档、判定与 HTTP 入口分离。

## 运行

```bash
python3 app.py --init --seed
python3 app.py
```

默认端口 `8215`。身份使用 `X-Actor` 和 `X-Role`，角色为 `dispatcher`、`operator`、`field`。可用 `--port`、`--db` 覆盖。

## 主要接口

- `POST /api/assets`、`POST /api/facilities`：登记线路资产和医院等重要用户。
- `POST /api/outages`：创建或幂等接收同一事故。
- `POST /api/telemetry`：记录并隔离错误遥测。
- `POST /api/plans`、`/submit`、`/approve`、`/activate`：创建、提交、审批并启用安全恢复计划。
- `POST /api/plans/{id}/change`：在不修改已确认步骤的前提下创建新计划版本。
- `POST /api/field-reports`：合并现场离线报告，重复客户端编号不会重复写入。
- `POST /api/plans/{id}/confirm`：调度员确认步骤，依赖未满足时拒绝。
- `POST /api/vouchers`：现场回传**停复电凭证**（`kind=start|complete`，含班组 `crew`、设备序列 `asset_code`、带时区现场时刻 `field_time`、`client_voucher_id`）。
- `POST /api/status`：发布当前恢复状态；每步确认齐全但缺当前版本有效完工凭证时拒绝发布“恢复完成”并返回每步卡住原因。
- `GET /api/plans/{id}`：详情、确认、现场报告，以及 `vouchers`（全量留档）、`step_vouchers`（每步凭证状态/卡住原因）、`publish_blockers`（发布阻碍）。
- `GET /api/state`、`GET /api/health`：状态和健康检查。

## 停复电凭证规则

- 开工与完工均记录班组、设备序列和现场时刻；完工凭证要求该步先有有效开工、完工时刻不早于开工时刻。
- 同一设备（设备序列）上一段作业未完工解除前，不允许下一段步骤回传有效完工凭证。
- 同一 `client_voucher_id` 重复回传沿用首条记录（即使首条被拒收也不重新判定），响应带 `duplicate=true`。
- 计划改版后旧计划变为 `superseded`，旧版本凭证立即失效（页面标记“改版失效”）；新版本每步都要重新回传有效完工凭证，事故才能发布“恢复完成”。
- 无效凭证仍留档（`valid=false` + `reject_reason`），可在每步视图中看到最近一次拒收原因。

## 模块划分

- `archive.py`（`VoucherArchive`）：凭证存档，append-only 落库与去重查询，不做判定。
- `voucher_policy.py`（`VoucherPolicy`）：受理判定、同设备上一段未解除拦截、改版失效、每步卡住原因与发布闸门。
- `app.py`：HTTP 入口与计划/事故编排；`POST /api/vouchers` 仅为入口，判定委托 policy、持久化委托 archive。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

当前为原型：容量和依赖是静态安全模型，不包含潮流计算、SCADA/EMS 协议、实时遥测质量码或生产级多实例锁；离线合并通过客户端编号和计划版本完成。
