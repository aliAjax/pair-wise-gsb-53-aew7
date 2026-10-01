# 移民案件期限与材料管理

纯Python标准库实现的移民案件管理服务，使用SQLite持久化（`BEGIN IMMEDIATE`事务），HTTP接口由`http.server`提供。

四个领域相互连接：**案件 records — 补件任务 supplements — 承办人当日名额 quota_slots/capacity — 政策版本 policies**。

## 联动规则

- **发补件先预留名额，容量不足排队**：发补件在一个事务内计数当日 `reserved/confirmed/occupied` 名额，未满则占用一个 `open` 名额（任务`reserved`），已满则任务`queued`；放行或扩容时按 FIFO 自动提升队首。
- **政策更新**：新政策发布后，所有未回应任务（`queued/reserved/confirmed`）置为`voided`并释放名额，依据新版政策重建任务（重算补件期限）；**已回应任务保留原政策版本与原判**，不重算。
- **两人同时确认同一名额只放行一个**：名额确认用条件更新（CAS `WHERE status='reserved' AND task_id=?`），后到者得到 409。
- **写入失败从最后完整批次继续**：批量补发记录 `last_completed_index`，单批事务回滚后断点停在上一完整批次，重试自动续跑。
- **重复请求不重复占名额**：`Idempotency-Key`（或请求体内 `idem_key`）命中时回放首次结果；补件任务表对有效幂等键有唯一部分索引兜底并发。
- **机构隔离**：所有读写按 `X-Org` 过滤，处理其他机构案件/任务/批次返回 403。
- **旧数据回填**：缺 `policy_version` 的案件按创建时（`received_day`）生效的政策一次性回填；历史已回应任务始终保留原判。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动（启动即尝试回填旧政策版本）。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、政策解析/依据、容量与补件校验。
- `src/repository.py`：建表与迁移、事务原语（预留/排队/CAS确认/回应/放行/政策重算/批次断点/幂等存储）。
- `src/service.py`：用例编排、机构权限、幂等包装与批量续跑。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线（补件预留/确认/回应/放行/失效/重建/回填均有审计）。
- `static/index.html`：演示页面。
- `tests/`：完整流程、规则、并发名额、政策重算、批量断点、幂等与回填测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8329
```

默认端口`8329`，服务启动时自动建表；对旧库会自动迁移出`organization`、`policy_version`列。

## 主要接口

案件（原有）：

- `GET/POST /api/records`、`GET /api/records/{id}`、`GET /api/records/{id}/audit`、`GET /api/stats`
- `POST /api/records/{id}/actions/{action}`，请求体 `{"expected_version":1,"data":{...}}`

政策与容量：

- `POST /api/policies`：发布政策版本，`data: {"version":"p2","effective_day":116,"basis":{"allowed_days":30}}`，发布即失效重算未回应任务。
- `POST /api/policies/backfill`：把缺政策版本的旧案件按创建时政策回填（幂等）。
- `GET /api/policies`
- `POST /api/capacity`：设置承办人当日名额，`data: {"officer_id":"off-li","day":115,"capacity":2}`；扩容自动放行排队。
- `GET /api/capacity?officer_id=off-li&day=115`：返回 `capacity/used/available/queued`。

补件任务：

- `POST /api/records/{id}/supplements`：发补件，先预留名额，不足则排队。`data: {"officer_id","day","request","request_day"?,"allowed_days"?,"idem_key"?}`。支持 `Idempotency-Key` 请求头。
- `GET /api/supplements?record_id=&status=&officer_id=&day=`、`GET /api/supplements/{id}`
- `POST /api/supplements/{id}/confirm`：书记员确认预留名额（并发只放行一个）。
- `POST /api/supplements/{id}/respond`：法律代表回应，`data: {"response_day":120,"documents":[...]}`；超期 422，重复回应回放原判。
- `POST /api/supplements/{id}/release`：放行名额回当日池，队首排队任务自动获得预留。
- `POST /api/supplements/batch`：批量发补件，`data: {"run_key":"...","items":[{record_id,officer_id,day,request},...]}`；写入失败后用相同`run_key`重试即从最后完整批次继续。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`、`X-Org`。角色：`intake_officer`、`legal_rep`、`case_officer`、`supervisor`、`clerk`（书记员，负责发补件与确认名额）、`admin`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

覆盖：完整流程、规则计算、重复引用、版本冲突、名额预留与排队、8线程并发不超额、两人并发确认只过一个、幂等不重占名额、政策更新失效重算/已回应保留、批量写入失败断点续跑、机构越权403、旧案件政策回填。
