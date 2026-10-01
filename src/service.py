"""业务用例编排：案件—补件任务—承办容量—政策版本联动。

关键保证：
- 发补件先预留承办人当日名额（事务内计数 + 唯一索引），容量不足则排队。
- 政策更新：未回应任务失效并按新依据重算；已回应任务保留原依据。
- 两人同时确认同一名额只放行一个（CAS 条件更新）。
- 批量补发以 last_completed_index 为断点，写入失败后从最后完整批次继续。
- 重复请求（idem_key）返回首次结果，不重复占名额。
- 机构隔离：只能处理本机构案件，越权拒绝。
- 旧数据缺政策版本：按创建时（received_day）政策回填；历史已回应任务保留原判。
"""
import json
import sqlite3
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, PermissionDenied, ValidationError, text
from .repository import Repository, TASK_RESPONDED
from .rules import (
    CAPACITY_ADMIN_ROLES,
    DEFAULT_ALLOWED_DAYS,
    POLICY_ADMIN_ROLES,
    QUOTA_CONFIRM_ROLES,
    SUPPLEMENT_ISSUE_ROLES,
    SUPPLEMENT_RESPOND_ROLES,
    DomainRules,
)


class Service:
    def __init__(self, repository: Repository, rules: DomainRules, audit: AuditRecorder = None) -> None:
        self.repository = repository
        self.rules = rules
        self.audit = audit or AuditRecorder(repository)
        # 启动即回填旧数据的政策版本
        self.backfill_policy_versions()

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _ensure_known_role(self, actor: Actor) -> None:
        if not self.rules.known_role(actor.role):
            raise PermissionDenied("角色无权访问该服务")

    @staticmethod
    def _require_role(actor: Actor, roles: set) -> None:
        if actor.role != "admin" and actor.role not in roles:
            raise PermissionDenied("角色无权执行该操作")

    @staticmethod
    def _same_org(actor: Actor, organization: str) -> bool:
        """机构隔离：空机构为系统初始化数据，允许；非空必须严格一致。"""
        return organization == "" or actor.organization == organization

    def _require_record_org(self, actor: Actor, record: Dict[str, Any]) -> None:
        if not self._same_org(actor, record.get("organization", "")):
            raise PermissionDenied("无权处理其他机构的案件")

    def _require_task_org(self, actor: Actor, task: Dict[str, Any]) -> None:
        if not self._same_org(actor, task.get("organization", "")):
            raise PermissionDenied("无权处理其他机构的补件任务")

    def _idem(self, idem_key: str, scope: str, producer) -> Dict[str, Any]:
        """通用幂等：重复请求回放首次结果，不执行业务，因此不重复占名额。"""
        if idem_key:
            cached = self.repository.idempotent_get(idem_key)
            if cached is not None:
                result = dict(cached["response"])
                result["replayed"] = True
                return result
        result = producer()
        if idem_key:
            self.repository.idempotent_put(idem_key, scope, 200, result)
        return result

    # ---- 案件 ----

    def create(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create(actor.role):
            raise PermissionDenied("角色无权创建记录")
        reference = text({"reference": reference}, "reference")
        prepared = self.rules.prepare_create(payload or {})
        self.rules.check_create_conflicts(
            prepared, self.repository.list_records(limit=500, organization=actor.organization or None))
        # 新案件直接绑定创建时点生效的政策版本
        policy = self.repository.policy_at(int(prepared["received_day"]))
        policy_version = policy["version"] if policy is not None else None
        record = self.repository.create(
            reference, self.rules.INITIAL_STATE, prepared, actor.user_id,
            organization=actor.organization or "", policy_version=policy_version)
        return record

    def list_records(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_records(state=state, limit=limit, organization=actor.organization or None)

    def get_record(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        record = self.repository.get(record_id)
        self._require_record_org(actor, record)
        return record

    def act(self, actor: Actor, record_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        if not self.rules.role_can_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        record = self.repository.get(record_id)
        self._require_record_org(actor, record)
        self.rules.require_transition(record, action)
        new_state, new_payload, summary = self.rules.apply_action(record, action, data or {})
        return self.repository.mutate(
            record_id=record_id,
            expected_version=int(expected_version),
            state=new_state,
            payload=new_payload,
            actor_id=actor.user_id,
            action=action,
            details={"summary": summary, "input": data or {}, "from": record["state"], "to": new_state},
        )

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        record = self.repository.get(record_id)
        self._require_record_org(actor, record)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()

    # ---- 政策版本 ----

    def publish_policy(self, actor: Actor, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._require_role(actor, POLICY_ADMIN_ROLES)
        validated = self.rules.validate_policy(payload)
        policy = self.repository.create_policy(
            validated["version"], int(validated["effective_day"]), validated["basis"], actor.user_id)
        # 新政策落地：未回应任务失效重算（已回应保留原判）。
        # 旧案件的政策版本回填在启动或显式回填接口进行（一次性，按创建时政策解析）。
        policy["effect"] = self.repository.apply_new_policy(policy, actor.user_id)
        return policy

    def list_policies(self, actor: Actor) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_policies()

    def backfill_policy_versions(self, actor: Optional[Actor] = None) -> int:
        """旧数据：缺政策版本的案件按创建时政策一次性回填。"""
        actor_id = actor.user_id if actor is not None else "system"
        if actor is not None:
            self._ensure_known_role(actor)
            self._require_role(actor, POLICY_ADMIN_ROLES)
        return self.repository.backfill_policy_versions(actor_id)

    # ---- 承办容量 ----

    def set_capacity(self, actor: Actor, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._require_role(actor, CAPACITY_ADMIN_ROLES)
        validated = self.rules.validate_capacity(payload)
        return self.repository.set_capacity(
            validated["officer_id"], int(validated["day"]), int(validated["capacity"]), actor.user_id)

    def capacity_view(self, actor: Actor, officer_id: str, day: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.capacity_view(officer_id, int(day))

    # ---- 补件任务 ----

    def issue_supplement(self, actor: Actor, record_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._require_role(actor, SUPPLEMENT_ISSUE_ROLES)
        record = self.repository.get(record_id)
        self._require_record_org(actor, record)
        validated = self.rules.validate_supplement(payload)
        day = int(validated["day"])
        policy = self.repository.policy_at(day)
        if policy is None:
            raise ValidationError("当前没有可用的政策版本，请先发布政策")
        allowed_days = self.rules.allowed_days(policy, validated.get("allowed_days"))
        due_day = int(validated["request_day"]) + allowed_days
        idem_key = (payload.get("idem_key") or "").strip()

        def do_issue() -> Dict[str, Any]:
            return self.repository.issue_supplement(
                record, validated["officer_id"], day, validated["request"],
                int(validated["request_day"]), due_day, policy, idem_key, actor.user_id)

        return self._idem(idem_key, "issue_supplement", do_issue)

    def confirm_quota(self, actor: Actor, task_id: int, idem_key: str = "") -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._require_role(actor, QUOTA_CONFIRM_ROLES)
        task = self.repository.get_supplement(task_id)
        self._require_task_org(actor, task)

        def do_confirm() -> Dict[str, Any]:
            return self.repository.confirm_slot(task_id, actor.user_id)

        return self._idem((idem_key or "").strip(), "confirm_quota", do_confirm)

    def respond_supplement(self, actor: Actor, task_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._require_role(actor, SUPPLEMENT_RESPOND_ROLES)
        task = self.repository.get_supplement(task_id)
        self._require_task_org(actor, task)
        if task["status"] == TASK_RESPONDED:
            # 历史已回应任务保留原判：重复回应回放原结果
            result = dict(task)
            result["replayed"] = True
            return result
        validated = self.rules.validate_response(payload or {}, int(task["due_day"]))
        return self.repository.respond_supplement(
            task_id, int(validated["response_day"]), validated["documents"], actor.user_id)

    def release_quota(self, actor: Actor, task_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._require_role(actor, QUOTA_CONFIRM_ROLES)
        task = self.repository.get_supplement(task_id)
        self._require_task_org(actor, task)
        return self.repository.release_slot(task_id, actor.user_id)

    def get_supplement(self, actor: Actor, task_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        task = self.repository.get_supplement(task_id)
        self._require_task_org(actor, task)
        return task

    def list_supplements(self, actor: Actor, record_id: Optional[int] = None,
                         status: Optional[str] = None, officer_id: Optional[str] = None,
                         day: Optional[int] = None) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        tasks = self.repository.list_supplements(
            record_id=record_id, status=status, officer_id=officer_id, day=day)
        # 机构隔离：过滤掉其他机构任务
        return [task for task in tasks if self._same_org(actor, task.get("organization", ""))]

    # ---- 批量补发 ----

    def issue_supplement_batch(self, actor: Actor, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._require_role(actor, SUPPLEMENT_ISSUE_ROLES)
        run_key = text(payload or {}, "run_key")
        raw_items = payload.get("items")
        if not isinstance(raw_items, list) or not raw_items:
            raise ValidationError("items必须是非空列表")
        if len(raw_items) > 1000:
            raise ValidationError("单批最多1000条")

        existing = self.repository.idempotent_get("batch:" + run_key)
        if existing is not None:
            result = dict(existing["response"])
            result["replayed"] = True
            return self._resume_batch(run_key, actor, result)

        enriched: List[Dict[str, Any]] = []
        for raw in raw_items:
            if not isinstance(raw, dict):
                raise ValidationError("items每项必须是对象")
            record_id = raw.get("record_id")
            if isinstance(record_id, bool) or not isinstance(record_id, int):
                raise ValidationError("record_id必须是整数")
            record = self.repository.get(record_id)
            self._require_record_org(actor, record)
            spec = self.rules.validate_supplement(raw)
            policy = self.repository.policy_at(int(spec["day"]))
            if policy is None:
                raise ValidationError("当前没有可用的政策版本，请先发布政策")
            allowed_days = self.rules.allowed_days(policy, spec.get("allowed_days"))
            enriched.append({
                "record_id": record_id,
                "officer_id": spec["officer_id"],
                "day": int(spec["day"]),
                "request": spec["request"],
                "request_day": int(spec["request_day"]),
                "due_day": int(spec["request_day"]) + allowed_days,
                "policy_version": policy["version"],
                "policy_basis": json.dumps(policy.get("basis", {}), ensure_ascii=False, sort_keys=True),
            })
        self.repository.create_batch_run(run_key, actor.organization or "", enriched, actor.user_id)
        self.repository.idempotent_put("batch:" + run_key, "batch", 200, {"run_key": run_key})
        return self._resume_batch(run_key, actor, {"run_key": run_key, "replayed": False})

    def _resume_batch(self, run_key: str, actor: Actor, result: Dict[str, Any]) -> Dict[str, Any]:
        """断点续跑：循环推进直到 completed；写入失败抛错，重试从最后完整批次继续。"""
        run = self.repository.get_batch_run(run_key)
        if run["organization"] and actor.organization != run["organization"]:
            raise PermissionDenied("无权操作其他机构的批次")
        while True:
            run = self.repository.get_batch_run(run_key)
            if run["status"] == "completed" and int(run["last_completed_index"]) >= len(run["items"]) - 1:
                result["run"] = run
                result["done"] = True
                return result
            try:
                chunk = self.repository.process_batch_chunk(run_key, actor.user_id)
            except sqlite3.OperationalError as exc:
                # 写入失败：批次已停在断点，返回当前状态，允许重试
                failed_run = self.repository.get_batch_run(run_key)
                result["run"] = failed_run
                result["done"] = False
                result["write_failed"] = str(exc)
                result["resume_from_index"] = int(failed_run["last_completed_index"]) + 1
                return result
            result.setdefault("chunks", []).append({
                "started_at_index": chunk["started_at_index"],
                "ended_at_index": chunk["last_completed_index"],
                "issued": [{"id": item["id"], "reference": item["reference"],
                            "status": item["status"], "replayed": item.get("replayed", False)}
                           for item in chunk["issued"]],
            })
            if chunk["status"] == "completed":
                result["run"] = {k: v for k, v in chunk.items() if k != "issued"}
                result["done"] = True
                return result
