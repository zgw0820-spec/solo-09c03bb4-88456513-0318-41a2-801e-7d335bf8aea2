# 供电柜告警检修抑制台

单设备（供电柜 #1）过热告警的检修抑制与可靠送达演示。FastAPI + SQLite，
两个容器：告警服务（网页 + API）与本地通知端（可模拟「已受理但丢回执」等故障）。

## 启动

```bash
docker compose up --build
```

- 操作页：<http://localhost:8080>
- 通知端（仅观测/注入故障）：<http://localhost:8081>

本地不装 Docker 时也可直接运行（需先 `pip install -r requirements-dev.txt`）：

```bash
NOTIFIER_DB=/tmp/notifier.db uvicorn app.notifier.main:app --port 8081
ALARM_DB=/tmp/alarm.db NOTIFIER_URL=http://127.0.0.1:8081 uvicorn app.alarm.main:app --port 8080
pytest -q
```

SQLite 文件分别落在两个具名卷 `alarm-data`、`notifier-data` 的 `/data` 下，
容器重启/重建后数据仍在。

## 业务规则与实现位置

| 需求 | 实现 |
| --- | --- |
| 设置检修截止时刻（可随时调整） | `POST /api/maintenance`，截止前再设置即顺延同一窗口 |
| 检修内新触发告警先不通知 | 触发时落在 `suppressed`，不产生任何投递 |
| 到期前恢复的不补送 | `recover()` 置 `skipped`，之后截止事件也不会再捞起它 |
| 到期仍活跃的按本轮固定通知号送达 | 截止时 `suppressed → pending`，通知号在建轮时即固定为 `ALM-000001` 形式 |
| 检修外触发的正常通知 | 触发即 `pending`，工作线程立即投递 |
| 恢复前已受理的通知保留记录 | `sent` 状态在恢复后不变，只追加说明；`accepted_at` 保留 |
| 恢复后尚未开始的投递取消 | `recover()` 见 `delivery_started_at IS NULL` 即 `cancelled` |
| 恢复时投递去向不明先显示待核实 | 新增 `verifying`（待核实）状态，未确认原请求去向前绝不判取消或已送 |
| 回执丢失先按通知号核实 | 任何非确认回执都 `GET /notifications/{no}` 核实，核实到受理即 `sent` |
| 未受理不能误报已送 | 只有通知端明确 `accepted:true` 才写 `accepted_at`；明确拒绝（503）才退避重试 |
| 结案取消时迟到请求不再受理 | 核实确认无记录后 `POST /notifications/{no}/cancel` 登记取消墓碑，迟到 `/notify` 返回 410 |
| 通知端失联期间保持待核实 | 核实/取消登记返回不可达时停在 `verifying` 并退避重试，恢复连通后再给结论 |
| 故障或重启后接续 | 启动时把残留 `delivering` 行改为「先核实」；通知端墓碑与受理记录均持久化，按通知号幂等 |
| 不重复实际通知 | 同一轮固定一个通知号，通知端对同号只记一条（`deliveries` 计数可观测） |

### 状态说明

| 状态 | 含义 |
| --- | --- |
| `suppressed` 抑制中 | 检修内触发，暂缓通知 |
| `pending` 待送 | 已安排投递，含退避等待 |
| `delivering` 待送（投递中） | 工作线程正在投递（瞬时态，计入待送分组） |
| `verifying` 待核实 | 投递请求已发出但去向未确认；只按固定通知号核实，不重发、不判取消 |
| `sent` 已送 | 通知端已确认受理 |
| `skipped` 未补送 | 检修截止/结束前已恢复，按规则不补送 |
| `cancelled` 已取消 | 恢复时投递尚未开始，或恢复后核实确认未受理并已在通知端登记取消 |

### 恢复与迟到受理的交错处理

值班员在投递请求超时后恢复告警时，系统不再直接显示「已取消」，而是：

