# 电网事故应急与恢复调度系统

标准库 Python 3.11+ + SQLite。系统管理停运事故、重要用户、备用容量、恢复步骤及安全依赖；接受现场离线报告并区分已合并、版本冲突和受保护记录，异常遥测单独隔离。

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
- `POST /api/vouchers`：现场回传停复电凭证（`phase=start|finish`，含 `crew` 班组、`asset_code` 设备序列、`field_time` 现场时刻、`client_voucher_id`、`expected_plan_version`）。重复编号沿用首条记录；计划未激活/版本过期/同设备上一段未解除完工时返回 `merge_status=conflict` 且不计生效。
- `GET /api/plans/{id}/vouchers`：查询每步开工/完工凭证与卡住原因。
- `POST /api/plans/{id}/confirm`：调度员确认步骤，依赖未满足时拒绝。
- `POST /api/status`：发布当前恢复状态。当前版本计划必须**每步都有有效完工凭证**（且全部确认）才会发布 `restored`，否则返回 `restoring`、`steps_missing_finish_voucher` 与 `voucher_block_reasons`。
- `GET /api/plans/{id}`、`GET /api/state`、`GET /api/health`：详情、状态和健康检查。

## 停复电凭证规则

- 开工、完工均记录班组、设备序列（步骤资产编号）和现场时刻。
- 同一设备在同一计划版本内，序号更早的上一段没有有效完工凭证（设备未解除）时，下一段完工判为冲突，不能计入生效。
- 同一 `client_voucher_id` 重复回传幂等，始终返回首条记录。
- 计划改版（`/change`）后旧计划变为 `superseded`，其凭证全部失效，不携带到新版本；现场须按当前版本重新回传。

## 模块划分

- `store.py`：存档（SQLite schema、凭证与各表行级查询）。
- `vouchers.py`：停复电凭证判定（接收、生效、同设备解除顺序、卡住原因视图）。
- `service.py`：业务编排（事故、计划、现场报告、确认、发布闸门）。
- `app.py`：HTTP 入口与启动（兼容 `from app import GridService, Store, ApiError`）。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

当前为原型：容量和依赖是静态安全模型，不包含潮流计算、SCADA/EMS 协议、实时遥测质量码或生产级多实例锁；离线合并通过客户端编号和计划版本完成。
