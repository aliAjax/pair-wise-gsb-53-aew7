"""SQLite 表结构与事务访问。"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .domain import Conflict, NotFound


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


BASELINE_POLICY = {
    "version": "baseline",
    "effective_day": 0,
    "evidence_days": 10,
}

# 补件任务状态：reserved（已预留名额）/ queued（容量不足排队）/ confirmed（已发出）/
# released（名额已释放）/ responded（已回应，锁定依据）/ voided（政策更新失效）
TASK_RESERVED = "reserved"
TASK_QUEUED = "queued"
TASK_CONFIRMED = "confirmed"
TASK_RELEASED = "released"
TASK_RESPONDED = "responded"
TASK_VOIDED = "voided"


def _task_row(row: sqlite3.Row) -> Dict[str, Any]:
    item = dict(row)
    return item


class Repository:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
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
                    organization TEXT NOT NULL DEFAULT '',
                    policy_version TEXT NOT NULL DEFAULT '',
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS policies (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    version TEXT NOT NULL UNIQUE,
                    effective_day INTEGER NOT NULL,
                    evidence_days INTEGER NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS evidence_tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id),
                    policy_version TEXT NOT NULL,
                    handler_id TEXT NOT NULL,
                    day INTEGER NOT NULL,
                    due_day INTEGER NOT NULL,
                    evidence_request TEXT NOT NULL,
                    status TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    documents TEXT NOT NULL DEFAULT '[]',
                    response_day INTEGER,
                    version INTEGER NOT NULL DEFAULT 1,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(record_id, idempotency_key)
                );
                CREATE TABLE IF NOT EXISTS capacities (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    handler_id TEXT NOT NULL,
                    day INTEGER NOT NULL,
                    capacity INTEGER NOT NULL,
                    updated_by TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(handler_id, day)
                );
                CREATE TABLE IF NOT EXISTS slot_reservations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id INTEGER NOT NULL REFERENCES evidence_tasks(id),
                    handler_id TEXT NOT NULL,
                    day INTEGER NOT NULL,
                    held INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    UNIQUE(task_id)
                );
                CREATE TABLE IF NOT EXISTS idempotent_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_key TEXT NOT NULL UNIQUE,
                    kind TEXT NOT NULL,
                    result TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS rfe_batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_key TEXT NOT NULL UNIQUE,
                    last_completed_index INTEGER NOT NULL DEFAULT -1,
                    total INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'running',
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_tasks_handler_day ON evidence_tasks(handler_id, day, status);
                CREATE INDEX IF NOT EXISTS idx_tasks_record ON evidence_tasks(record_id, status);
                CREATE INDEX IF NOT EXISTS idx_slots_held ON slot_reservations(handler_id, day, held);
                """
            )
            self._migrate_columns(connection)
            self._seed_and_backfill(connection)

    @staticmethod
    def _migrate_columns(connection: sqlite3.Connection) -> None:
        """为旧版本数据库补齐新增列（SQLite 无 IF NOT EXISTS，按现有列集合判断）。"""
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(records)").fetchall()}
        if "organization" not in columns:
            connection.execute("ALTER TABLE records ADD COLUMN organization TEXT NOT NULL DEFAULT ''")
        if "policy_version" not in columns:
            connection.execute("ALTER TABLE records ADD COLUMN policy_version TEXT NOT NULL DEFAULT ''")
        audit_columns = {row["name"] for row in connection.execute("PRAGMA table_info(audit_events)").fetchall()}
        if audit_columns:
            # 旧版 audit_events.record_id 为 NOT NULL，重建为可空以承载政策类系统事件
            sql = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='audit_events'"
            ).fetchone()["sql"]
            if "record_id INTEGER NOT NULL" in sql:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS audit_events_new (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        record_id INTEGER,
                        action TEXT NOT NULL,
                        actor_id TEXT NOT NULL,
                        version INTEGER NOT NULL,
                        details TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    );
                    INSERT INTO audit_events_new(id,record_id,action,actor_id,version,details,created_at)
                    SELECT id,record_id,action,actor_id,version,details,created_at FROM audit_events;
                    DROP TABLE audit_events;
                    ALTER TABLE audit_events_new RENAME TO audit_events;
                    CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);
                    """
                )

    @staticmethod
    def _seed_and_backfill(connection: sqlite3.Connection) -> None:
        """播种基线政策；旧案件回填创建时政策；旧审计补齐 record_id 列。"""
        now = _now()
        connection.execute(
            "INSERT OR IGNORE INTO policies(version,effective_day,evidence_days,created_by,created_at) VALUES(?,?,?,?,?)",
            (BASELINE_POLICY["version"], BASELINE_POLICY["effective_day"], BASELINE_POLICY["evidence_days"], "system", now),
        )
        connection.execute(
            "UPDATE records SET policy_version=? WHERE policy_version IS NULL OR policy_version=''",
            (BASELINE_POLICY["version"],),
        )

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    def create(self, reference: str, state: str, payload: Dict[str, Any], actor_id: str, organization: str = "", policy_version: str = "") -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                policy = self.policy_for_day_connection(connection, int(payload.get("received_day", 0))) if not policy_version else None
                final_policy = policy_version or policy["version"]
                cursor = connection.execute(
                    "INSERT INTO records(reference,state,version,organization,policy_version,payload,created_by,updated_by,created_at,updated_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (reference, state, 1, organization, final_policy, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, actor_id, now, now),
                )
                record_id = int(cursor.lastrowid)
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (record_id, "created", actor_id, 1, json.dumps({"state": state, "policy_version": final_policy}, ensure_ascii=False, sort_keys=True), now),
                )
                row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
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

    def list_records(self, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            if state:
                rows = connection.execute("SELECT * FROM records WHERE state=? ORDER BY id DESC LIMIT ?", (state, limit)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM records ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(row) for row in rows]

    def mutate(self, record_id: int, expected_version: int, state: str, payload: Dict[str, Any], actor_id: str, action: str, details: Dict[str, Any]) -> Dict[str, Any]:
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
            connection.commit()
        return self._row(result)

    def add_audit(self, record_id: Optional[int], actor_id: str, action: str, details: Dict[str, Any]) -> None:
        with self._connect() as connection:
            if record_id is not None:
                row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
                if row is None:
                    raise NotFound("记录不存在")
                version = int(row["version"])
            else:
                version = 0
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, version, json.dumps(details, ensure_ascii=False, sort_keys=True), _now()),
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

    # ---- 政策版本 ----
    def policy_for_day_connection(self, connection: sqlite3.Connection, day: int) -> Dict[str, Any]:
        row = connection.execute(
            "SELECT * FROM policies WHERE effective_day<=? ORDER BY effective_day DESC, id DESC LIMIT 1",
            (day,),
        ).fetchone()
        if row is None:
            raise NotFound("当天没有适用政策版本")
        return dict(row)

    def policy_for_day(self, day: int) -> Dict[str, Any]:
        with self._connect() as connection:
            return self.policy_for_day_connection(connection, day)

    def get_policy(self, version: str) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM policies WHERE version=?", (version,)).fetchone()
        if row is None:
            raise NotFound("政策版本不存在")
        return dict(row)

    def list_policies(self) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM policies ORDER BY effective_day DESC, id DESC").fetchall()
        return [dict(row) for row in rows]

    def publish_policy(self, policy: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        """发布政策：未回应任务失效并按新版本重算；已回应任务保留原依据。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = connection.execute(
                    "INSERT INTO policies(version,effective_day,evidence_days,created_by,created_at) VALUES(?,?,?,?,?)",
                    (policy["version"], policy["effective_day"], policy["evidence_days"], actor_id, now),
                )
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise Conflict("政策版本已存在") from exc
            new_policy_id = int(cursor.lastrowid)

            active_rows = connection.execute(
                "SELECT * FROM evidence_tasks WHERE status IN (?,?,?)",
                (TASK_RESERVED, TASK_QUEUED, TASK_CONFIRMED),
            ).fetchall()
            voided = 0
            reissued = 0
            for old in active_rows:
                old = dict(old)
                connection.execute(
                    "UPDATE evidence_tasks SET status=?, updated_at=? WHERE id=?",
                    (TASK_VOIDED, now, old["id"]),
                )
                connection.execute("UPDATE slot_reservations SET held=0 WHERE task_id=? AND held=1", (old["id"],))
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (old["record_id"], "task_voided", actor_id, int(old["version"]),
                     json.dumps({"task_id": old["id"], "old_policy": old["policy_version"], "new_policy": policy["version"]}, ensure_ascii=False, sort_keys=True), now),
                )
                voided += 1

                # 按新政策重算并尝试重新预留同一承办人当日名额
                due_day = policy["effective_day"] + policy["evidence_days"]
                new_key = "policy-%s:%s" % (policy["version"], old["id"])
                new_cursor = connection.execute(
                    "INSERT INTO evidence_tasks(record_id,policy_version,handler_id,day,due_day,evidence_request,status,idempotency_key,created_by,created_at,updated_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (old["record_id"], policy["version"], old["handler_id"], policy["effective_day"], due_day,
                     old["evidence_request"], TASK_QUEUED, new_key, actor_id, now, now),
                )
                new_task_id = int(new_cursor.lastrowid)
                held = self._try_hold_slot_connection(connection, old["handler_id"], policy["effective_day"])
                if held:
                    connection.execute(
                        "UPDATE evidence_tasks SET status=? WHERE id=?",
                        (TASK_RESERVED, new_task_id),
                    )
                    connection.execute(
                        "INSERT INTO slot_reservations(task_id,handler_id,day,held,created_at) VALUES(?,?,?,1,?)",
                        (new_task_id, old["handler_id"], policy["effective_day"], now),
                    )
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (old["record_id"], "task_reissued", actor_id, 1,
                     json.dumps({"task_id": new_task_id, "source_task_id": old["id"], "policy_version": policy["version"],
                                 "queued": not held}, ensure_ascii=False, sort_keys=True), now),
                )
                reissued += 1

            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (None, "policy_published", actor_id, 1,
                 json.dumps({"version": policy["version"], "voided": voided, "reissued": reissued}, ensure_ascii=False, sort_keys=True), now),
            )
            row = connection.execute("SELECT * FROM policies WHERE id=?", (new_policy_id,)).fetchone()
            connection.commit()
        result = dict(row)
        result["voided"] = voided
        result["reissued"] = reissued
        return result

    # ---- 承办容量与名额 ----
    def upsert_capacity(self, handler_id: str, day: int, capacity: int, actor_id: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM capacities WHERE handler_id=? AND day=?", (handler_id, day)).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO capacities(handler_id,day,capacity,updated_by,updated_at) VALUES(?,?,?,?,?)",
                    (handler_id, day, capacity, actor_id, now),
                )
            else:
                connection.execute(
                    "UPDATE capacities SET capacity=?,updated_by=?,updated_at=? WHERE handler_id=? AND day=?",
                    (capacity, actor_id, now, handler_id, day),
                )
            # 容量提升后尝试让排队任务转入预留
            promoted = self._promote_queued_connection(connection, handler_id, day, now)
            result = connection.execute("SELECT * FROM capacities WHERE handler_id=? AND day=?", (handler_id, day)).fetchone()
            connection.commit()
        item = dict(result)
        item["promoted"] = promoted
        return item

    def get_capacity(self, handler_id: str, day: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM capacities WHERE handler_id=? AND day=?", (handler_id, day)).fetchone()
            held = self._held_count_connection(connection, handler_id, day)
        if row is None:
            return {"handler_id": handler_id, "day": day, "capacity": 0, "held": held, "available": -held}
        item = dict(row)
        item["held"] = held
        item["available"] = int(row["capacity"]) - held
        return item

    @staticmethod
    def _held_count_connection(connection: sqlite3.Connection, handler_id: str, day: int) -> int:
        row = connection.execute(
            "SELECT COUNT(*) AS total FROM slot_reservations WHERE handler_id=? AND day=? AND held=1",
            (handler_id, day),
        ).fetchone()
        return int(row["total"])

    def _try_hold_slot_connection(self, connection: sqlite3.Connection, handler_id: str, day: int) -> bool:
        """调用方必须已持有写事务。容量不足返回 False（排队）。"""
        cap_row = connection.execute("SELECT capacity FROM capacities WHERE handler_id=? AND day=?", (handler_id, day)).fetchone()
        capacity = int(cap_row["capacity"]) if cap_row is not None else 0
        held = self._held_count_connection(connection, handler_id, day)
        return held < capacity

    @staticmethod
    def _promote_queued_connection(connection: sqlite3.Connection, handler_id: str, day: int, now: str) -> int:
        promoted = 0
        queued = connection.execute(
            "SELECT * FROM evidence_tasks WHERE handler_id=? AND day=? AND status=? ORDER BY id",
            (handler_id, day, TASK_QUEUED),
        ).fetchall()
        for task in queued:
            cap_row = connection.execute("SELECT capacity FROM capacities WHERE handler_id=? AND day=?", (handler_id, day)).fetchone()
            capacity = int(cap_row["capacity"]) if cap_row is not None else 0
            held_row = connection.execute(
                "SELECT COUNT(*) AS total FROM slot_reservations WHERE handler_id=? AND day=? AND held=1",
                (handler_id, day),
            ).fetchone()
            if int(held_row["total"]) >= capacity:
                break
            task_id = int(dict(task)["id"])
            connection.execute("UPDATE evidence_tasks SET status=?,updated_at=? WHERE id=?", (TASK_RESERVED, now, task_id))
            connection.execute(
                "INSERT INTO slot_reservations(task_id,handler_id,day,held,created_at) VALUES(?,?,?,1,?)",
                (task_id, handler_id, day, now),
            )
            promoted += 1
        return promoted

    # ---- 补件任务 ----
    def reserve_evidence_task(self, task: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        """发补件：预留承办人当日名额；容量不足排队；重复请求幂等。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self.get_record_connection(connection, task["record_id"])
            existing = connection.execute(
                "SELECT * FROM evidence_tasks WHERE record_id=? AND idempotency_key=?",
                (task["record_id"], task["idempotency_key"]),
            ).fetchone()
            if existing is not None:
                connection.commit()
                item = _task_row(existing)
                item["idempotent_replayed"] = True
                return item
            cursor = connection.execute(
                "INSERT INTO evidence_tasks(record_id,policy_version,handler_id,day,due_day,evidence_request,status,idempotency_key,created_by,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (task["record_id"], task["policy_version"], task["handler_id"], task["day"], task["due_day"],
                 task["evidence_request"], TASK_QUEUED, task["idempotency_key"], actor_id, now, now),
            )
            task_id = int(cursor.lastrowid)
            queued = True
            if self._try_hold_slot_connection(connection, task["handler_id"], task["day"]):
                connection.execute(
                    "UPDATE evidence_tasks SET status=? WHERE id=?",
                    (TASK_RESERVED, task_id),
                )
                connection.execute(
                    "INSERT INTO slot_reservations(task_id,handler_id,day,held,created_at) VALUES(?,?,?,1,?)",
                    (task_id, task["handler_id"], task["day"], now),
                )
                queued = False
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (task["record_id"], "task_reserved", actor_id, 1,
                 json.dumps({"task_id": task_id, "handler_id": task["handler_id"], "day": task["day"],
                             "policy_version": task["policy_version"], "queued": queued}, ensure_ascii=False, sort_keys=True), now),
            )
            row = connection.execute("SELECT * FROM evidence_tasks WHERE id=?", (task_id,)).fetchone()
            connection.commit()
        return _task_row(row)

    def get_task_connection(self, connection: sqlite3.Connection, task_id: int) -> Dict[str, Any]:
        row = connection.execute("SELECT * FROM evidence_tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise NotFound("补件任务不存在")
        return _task_row(row)

    def get_task(self, task_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            return self.get_task_connection(connection, task_id)

    @staticmethod
    def get_record_connection(connection: sqlite3.Connection, record_id: int) -> Dict[str, Any]:
        row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise NotFound("记录不存在")
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    def confirm_task(self, task_id: int, expected_version: int, actor_id: str) -> Dict[str, Any]:
        """两人同时确认同一名额：任务版本 CAS，只放行一个。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            task = self.get_task_connection(connection, task_id)
            if int(task["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("任务版本冲突，请刷新后重试")
            if task["status"] != TASK_RESERVED:
                connection.rollback()
                raise Conflict("当前任务状态(%s)不允许确认" % task["status"])
            held = connection.execute(
                "SELECT COUNT(*) AS total FROM slot_reservations WHERE task_id=? AND held=1",
                (task_id,),
            ).fetchone()
            if int(held["total"]) < 1:
                connection.rollback()
                raise Conflict("名额未持有，无法确认")
            new_version = int(expected_version) + 1
            connection.execute(
                "UPDATE evidence_tasks SET status=?,version=?,updated_at=? WHERE id=? AND version=?",
                (TASK_CONFIRMED, new_version, now, task_id, expected_version),
            )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (task["record_id"], "task_confirmed", actor_id, new_version,
                 json.dumps({"task_id": task_id, "handler_id": task["handler_id"], "day": task["day"]}, ensure_ascii=False, sort_keys=True), now),
            )
            result = self.get_task_connection(connection, task_id)
            connection.commit()
        return result

    def release_task(self, task_id: int, expected_version: int, actor_id: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            task = self.get_task_connection(connection, task_id)
            if int(task["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("任务版本冲突，请刷新后重试")
            if task["status"] not in (TASK_RESERVED, TASK_CONFIRMED):
                connection.rollback()
                raise Conflict("当前任务状态(%s)不允许释放" % task["status"])
            connection.execute("UPDATE slot_reservations SET held=0 WHERE task_id=? AND held=1", (task_id,))
            new_version = int(expected_version) + 1
            connection.execute(
                "UPDATE evidence_tasks SET status=?,version=?,updated_at=? WHERE id=? AND version=?",
                (TASK_RELEASED, new_version, now, task_id, expected_version),
            )
            now2 = _now()
            promoted = self._promote_queued_connection(connection, task["handler_id"], task["day"], now2)
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (task["record_id"], "task_released", actor_id, new_version,
                 json.dumps({"task_id": task_id, "promoted": promoted}, ensure_ascii=False, sort_keys=True), now),
            )
            result = self.get_task_connection(connection, task_id)
            connection.commit()
        return result

    def respond_task(self, task_id: int, expected_version: int, response_day: int, documents: List[str], actor_id: str) -> Dict[str, Any]:
        """已回应任务锁定原政策依据，重复回应被拒绝。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            task = self.get_task_connection(connection, task_id)
            if int(task["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("任务版本冲突，请刷新后重试")
            if task["status"] not in (TASK_CONFIRMED, TASK_RESERVED):
                connection.rollback()
                raise Conflict("当前任务状态(%s)不允许回应" % task["status"])
            if response_day > int(task["due_day"]):
                connection.rollback()
                from .domain import ValidationError
                raise ValidationError("补件回应超过期限")
            new_version = int(expected_version) + 1
            connection.execute(
                "UPDATE evidence_tasks SET status=?,response_day=?,documents=?,version=?,updated_at=? WHERE id=? AND version=?",
                (TASK_RESPONDED, response_day, json.dumps(documents, ensure_ascii=False), new_version, now, task_id, expected_version),
            )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (task["record_id"], "task_responded", actor_id, new_version,
                 json.dumps({"task_id": task_id, "policy_version": task["policy_version"], "response_day": response_day}, ensure_ascii=False, sort_keys=True), now),
            )
            result = self.get_task_connection(connection, task_id)
            connection.commit()
        return result

    def list_tasks(self, record_id: Optional[int] = None, handler_id: Optional[str] = None, status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM evidence_tasks WHERE 1=1"
        args: List[Any] = []
        if record_id is not None:
            sql += " AND record_id=?"
            args.append(record_id)
        if handler_id:
            sql += " AND handler_id=?"
            args.append(handler_id)
        if status:
            sql += " AND status=?"
            args.append(status)
        sql += " ORDER BY id"
        with self._connect() as connection:
            rows = connection.execute(sql, args).fetchall()
        return [_task_row(row) for row in rows]

    # ---- 幂等与批次断点 ----
    def load_idempotent(self, request_key: str) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM idempotent_requests WHERE request_key=?", (request_key,)).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["result"] = json.loads(item["result"])
        return item

    def save_idempotent(self, request_key: str, kind: str, result: Dict[str, Any]) -> None:
        with self._connect() as connection:
            try:
                connection.execute(
                    "INSERT INTO idempotent_requests(request_key,kind,result,created_at) VALUES(?,?,?,?)",
                    (request_key, kind, json.dumps(result, ensure_ascii=False, sort_keys=True), _now()),
                )
            except sqlite3.IntegrityError:
                pass

    def get_batch(self, batch_key: str) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM rfe_batches WHERE batch_key=?", (batch_key,)).fetchone()
        return dict(row) if row is not None else None

    def create_batch(self, batch_key: str, total: int, actor_id: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            try:
                connection.execute(
                    "INSERT INTO rfe_batches(batch_key,last_completed_index,total,status,created_by,created_at,updated_at)"
                    " VALUES(?,? ,?,'running',?,?,?)",
                    (batch_key, -1, total, actor_id, now, now),
                )
            except sqlite3.IntegrityError:
                pass
            row = connection.execute("SELECT * FROM rfe_batches WHERE batch_key=?", (batch_key,)).fetchone()
        return dict(row)

    def advance_batch(self, batch_key: str, last_index: int, status: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE rfe_batches SET last_completed_index=?,status=?,updated_at=? WHERE batch_key=?",
                (last_index, status, now, batch_key),
            )
            row = connection.execute("SELECT * FROM rfe_batches WHERE batch_key=?", (batch_key,)).fetchone()
            connection.commit()
        return dict(row)
