# 移民案件期限与材料管理

纯Python标准库实现的移民案件期限与材料管理原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、法定天数、补件期限和材料完整性和冲突检查。
- `src/repository.py`：SQLite建表、事务和查询（案件、政策版本、补件任务、容量、名额、批次）。
- `src/service.py`：用例编排、权限/机构检查、乐观并发、幂等和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则计算、失败场景、容量政策与批次续跑测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8329
```

默认端口为`8329`，默认数据库位于项目目录。服务启动时自动建表。

## 主要接口

- `GET /health`：健康检查。
- `GET /`：演示页面。
- `GET /api/records`：记录列表，可带`state`和`limit`参数。
- `GET /api/records/{id}`：记录详情。
- `GET /api/records/{id}/audit`：审计时间线。
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。
- `GET/POST /api/policies`：列出政策版本 / 发布新版本（supervisor、admin），发布体为`{"version":"...","effective_day":120,"evidence_days":5}`。发布时未回应任务失效并按新版本重算，已回应任务保留原依据。
- `GET/POST /api/capacity`：查询/设置承办人当日容量，请求体为`{"handler_id":"H1","day":120,"capacity":3}`；容量提升后排队任务自动补上。
- `GET /api/evidence-tasks`：补件任务列表，支持`record_id`、`handler_id`、`status`过滤。
- `POST /api/evidence-tasks`：发补件并预留承办人当日名额，容量不足返回`queued`，请求体为`{"record_id":1,"handler_id":"H1","day":120,"evidence_request":"..."}`。
- `POST /api/evidence-tasks/{id}/confirm|release|respond`：确认（两人同时确认同一名额只放行一个）、释放名额、回应补件，均需`expected_version`；回应体为`{"response_day":125,"documents":[...]}`。
- `POST /api/rfe-batches`：批量发补件，请求体为`{"batch_key":"B1","items":[...]}`，写入失败后重跑同一批次从最后完整项继续，不重复占名额。

写接口可带`Idempotency-Key`请求头（批量也可放在`batch_key`），重复请求回放原结果，不重复占用名额。

## 联动语义

- **案件—政策版本**：案件创建时按`received_day`落在适用政策版本并快照；旧数据缺版本时回填`baseline`，历史已回应任务保留原判与原依据。
- **补件任务—容量**：发补件先在`BEGIN IMMEDIATE`写事务内检查`capacity - 已持有名额`，有名额则写入`slot_reservations`并置`reserved`，否则置`queued`；名额释放或容量提升后排队任务按序补上。
- **政策更新**：`reserved/queued/confirmed`任务置`voided`并释放名额，按新版本重算`due_day`重新占位；`responded`任务不动。
- **确认并发**：任务行带`version`，确认用乐观锁CAS，并发第二个确认返回409。
- **越权**：案件带`organization`，非本机构（且非admin）读写一律403。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝和版本冲突。
