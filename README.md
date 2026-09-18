# 慢跑训练负荷服务

本项目服务于慢跑训练与可穿戴数据。个人基线、设备指标、伤病标签和教练覆盖共同决定训练建议。系统应支持清晰的领域对象、事件记录和责任追溯，运行入口提供稳定的健康检查，便于本地联调和运维巡检。

## 领域结构（`domain/`）

- `models.py` — 用户基线、指标批次、规范化训练记录、计划版本、建议与事件。人群负荷系数（初学者 1.2 / 慢病 1.5 / 伤后恢复 1.8 / 常规 1.0）保证同样的 30 分钟超慢跑对不同人群算出不同负荷。
- `users.py` — 注册跑者、伤病标签、本人授权（coach / medical）。
- `ingest.py` — 可穿戴数据汇集：`sync_id` 幂等去重，重复同步只计一次负荷；按记录时区归因训练日，跨日同步不扭曲周计划；手工修正作废旧批次，负荷仍只算一次。
- `rules.py` — 安全规则：异常心率、伤病标签、时区变化、关键数据缺失。命中即暂停相关安排，**不自动增加训练量**，并请求本人或专业人员确认。
- `recommend.py` — 建议生成（附 `explanation`：采用了哪些数据与规则）、确认流程（集齐所需角色后恢复计划）、教练覆盖（必须填写原因与期限，原版本完整保留，过期自动回落）。
- `access.py` — 角色视图：跑者看自己的全部细节，教练看训练摘要，医疗顾问看健康摘要；`replay` 完整呈现原始记录、风险判断以及后来是否被人工调整。
- `store.py` — 内存仓储与追加式事件日志，所有状态变化可追溯。

## 运行与测试

```bash
python3 service.py --check        # 基础检查
python3 service.py --port 8000    # 启动服务，GET /health 确认身份
npm test                          # 运行全部 unittest（契约 + 领域 + 接口）
```

## 主要接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/users` | 注册跑者与个人基线（分组、心率、常驻时区） |
| POST | `/users/{uid}/metrics` | 设备/手工数据汇集（`sync_id` 幂等，`corrects` 修正） |
| POST | `/users/{uid}/injuries` · `/injuries/clear` | 添加 / 清除伤病标签 |
| POST | `/users/{uid}/grants` | 本人授权教练或医疗顾问查看摘要 |
| POST | `/users/{uid}/recommendations` | 生成建议（ready 或 held） |
| POST | `/recommendations/{rid}/confirm` | 本人或授权医疗顾问确认待确认建议 |
| POST | `/users/{uid}/overrides` | 教练覆盖（必填原因与期限） |
| GET | `/users/{uid}/week?actor=` | 周负荷摘要（按角色裁剪，标注数据完整度） |
| GET | `/recommendations/{rid}?actor=` | 按角色查看建议 |
| GET | `/recommendations/{rid}/replay?actor=` | 回放：原始记录、规则判定、后续人工调整 |