1. 恢复时若已有投递在途（`delivery_started_at` 已写），轮次进入 `verifying` 待核实；
2. 工作线程只按固定通知号 `GET /notifications/{no}` 核实，绝不新发通知：
   - 通知端**已受理**（含稍后才迟到受理的原请求）→ 置 `sent`，保留送达记录；
   - 通知端**失联/超时** → 一直停在 `verifying`，按退避继续核实；
   - 通知端**明确无此号** → 先 `POST /notifications/{no}/cancel` 登记取消墓碑，
     再置 `cancelled`；此后任何携带该号的迟到请求都被通知端以 **410** 拒绝；
3. 取消登记与迟到受理在通知端由同一把锁串行判定，同一轮不可能同时是已取消与已受理；
4. 通知端、告警端任一重启后，受理记录与取消墓碑都随 SQLite 持久化，重启后继续按号核实，
   结论不会反转。

## 通知端故障注入

```bash
# normal | drop_receipt（已受理但回 504，回执丢失）| reject（未受理，回 503）
curl -X POST localhost:8081/api/fault -H 'content-type: application/json' \
     -d '{"mode":"drop_receipt"}'
curl localhost:8081/api/notifications      # 已受理与取消墓碑（state 字段区分）
```

`NOTIFIER_MODE` 环境变量可设置初始模式。

通知端每个通知号带 `state`：`accepted`（已受理）或 `cancelled`（取消墓碑）。
`POST /notifications/{no}/cancel` 用于告警侧结案登记；对已取消的号再投递返回
`410`，查询也返回 `410`。故障模式（含 reject / drop_receipt）不会覆盖墓碑判定。

## 自测覆盖

`pytest -q`（34 项）：

- 检修外触发立即送达，重复 tick 不重复通知；
- 检修内触发抑制、截止前恢复 `skipped` 且不补送；
- 检修到期后活跃告警按固定通知号送达，恢复后保留记录；
- 手动结束检修释放抑制告警；
- 通知端 503 明确拒绝时保持 `pending`、无 `accepted_at`，恢复后直接取消；
- 丢回执（504）按号核实为已受理，通知端 `deliveries` 仍为 1；
- 超时且核实不可达时进入并保持 `verifying` 待核实，不误报已送、不判取消；
- 恢复时投递在途：先 `verifying`，核实到迟到受理则保留 `sent` 记录；
- 核实确认无记录：通知端登记取消墓碑后 `cancelled`，迟到 `/notify` 被 410 拒绝；
- 取消与受理竞争：取消登记时若已受理则受理胜出，同一轮不会同时已取消且已受理；
- 重启后残留投递先核实、不重发；通知端/告警端分别重启后结论不反转、墓碑持久；
- 活跃告警核实为 404 后仍按同号正常送达且只通知一次；
- 恢复后未开始投递取消；已开始投递保留并只核实；
- 调整检修截止时刻后，抑制轮次按新截止送达；
- 服务停机跨过截止时刻，重启后仍按时送达；
- 手动结束检修时已恢复轮次不补送；
- 通知端全程不可达时不误报已送，恢复连通后按号核实结案；
- API 参数校验、409 冲突、页面与静态资源、通知端故障模式、幂等与取消墓碑接口。

## 已知限制

- 单设备、单实例假设：用进程内锁 + SQLite 串行化，未做多副本协调。
- 「投递已开始」以发出 HTTP 请求前写入 `delivery_started_at` 为准；
  通知端慢响应期间恢复会进入 `verifying` 待核实，只按通知号核实，而不是直接取消。
- 取消墓碑只在通知端给出明确「无此号」（404/410）后登记；通知端持续失联时
  轮次一直停在 `verifying`，不会本地假定取消。
- 通知端是演示替身，受理记录、取消墓碑与故障模式同样存 SQLite；生产替换时需保留
  按通知号查询、同号幂等与取消后拒绝迟到通知（410）的语义。
- 未做鉴权、HTTPS、告警分级与真实设备采集；页面按 1 秒轮询 `/api/state`。
