# 慢跑训练负荷服务

本项目服务于慢跑训练与可穿戴数据。个人基线、设备指标、伤病标签和教练覆盖共同决定训练建议。系统应支持清晰的领域对象、事件记录和责任追溯，运行入口提供稳定的健康检查，便于本地联调和运维巡检。

运行 `python3 service.py --check` 可检查基础配置；执行 `python3 service.py --port 8000` 后访问 `/health` 可以确认服务身份。

## 领域规则

**个性化负荷**：同样的 30 分钟超慢跑（4–6 km/h）对不同人群不是同一种负荷。

- 强度 =（平均心率 − 静息心率）÷（最大心率 − 静息心率），截断到 [0, 1.5]；
- 单次负荷 = 时长 × 强度 × 人群敏感系数：普通 1.0 / 初学者 1.2 / 慢病 1.4 / 伤后恢复 1.6（多标签取最保守）；
- 周负荷增幅上限：普通 10% / 初学者 8% / 慢病与伤后恢复 5%（多标签取最保守）；
- 恢复状态（睡眠、主观疲劳、晨起静息心率）低于 60 时目标只降不增（×0.8）。

**风险闸门**：出现以下任一信号时，不自动增加训练量，目标压回不超过最近 7 天负荷，相关安排暂停（`paused` + 计划 `suspended`），并向本人或授权专业人员发起确认请求：

- `abnormal_heart_rate`：单次最高心率 ≥ 90% 最大心率，或晨起静息心率 ≥ 110% 基线；
- `active_injury`：存在活动状态的伤病标签；
- `timezone_change`：设备上报时区与常驻时区不一致（跨日归属可能失真）；
- `missing_key_data`：训练记录缺心率，或有训练却完全没有睡眠/疲劳等每日指标。

确认（`POST /recommendations/{id}/confirmations`）可由本人或持有效授权的教练/医疗顾问完成，确认后安排恢复生效。

**数据摄入**：同一 `(source, sync_id)` 的同步包只处理一次；同一 `(runner, source, external_id)` 的记录只计一次负荷。手工修正用 `corrects` 指回原记录——原版本保留并标记 `superseded_by`，负荷按新版本计算。训练归属日期一律按跑者常驻时区换算，漏传晚到、跨日同步都会归回真实发生日，不会扭曲周计划。

**留痕与回放**：所有关键动作写入追加式事件日志；教练/医疗覆盖必须给出原因与未来期限，被覆盖前的原版本完整保留；新建议取代旧建议时旧版本标记 `superseded`。`GET /recommendations/{id}/replay` 完整呈现生成时的原始记录快照、风险判断、规则说明，以及后来是否被人工调整（确认/覆盖）。

**访问控制**：跑者只能查看自己的健康细节（完整视图）；教练凭 `training` 授权看训练摘要（负荷、计划、风险标记名，不含原始心率/睡眠/主观疲劳）；医疗顾问凭 `medical` 授权看医疗摘要（风险判断细节、心率统计、伤病标签）。授权由跑者本人授予且带期限，回放同样按角色投影。

## HTTP API

身份通过请求头声明（本地联调用，生产环境应替换为真实认证）：`X-Actor-Id` + `X-Actor-Role`（`runner` / `coach` / `medical` / `system`）。错误统一返回 `{"error": {"code", "message"}}`。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 服务身份（无需身份头） |
| POST | `/runners` | 注册跑者档案（本人或 system） |
| GET | `/runners/{id}` | 按角色返回完整/摘要视图 |
| POST | `/runners/{id}/authorizations` | 本人授予教练/医疗限时授权 |
| GET | `/runners/{id}/events` | 本人查看完整审计事件流 |
| POST | `/syncs` | 设备同步摄入（幂等去重、修正、跨日归属） |
| POST | `/recommendations` | 生成下一 ISO 周建议（含数据与规则说明） |
| GET | `/recommendations/{id}` | 按角色投影的建议视图 |
| GET | `/recommendations/{id}/replay` | 回放：原始记录 + 风险判断 + 人工调整 |
| POST | `/recommendations/{id}/confirmations` | 暂停建议的确认恢复 |
| POST | `/recommendations/{id}/overrides` | 教练/医疗覆盖（原因 + 期限 + 原版本） |

## 代码结构

```
training/
  models.py         领域对象（跑者、记录、建议、覆盖、事件）
  store.py          内存仓储与追加式事件日志
  ingestion.py      设备同步摄入（幂等、修正、跨日归属）
  load.py           个性化负荷与恢复状态
  risk.py           风险闸门（四类风险信号）
  recommendation.py 建议生成 / 暂停确认 / 覆盖 / 回放
  access.py         角色访问控制与摘要投影
  profiles.py       跑者注册与授权管理
service.py          HTTP 入口（保留 /health 契约）
```

## 测试

`npm test`（等价于 `python3 -m unittest -v service_contract test_load test_ingestion test_recommendation test_access test_api`）覆盖：人群敏感系数、幂等去重、手工修正、跨日归属、四类风险暂停、确认/覆盖留痕、角色视图与回放投影、HTTP 端到端流程。
