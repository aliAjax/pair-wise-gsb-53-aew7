"""SQLite 表结构、迁移与事务访问。

新增四域联动：
- policies：政策版本（发补件时快照其依据）。
- officer_capacity / quota_slots：承办人当日容量与名额（预留/确认/占用/放行）。
- supplement_tasks：补件任务（关联案件、名额、政策版本，支持排队与失效重算）。
- batch_runs：批量补发的断点（从最后完整批次继续）。
- idempotent_requests：重复请求不重复占名额。

所有写操作在单个 BEGIN IMMEDIATE 事务内完成；并发下名额靠
唯一部分索引 + 条件 UPDATE（CAS）兜底，超容量只能排队。
"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .domain import Conflict, NotFound


# 名额状态
SLOT_OPEN = "open"            # 由容量衍生的可占名额
SLOT_RESERVED = "reserved"    # 已预留给某补件任务，待确认
SLOT_CONFIRMED = "confirmed"  # 承办人已确认
SLOT_OCCUPIED = "occupied"    # 补件已回应，名额实际消耗
SLOT_RELEASED = "released"    # 放行（回应/失效后释放），可重新分配

# 补件任务状态
TASK_QUEUED = "queued"
TASK_RESERVED = "reserved"
TASK_CONFIRMED = "confirmed"
TASK_RESPONDED = "responded"
TASK_VOIDED = "voided"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Repository:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._init_schema()
        # 批量补发失败注入：{(run_key): {fail_before_index: n}}，测试用
        self._batch_fail_points: Dict[str, int] = {}
        # 提交前钩子：在写事务 commit 前回调，测试可用来模拟“写入失败”
        self.pre_commit_hook = None

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reference TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);

                CREATE TABLE IF NOT EXISTS policies (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    version TEXT NOT NULL UNIQUE,
                    effective_day INTEGER NOT NULL,
                    basis TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_policies_effective ON policies(effective_day);

                CREATE TABLE IF NOT EXISTS officer_capacity (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    officer_id TEXT NOT NULL,
                    day INTEGER NOT NULL,
                    capacity INTEGER NOT NULL,
                    updated_by TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(officer_id, day)
                );

                CREATE TABLE IF NOT EXISTS quota_slots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    officer_id TEXT NOT NULL,
                    day INTEGER NOT NULL,
                    seq INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    task_id INTEGER,
                    updated_at TEXT NOT NULL,
                    UNIQUE(officer_id, day, seq)
                );
                -- 每个承办人每天处于占用态（预留/确认/占用）的名额最多一条 seq 对一个任务，
                -- 由“条件插入 + 计数”保证不超过 capacity；排队任务不占此唯一资源。
                CREATE UNIQUE INDEX IF NOT EXISTS idx_slot_active_task
                    ON quota_slots(task_id)
                    WHERE task_id IS NOT NULL AND status IN ('reserved', 'confirmed', 'occupied');
                CREATE INDEX IF NOT EXISTS idx_slot_lookup ON quota_slots(officer_id, day, status);

                CREATE TABLE IF NOT EXISTS supplement_tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reference TEXT NOT NULL UNIQUE,
                    record_id INTEGER NOT NULL REFERENCES records(id),
                    organization TEXT NOT NULL DEFAULT '',
                    officer_id TEXT NOT NULL,
                    day INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    request TEXT NOT NULL,
                    request_day INTEGER NOT NULL,
                    due_day INTEGER NOT NULL,
                    policy_version TEXT,
                    policy_basis TEXT,
                    idem_key TEXT NOT NULL DEFAULT '',
                    slot_id INTEGER,
                    supersedes_id INTEGER REFERENCES supplement_tasks(id),
                    response_day INTEGER,
                    response_docs TEXT,
                    responded_by TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                -- 同一幂等键只允许一条“仍有效”的任务（voided 的旧任务让出键）
                CREATE UNIQUE INDEX IF NOT EXISTS idx_supplement_idem
                    ON supplement_tasks(idem_key)
                    WHERE idem_key <> '' AND status <> 'voided';
                CREATE INDEX IF NOT EXISTS idx_supplement_record ON supplement_tasks(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_supplement_officer ON supplement_tasks(officer_id, day, status);
                CREATE INDEX IF NOT EXISTS idx_supplement_queue ON supplement_tasks(status, id);

                CREATE TABLE IF NOT EXISTS batch_runs (
                    run_key TEXT PRIMARY KEY,
                    organization TEXT NOT NULL DEFAULT '',
                    items TEXT NOT NULL,
                    last_completed_index INTEGER NOT NULL DEFAULT -1,
                    status TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS idempotent_requests (
                    idem_key TEXT PRIMARY KEY,
                    scope TEXT NOT NULL,
                    status_code INTEGER NOT NULL,
                    response TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            self._migrate_records(connection)
            connection.execute("PRAGMA user_version = 1")

    def _migrate_records(self, connection: sqlite3.Connection) -> None:
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(records)").fetchall()}
        if "organization" not in columns:
            connection.execute("ALTER TABLE records ADD COLUMN organization TEXT NOT NULL DEFAULT ''")
        if "policy_version" not in columns:
            connection.execute("ALTER TABLE records ADD COLUMN policy_version TEXT")

    # ---- 通用 ----

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    def _fire_hook(self, connection: sqlite3.Connection, tag: str) -> None:
        if self.pre_commit_hook is not None:
            self.pre_commit_hook(connection, tag)

    # ---- 案件 records ----

    def create(self, reference: str, state: str, payload: Dict[str, Any], actor_id: str,
               organization: str = "", policy_version: Optional[str] = None) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.execute(
                    "INSERT INTO records(reference,state,version,payload,created_by,updated_by,created_at,updated_at,organization,policy_version)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (reference, state, 1, json.dumps(payload, ensure_ascii=False, sort_keys=True),
                     actor_id, actor_id, now, now, organization, policy_version),
                )
                record_id = int(cursor.lastrowid)
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (record_id, "created", actor_id, 1,
                     json.dumps({"state": state, "organization": organization, "policy_version": policy_version},
                                ensure_ascii=False, sort_keys=True), now),
                )
                row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
                self._fire_hook(connection, "record.create")
                connection.commit()
        except sqlite3.IntegrityError as exc:
            raise Conflict("reference已存在") from exc
        return self._row(row)

    def get(self, record_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise NotFound("记录不存在")
        return self._row(row)

    def list_records(self, state: Optional[str] = None, limit: int = 100,
                     organization: Optional[str] = None) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            if state and organization is not None:
                rows = connection.execute(
                    "SELECT * FROM records WHERE state=? AND organization=? ORDER BY id DESC LIMIT ?",
                    (state, organization, limit)).fetchall()
            elif state:
                rows = connection.execute("SELECT * FROM records WHERE state=? ORDER BY id DESC LIMIT ?", (state, limit)).fetchall()
            elif organization is not None:
                rows = connection.execute(
                    "SELECT * FROM records WHERE organization=? ORDER BY id DESC LIMIT ?", (organization, limit)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM records ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(row) for row in rows]

    def mutate(self, record_id: int, expected_version: int, state: str, payload: Dict[str, Any], actor_id: str,
               action: str, details: Dict[str, Any]) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            version = int(expected_version) + 1
            connection.execute(
                "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (state, version, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now, record_id),
            )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, version, json.dumps(details, ensure_ascii=False, sort_keys=True), now),
            )
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            self._fire_hook(connection, "record.mutate")
            connection.commit()
        return self._row(result)

    def backfill_policy_versions(self, actor_id: str = "system") -> int:
        """旧数据：案件缺 policy_version 时，按创建时（received_day）生效政策回填。只回填一次。"""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            policies = [dict(row) for row in connection.execute(
                "SELECT * FROM policies ORDER BY effective_day").fetchall()]
            if not policies:
                connection.commit()
                return 0
            rows = connection.execute(
                "SELECT * FROM records WHERE policy_version IS NULL OR policy_version = '' ORDER BY id").fetchall()
            count = 0
            now = _now()
            for row in rows:
                received_day = int(json.loads(row["payload"]).get("received_day", 0))
                applicable = [p for p in policies if int(p["effective_day"]) <= received_day]
                policy = max(applicable, key=lambda p: int(p["effective_day"])) if applicable else policies[0]
                connection.execute(
                    "UPDATE records SET policy_version=?, updated_at=? WHERE id=?",
                    (policy["version"], now, row["id"]))
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (row["id"], "policy_backfilled", actor_id, int(row["version"]),
                     json.dumps({"policy_version": policy["version"], "reason": "旧数据按创建时政策回填"},
                                ensure_ascii=False), now))
                count += 1
            self._fire_hook(connection, "policy.backfill")
            connection.commit()
        return count

    def add_audit(self, record_id: int, actor_id: str, action: str, details: Dict[str, Any]) -> None:
        with self._connect() as connection:
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                raise NotFound("记录不存在")
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, int(row["version"]),
                 json.dumps(details, ensure_ascii=False, sort_keys=True), _now()),
            )

    def audit_timeline(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM audit_events WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result

    def stats(self) -> Dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute("SELECT state, COUNT(*) AS total FROM records GROUP BY state").fetchall()
        return {str(row["state"]): int(row["total"]) for row in rows}

    def health(self) -> bool:
        try:
            with self._connect() as connection:
                connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False

    # ---- 政策版本 policies ----

    def create_policy(self, version: str, effective_day: int, basis: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.execute(
                    "INSERT INTO policies(version,effective_day,basis,created_by,created_at) VALUES(?,?,?,?,?)",
                    (version, effective_day, json.dumps(basis, ensure_ascii=False, sort_keys=True), actor_id, now))
                policy_id = int(cursor.lastrowid)
                row = connection.execute("SELECT * FROM policies WHERE id=?", (policy_id,)).fetchone()
                self._fire_hook(connection, "policy.create")
                connection.commit()
        except sqlite3.IntegrityError as exc:
            raise Conflict("政策版本已存在") from exc
        return self._policy_row(row)

    def list_policies(self) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM policies ORDER BY effective_day, id").fetchall()
        return [self._policy_row(row) for row in rows]

    def get_policy(self, version: str) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM policies WHERE version=?", (version,)).fetchone()
        if row is None:
            raise NotFound("政策版本不存在")
        return self._policy_row(row)

    def policy_at(self, day: int) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM policies WHERE effective_day <= ? ORDER BY effective_day DESC, id DESC LIMIT 1",
                (day,)).fetchone()
            if row is None:
                row = connection.execute(
                    "SELECT * FROM policies ORDER BY effective_day ASC, id ASC LIMIT 1").fetchone()
        return self._policy_row(row) if row is not None else None

    @staticmethod
    def _policy_row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["basis"] = json.loads(item["basis"])
        return item

    def apply_new_policy(self, new_policy: Dict[str, Any], actor_id: str) -> Dict[str, int]:
        """政策更新：未回应任务（排队/预留/确认）失效并按新依据重算；已回应保留原依据。"""
        statuses = (TASK_QUEUED, TASK_RESERVED, TASK_CONFIRMED)
        now = _now()
        allowed_days = int(new_policy.get("basis", {}).get("allowed_days", 10))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            open_tasks = [dict(row) for row in connection.execute(
                "SELECT * FROM supplement_tasks WHERE status IN (?,?,?) ORDER BY id", statuses).fetchall()]
            voided = 0
            created = 0
            for old in open_tasks:
                # 释放已占名额，回到当日名额池供排队任务复用
                if old["slot_id"] is not None:
                    connection.execute(
                        "UPDATE quota_slots SET status=?, task_id=NULL, updated_at=? WHERE id=? AND status IN (?,?)",
                        (SLOT_OPEN, now, old["slot_id"], SLOT_RESERVED, SLOT_CONFIRMED))
                connection.execute(
                    "UPDATE supplement_tasks SET status=?, updated_at=? WHERE id=?",
                    (TASK_VOIDED, now, old["id"]))
                self._task_audit(connection, old["record_id"], actor_id, "supplement_voided", {
                    "task_id": old["id"], "reason": "政策更新，未回应任务失效",
                    "old_policy_version": old["policy_version"], "new_policy_version": new_policy["version"]})
                new_due = int(old["request_day"]) + allowed_days
                cursor = connection.execute(
                    "INSERT INTO supplement_tasks(reference,record_id,organization,officer_id,day,status,request,"
                    "request_day,due_day,policy_version,policy_basis,idem_key,slot_id,supersedes_id,created_by,created_at,updated_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    ("SUP-%d-v%s" % (old["id"], new_policy["version"].replace(".", "_")),
                     old["record_id"], old["organization"], old["officer_id"], old["day"], TASK_QUEUED,
                     old["request"], old["request_day"], new_due, new_policy["version"],
                     json.dumps(new_policy.get("basis", {}), ensure_ascii=False, sort_keys=True),
                     "", None, old["id"], actor_id, now, now))
                new_id = int(cursor.lastrowid)
                self._task_audit(connection, old["record_id"], actor_id, "supplement_recreated", {
                    "task_id": new_id, "supersedes_id": old["id"], "policy_version": new_policy["version"],
                    "due_day": new_due, "queued": True})
                voided += 1
                created += 1
            promoted = self._promote_queue_locked(connection)
            self._fire_hook(connection, "policy.apply")
            connection.commit()
        return {"voided": voided, "recreated": created, "promoted": promoted}

    # ---- 容量与名额 ----

    def set_capacity(self, officer_id: str, day: int, capacity: int, actor_id: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM officer_capacity WHERE officer_id=? AND day=?", (officer_id, day)).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO officer_capacity(officer_id,day,capacity,updated_by,updated_at) VALUES(?,?,?,?,?)",
                    (officer_id, day, capacity, actor_id, now))
            else:
                connection.execute(
                    "UPDATE officer_capacity SET capacity=?, updated_by=?, updated_at=? WHERE id=?",
                    (capacity, actor_id, now, row["id"]))
            self._ensure_slots_locked(connection, officer_id, day)
            promoted = self._promote_queue_locked(connection, officer_id, day)
            result = connection.execute(
                "SELECT * FROM officer_capacity WHERE officer_id=? AND day=?", (officer_id, day)).fetchone()
            self._fire_hook(connection, "capacity.set")
            connection.commit()
        item = dict(result)
        item["promoted"] = promoted
        return item

    def _ensure_slots_locked(self, connection: sqlite3.Connection, officer_id: str, day: int) -> None:
        """容量增加时补足 open 名额（seq 从 1 连续编号）。"""
        cap_row = connection.execute(
            "SELECT capacity FROM officer_capacity WHERE officer_id=? AND day=?", (officer_id, day)).fetchone()
        capacity = int(cap_row["capacity"]) if cap_row is not None else 0
        existing = int(connection.execute(
            "SELECT COUNT(*) AS c FROM quota_slots WHERE officer_id=? AND day=?", (officer_id, day)).fetchone()["c"])
        for seq in range(existing + 1, capacity + 1):
            connection.execute(
                "INSERT INTO quota_slots(officer_id,day,seq,status,task_id,updated_at) VALUES(?,?,?,?,?,?)",
                (officer_id, day, seq, SLOT_OPEN, None, _now()))

    @staticmethod
    def _active_count_locked(connection: sqlite3.Connection, officer_id: str, day: int) -> int:
        return int(connection.execute(
            "SELECT COUNT(*) AS c FROM quota_slots WHERE officer_id=? AND day=? "
            "AND status IN (?,?,?)",
            (officer_id, day, SLOT_RESERVED, SLOT_CONFIRMED, SLOT_OCCUPIED)).fetchone()["c"])

    def _promote_queue_locked(self, connection: sqlite3.Connection,
                              officer_id: Optional[str] = None, day: Optional[int] = None) -> int:
        """FIFO：出现可用名额时，最早的排队任务获得预留。返回提升条数。"""
        promoted = 0
        while True:
            if officer_id is not None and day is not None:
                task_row = connection.execute(
                    "SELECT * FROM supplement_tasks WHERE status=? AND officer_id=? AND day=?"
                    " ORDER BY updated_at, id LIMIT 1",
                    (TASK_QUEUED, officer_id, day)).fetchone()
            else:
                task_row = connection.execute(
                    "SELECT * FROM supplement_tasks WHERE status=? ORDER BY updated_at, id LIMIT 1",
                    (TASK_QUEUED,)).fetchone()
            if task_row is None:
                break
            task = dict(task_row)
            cap_row = connection.execute(
                "SELECT capacity FROM officer_capacity WHERE officer_id=? AND day=?",
                (task["officer_id"], task["day"])).fetchone()
            if cap_row is None:
                if officer_id is not None:
                    break
                break
            self._ensure_slots_locked(connection, task["officer_id"], task["day"])
            active = self._active_count_locked(connection, task["officer_id"], task["day"])
            if active >= int(cap_row["capacity"]):
                break
            slot = connection.execute(
                "SELECT * FROM quota_slots WHERE officer_id=? AND day=? AND status=? ORDER BY seq LIMIT 1",
                (task["officer_id"], task["day"], SLOT_OPEN)).fetchone()
            if slot is None:
                break
            connection.execute(
                "UPDATE quota_slots SET status=?, task_id=?, updated_at=? WHERE id=? AND status=?",
                (SLOT_RESERVED, task["id"], _now(), slot["id"], SLOT_OPEN))
            connection.execute(
                "UPDATE supplement_tasks SET status=?, slot_id=?, updated_at=? WHERE id=? AND status=?",
                (TASK_RESERVED, slot["id"], _now(), task["id"], TASK_QUEUED))
            promoted += 1
        return promoted

    def capacity_view(self, officer_id: str, day: int) -> Dict[str, Any]:
        with self._connect() as connection:
            cap_row = connection.execute(
                "SELECT * FROM officer_capacity WHERE officer_id=? AND day=?", (officer_id, day)).fetchone()
            capacity = int(cap_row["capacity"]) if cap_row is not None else 0
            active = self._active_count_locked(connection, officer_id, day)
            queued = int(connection.execute(
                "SELECT COUNT(*) AS c FROM supplement_tasks WHERE officer_id=? AND day=? AND status=?",
                (officer_id, day, TASK_QUEUED)).fetchone()["c"])
        return {"officer_id": officer_id, "day": day, "capacity": capacity,
                "used": active, "available": max(0, capacity - active), "queued": queued}

    # ---- 补件任务 ----

    def _next_task_ref(self, connection: sqlite3.Connection) -> str:
        row = connection.execute("SELECT seq FROM sqlite_sequence WHERE name='supplement_tasks'").fetchone()
        next_id = (int(row["seq"]) + 1) if row is not None else 1
        return "SUP-%d" % next_id

    def _reserve_locked(self, connection: sqlite3.Connection, record: Dict[str, Any], officer_id: str, day: int,
                        request_text: str, request_day: int, due_day: int, policy: Dict[str, Any],
                        idem_key: str, actor_id: str, reference: Optional[str] = None) -> Dict[str, Any]:
        """在已持有事务内完成“预留或排队”。返回新任务。"""
        now = _now()
        reference = reference or self._next_task_ref(connection)
        cap_row = connection.execute(
            "SELECT capacity FROM officer_capacity WHERE officer_id=? AND day=?", (officer_id, day)).fetchone()
        slot_id: Optional[int] = None
        status = TASK_QUEUED
        if cap_row is not None:
            self._ensure_slots_locked(connection, officer_id, day)
            active = self._active_count_locked(connection, officer_id, day)
            if active < int(cap_row["capacity"]):
                slot = connection.execute(
                    "SELECT * FROM quota_slots WHERE officer_id=? AND day=? AND status=? ORDER BY seq LIMIT 1",
                    (officer_id, day, SLOT_OPEN)).fetchone()
                if slot is not None:
                    slot_id = int(slot["id"])
                    status = TASK_RESERVED
        try:
            cursor = connection.execute(
                "INSERT INTO supplement_tasks(reference,record_id,organization,officer_id,day,status,request,"
                "request_day,due_day,policy_version,policy_basis,idem_key,slot_id,supersedes_id,created_by,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (reference, record["id"], record.get("organization", ""), officer_id, day, status, request_text,
                 request_day, due_day, policy["version"],
                 json.dumps(policy.get("basis", {}), ensure_ascii=False, sort_keys=True),
                 idem_key, slot_id, None, actor_id, now, now))
        except sqlite3.IntegrityError as exc:
            # 并发下同一幂等键已被对方写入：取回原任务，不重复占名额
            if idem_key:
                existing = connection.execute(
                    "SELECT * FROM supplement_tasks WHERE idem_key=? AND status <> ?",
                    (idem_key, TASK_VOIDED)).fetchone()
                if existing is not None:
                    return self._task_row(existing, replayed=True)
            raise Conflict("补件任务已存在") from exc
        task_id = int(cursor.lastrowid)
        if status == TASK_RESERVED and slot_id is not None:
            connection.execute(
                "UPDATE quota_slots SET status=?, task_id=?, updated_at=? WHERE id=? AND status=?",
                (SLOT_RESERVED, task_id, now, slot_id, SLOT_OPEN))
        self._task_audit(connection, record["id"], actor_id, "supplement_issued", {
            "task_id": task_id, "reference": reference, "officer_id": officer_id, "day": day,
            "policy_version": policy["version"], "due_day": due_day,
            "reservation": status, "slot_id": slot_id})
        row = connection.execute("SELECT * FROM supplement_tasks WHERE id=?", (task_id,)).fetchone()
        return self._task_row(row, replayed=False)

    def issue_supplement(self, record: Dict[str, Any], officer_id: str, day: int, request_text: str,
                         request_day: int, due_day: int, policy: Dict[str, Any],
                         idem_key: str, actor_id: str) -> Dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            # 并发：同幂等键已存在则原样返回，不再占名额
            if idem_key:
                existing = connection.execute(
                    "SELECT * FROM supplement_tasks WHERE idem_key=? AND status <> ?",
                    (idem_key, TASK_VOIDED)).fetchone()
                if existing is not None:
                    task = self._task_row(existing, replayed=True)
                    connection.commit()
                    return task
            task = self._reserve_locked(connection, record, officer_id, day, request_text, request_day,
                                        due_day, policy, idem_key, actor_id)
            self._fire_hook(connection, "supplement.issue")
            connection.commit()
        return task

    @staticmethod
    def _task_row(row: sqlite3.Row, replayed: bool = False) -> Dict[str, Any]:
        item = dict(row)
        item["policy_basis"] = json.loads(item["policy_basis"]) if item.get("policy_basis") else {}
        docs = item.get("response_docs")
        item["response_docs"] = json.loads(docs) if docs else []
        item["replayed"] = replayed
        return item

    @staticmethod
    def _task_audit(connection: sqlite3.Connection, record_id: int, actor_id: str,
                    action: str, details: Dict[str, Any]) -> None:
        version_row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
        connection.execute(
            "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
            (record_id, action, actor_id, int(version_row["version"]),
             json.dumps(details, ensure_ascii=False, sort_keys=True), _now()))

    def get_supplement(self, task_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM supplement_tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise NotFound("补件任务不存在")
        return self._task_row(row)

    def get_supplement_record_locked(self, connection: sqlite3.Connection, task_id: int) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM supplement_tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise NotFound("补件任务不存在")
        return row

    def list_supplements(self, record_id: Optional[int] = None, status: Optional[str] = None,
                         officer_id: Optional[str] = None, day: Optional[int] = None,
                         limit: int = 200) -> List[Dict[str, Any]]:
        clauses, params = [], []
        if record_id is not None:
            clauses.append("record_id=?")
            params.append(record_id)
        if status:
            clauses.append("status=?")
            params.append(status)
        if officer_id:
            clauses.append("officer_id=?")
            params.append(officer_id)
        if day is not None:
            clauses.append("day=?")
            params.append(day)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        limit = max(1, min(int(limit), 1000))
        params.append(limit)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM supplement_tasks%s ORDER BY id DESC LIMIT ?" % where, params).fetchall()
        return [self._task_row(row) for row in rows]

    def confirm_slot(self, task_id: int, actor_id: str) -> Dict[str, Any]:
        """书记员确认预留名额：CAS 保证并发只放行一个；重复确认同一任务幂等返回。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self.get_supplement_record_locked(connection, task_id)
            task = dict(row)
            if task["status"] in (TASK_CONFIRMED, TASK_RESPONDED):
                # 名额已被另一笔确认放行：并发下后到者必须被拒绝。
                # 真正的“同一请求重复提交”由服务层幂等键回放，不走到这里。
                connection.rollback()
                raise Conflict("名额已被其他书记员确认")
            if task["status"] != TASK_RESERVED or task["slot_id"] is None:
                connection.rollback()
                raise Conflict("任务当前没有可确认的预留名额（可能仍在排队）")
            cursor = connection.execute(
                "UPDATE quota_slots SET status=?, updated_at=? WHERE id=? AND status=? AND task_id=?",
                (SLOT_CONFIRMED, now, task["slot_id"], SLOT_RESERVED, task_id))
            if cursor.rowcount != 1:
                connection.rollback()
                raise Conflict("名额已被占用或已放行")
            connection.execute(
                "UPDATE supplement_tasks SET status=?, updated_at=? WHERE id=? AND status=?",
                (TASK_CONFIRMED, now, task_id, TASK_RESERVED))
            self._task_audit(connection, task["record_id"], actor_id, "quota_confirmed", {
                "task_id": task_id, "slot_id": task["slot_id"], "officer_id": task["officer_id"]})
            result = connection.execute("SELECT * FROM supplement_tasks WHERE id=?", (task_id,)).fetchone()
            self._fire_hook(connection, "quota.confirm")
            connection.commit()
        return self._task_row(result)

    def respond_supplement(self, task_id: int, response_day: int, documents: List[str], actor_id: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self.get_supplement_record_locked(connection, task_id)
            task = dict(row)
            if task["status"] == TASK_RESPONDED:
                # 重复回应不改变原判、不重复占名额
                result = self._task_row(row, replayed=True)
                connection.commit()
                return result
            if task["status"] != TASK_CONFIRMED:
                connection.rollback()
                raise Conflict("任务尚未确认名额，不能回应")
            connection.execute(
                "UPDATE quota_slots SET status=?, updated_at=? WHERE id=? AND status IN (?,?)",
                (SLOT_OCCUPIED, now, task["slot_id"], SLOT_RESERVED, SLOT_CONFIRMED))
            connection.execute(
                "UPDATE supplement_tasks SET status=?, response_day=?, response_docs=?, responded_by=?, updated_at=? WHERE id=?",
                (TASK_RESPONDED, response_day, json.dumps(documents, ensure_ascii=False), actor_id, now, task_id))
            self._task_audit(connection, task["record_id"], actor_id, "supplement_responded", {
                "task_id": task_id, "response_day": response_day, "documents": documents,
                "policy_version": task["policy_version"], "basis_retained": True})
            result = connection.execute("SELECT * FROM supplement_tasks WHERE id=?", (task_id,)).fetchone()
            self._fire_hook(connection, "supplement.respond")
            connection.commit()
        return self._task_row(result)

    def release_slot(self, task_id: int, actor_id: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self.get_supplement_record_locked(connection, task_id)
            task = dict(row)
            if task["status"] not in (TASK_RESERVED, TASK_CONFIRMED) or task["slot_id"] is None:
                connection.rollback()
                raise Conflict("任务当前没有可放行的预留名额")
            connection.execute(
                "UPDATE quota_slots SET status=?, task_id=NULL, updated_at=? WHERE id=? AND status IN (?,?)",
                (SLOT_OPEN, now, task["slot_id"], SLOT_RESERVED, SLOT_CONFIRMED))
            connection.execute(
                "UPDATE supplement_tasks SET status=?, slot_id=NULL, updated_at=? WHERE id=?",
                (TASK_QUEUED, now, task_id))
            self._task_audit(connection, task["record_id"], actor_id, "quota_released", {
                "task_id": task_id, "slot_id": task["slot_id"]})
            promoted = self._promote_queue_locked(connection, task["officer_id"], task["day"])
            result = connection.execute("SELECT * FROM supplement_tasks WHERE id=?", (task_id,)).fetchone()
            self._fire_hook(connection, "quota.release")
            connection.commit()
        item = self._task_row(result)
        item["promoted"] = promoted
        return item

    # ---- 批量补发（断点续跑）----

    def create_batch_run(self, run_key: str, organization: str, items: List[Dict[str, Any]],
                         actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "INSERT INTO batch_runs(run_key,organization,items,last_completed_index,status,created_by,created_at,updated_at)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    (run_key, organization,
                     json.dumps(items, ensure_ascii=False, sort_keys=True), -1, "created", actor_id, now, now))
                row = connection.execute("SELECT * FROM batch_runs WHERE run_key=?", (run_key,)).fetchone()
                connection.commit()
        except sqlite3.IntegrityError as exc:
            raise Conflict("批次已存在") from exc
        return self._batch_row(row)

    def get_batch_run(self, run_key: str) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM batch_runs WHERE run_key=?", (run_key,)).fetchone()
        if row is None:
            raise NotFound("批次不存在")
        return self._batch_row(row)

    @staticmethod
    def _batch_row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["items"] = json.loads(item["items"])
        return item

    def set_batch_fail_point(self, run_key: str, fail_before_index: int) -> None:
        self._batch_fail_points[run_key] = int(fail_before_index)

    def process_batch_chunk(self, run_key: str, actor_id: str) -> Dict[str, Any]:
        """从 last_completed_index+1 继续；全部完成才提交。写入失败由调用方标记，重试自动续跑。"""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM batch_runs WHERE run_key=?", (run_key,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("批次不存在")
            run = self._batch_row(row)
            items = run["items"]
            start = int(run["last_completed_index"]) + 1
            issued: List[Dict[str, Any]] = []
            now = _now()
            fail_point = self._batch_fail_points.get(run_key)
            for index in range(start, len(items)):
                if fail_point is not None and index == fail_point:
                    # 模拟“写入失败”：事务回滚，本批无一落库，断点停在上一完整批次
                    connection.rollback()
                    self._mark_batch_failed(run_key, "写入失败，索引%s未完成" % index)
                    raise sqlite3.OperationalError("模拟写入失败：索引%s" % index)
                spec = items[index]
                record = connection.execute("SELECT * FROM records WHERE id=?", (spec["record_id"],)).fetchone()
                if record is None:
                    connection.rollback()
                    raise NotFound("记录不存在：%s" % spec["record_id"])
                policy = {"version": spec["policy_version"],
                          "basis": json.loads(spec.get("policy_basis") or "{}")}
                # 批次内重复条目：已成功发过的任务直接取回，不重复占名额
                idem_key = "batch:%s:%s" % (run_key, spec["record_id"])
                existing = connection.execute(
                    "SELECT * FROM supplement_tasks WHERE idem_key=? AND status <> ?",
                    (idem_key, TASK_VOIDED)).fetchone()
                if existing is not None:
                    issued.append(self._task_row(existing, replayed=True))
                else:
                    task = self._reserve_locked(
                        connection, self._row(record), spec["officer_id"], int(spec["day"]),
                        spec["request"], int(spec["request_day"]), int(spec["due_day"]),
                        policy, idem_key, actor_id,
                        reference="%s-%d" % (run_key, index + 1))
                    issued.append(task)
                connection.execute(
                    "UPDATE batch_runs SET last_completed_index=?, status=?, updated_at=? WHERE run_key=?",
                    (index, "running", now, run_key))
            connection.execute(
                "UPDATE batch_runs SET last_completed_index=?, status=?, updated_at=? WHERE run_key=?",
                (len(items) - 1, "completed", now, run_key))
            result_row = connection.execute("SELECT * FROM batch_runs WHERE run_key=?", (run_key,)).fetchone()
            self._fire_hook(connection, "batch.chunk")
            connection.commit()
        self._batch_fail_points.pop(run_key, None)
        result = self._batch_row(result_row)
        result["issued"] = issued
        result["started_at_index"] = start
        return result

    def _mark_batch_failed(self, run_key: str, error: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE batch_runs SET status=?, updated_at=? WHERE run_key=?",
                ("failed:" + error, _now(), run_key))

    # ---- 通用幂等存储 ----

    def idempotent_get(self, idem_key: str) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM idempotent_requests WHERE idem_key=?", (idem_key,)).fetchone()
        if row is None:
            return None
        return {"status_code": int(row["status_code"]),
                "response": json.loads(row["response"])}

    def idempotent_put(self, idem_key: str, scope: str, status_code: int, response: Dict[str, Any]) -> None:
        try:
            with self._connect() as connection:
                connection.execute(
                    "INSERT INTO idempotent_requests(idem_key,scope,status_code,response,created_at) VALUES(?,?,?,?,?)",
                    (idem_key, scope, status_code, json.dumps(response, ensure_ascii=False, sort_keys=True), _now()))
        except sqlite3.IntegrityError:
            # 并发下对方先写：忽略，读取方会拿到对方的结果
            pass
