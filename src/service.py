"""业务用例编排、权限检查与审计。"""
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, PermissionDenied, ValidationError, text
from .repository import (
    TASK_QUEUED,
    Repository,
)
from .rules import DomainRules


class Service:
    def __init__(self, repository: Repository, rules: DomainRules, audit: AuditRecorder = None) -> None:
        self.repository = repository
        self.rules = rules
        self.audit = audit or AuditRecorder(repository)

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _ensure_known_role(self, actor: Actor) -> None:
        if not self.rules.known_role(actor.role):
            raise PermissionDenied("角色无权访问该服务")

    def _check_org(self, actor: Actor, record: Dict[str, Any]) -> None:
        if actor.role == "admin":
            return
        owner_org = (record.get("organization") or "").strip()
        actor_org = (actor.organization or "").strip()
        if owner_org and actor_org and owner_org != actor_org:
            raise PermissionDenied("无权处理其他机构的案件")

    def _record_checked(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        record = self.repository.get(record_id)
        self._check_org(actor, record)
        return record

    def create(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create(actor.role):
            raise PermissionDenied("角色无权创建记录")
        reference = text({"reference": reference}, "reference")
        prepared = self.rules.prepare_create(payload or {})
        self.rules.check_create_conflicts(prepared, self.repository.list_records(limit=500))
        return self.repository.create(
            reference, self.rules.INITIAL_STATE, prepared, actor.user_id,
            organization=(actor.organization or "").strip(),
        )

    def list_records(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        items = self.repository.list_records(state=state, limit=limit)
        if actor.role == "admin" or not (actor.organization or "").strip():
            return items
        return [item for item in items if not (item.get("organization") or "").strip() or item["organization"] == actor.organization]

    def get_record(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self._record_checked(actor, record_id)

    def act(self, actor: Actor, record_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        if not self.rules.role_can_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        record = self._record_checked(actor, record_id)
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
        self._record_checked(actor, record_id)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()

    # ---- 政策版本 ----
    def publish_policy(self, actor: Actor, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if actor.role not in ("admin", "supervisor"):
            raise PermissionDenied("角色无权发布政策版本")
        policy = self.rules.validate_policy(payload or {})
        return self.repository.publish_policy(policy, actor.user_id)

    def list_policies(self, actor: Actor) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_policies()

    # ---- 承办容量 ----
    def set_capacity(self, actor: Actor, payload: Dict[str, Any], idempotency_key: str = "") -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if actor.role not in ("admin", "supervisor"):
            raise PermissionDenied("角色无权设置承办容量")
        cap = self.rules.validate_capacity(payload or {})
        key = idempotency_key.strip() or "capacity:%s:%s:%s" % (cap["handler_id"], cap["day"], cap["capacity"])
        cached = self.repository.load_idempotent(key)
        if cached is not None:
            result = dict(cached["result"])
            result["idempotent_replayed"] = True
            return result
        result = self.repository.upsert_capacity(cap["handler_id"], cap["day"], cap["capacity"], actor.user_id)
        self.repository.save_idempotent(key, "capacity", result)
        return result

    def get_capacity(self, actor: Actor, handler_id: str, day: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.get_capacity(text({"handler_id": handler_id}, "handler_id"), int(day))

    # ---- 补件任务 ----
    def _build_task(self, actor: Actor, item: Dict[str, Any], idempotency_key: str) -> Dict[str, Any]:
        data = self.rules.validate_evidence_task(item)
        record = self._record_checked(actor, data["record_id"])
        policy = self.repository.policy_for_day(data["day"])
        return {
            "record_id": data["record_id"],
            "handler_id": data["handler_id"],
            "day": data["day"],
            "due_day": data["day"] + int(policy["evidence_days"]),
            "evidence_request": data["evidence_request"],
            "policy_version": policy["version"],
            "idempotency_key": idempotency_key,
            "reference": record["reference"],
        }

    def reserve_evidence(self, actor: Actor, payload: Dict[str, Any], idempotency_key: str = "") -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_action(actor.role, "request_evidence"):
            raise PermissionDenied("角色无权发补件")
        item = payload or {}
        record_hint = item.get("record_id")
        key = idempotency_key.strip() or (
            "rfe:%s:%s:%s" % (record_hint, item.get("handler_id", ""), item.get("day", "")) if record_hint else ""
        )
        key = text({"idempotency_key": key}, "idempotency_key")
        cached = self.repository.load_idempotent(key)
        if cached is not None:
            result = dict(cached["result"])
            result["idempotent_replayed"] = True
            return result
        task = self._build_task(actor, item, key)
        result = self.repository.reserve_evidence_task(task, actor.user_id)
        result["queued"] = result["status"] == TASK_QUEUED
        self.repository.save_idempotent(key, "rfe", {k: v for k, v in result.items() if k != "idempotent_replayed"})
        return result

    def confirm_task(self, actor: Actor, task_id: int, expected_version: int, idempotency_key: str = "") -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_action(actor.role, "request_evidence"):
            raise PermissionDenied("角色无权确认补件")
        task = self.repository.get_task(int(task_id))
        self._record_checked(actor, task["record_id"])
        key = (idempotency_key.strip() or "confirm-task:%s:%s" % (task_id, expected_version))
        cached = self.repository.load_idempotent(key)
        if cached is not None:
            result = dict(cached["result"])
            result["idempotent_replayed"] = True
            return result
        result = self.repository.confirm_task(int(task_id), int(expected_version), actor.user_id)
        self.repository.save_idempotent(key, "task_confirm", result)
        return result

    def release_task(self, actor: Actor, task_id: int, expected_version: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        task = self.repository.get_task(int(task_id))
        self._record_checked(actor, task["record_id"])
        return self.repository.release_task(int(task_id), int(expected_version), actor.user_id)

    def respond_task(self, actor: Actor, task_id: int, expected_version: int, payload: Dict[str, Any], idempotency_key: str = "") -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_action(actor.role, "respond"):
            raise PermissionDenied("角色无权回应补件")
        data = self.rules.validate_task_response(payload or {})
        task = self.repository.get_task(int(task_id))
        self._record_checked(actor, task["record_id"])
        key = (idempotency_key.strip() or "respond-task:%s:%s" % (task_id, expected_version))
        cached = self.repository.load_idempotent(key)
        if cached is not None:
            result = dict(cached["result"])
            result["idempotent_replayed"] = True
            return result
        result = self.repository.respond_task(
            int(task_id), int(expected_version), data["response_day"], data["documents"], actor.user_id)
        self.repository.save_idempotent(key, "task_respond", result)
        return result

    def list_tasks(self, actor: Actor, record_id: Optional[int] = None, handler_id: Optional[str] = None, status: Optional[str] = None) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if record_id is not None:
            self._record_checked(actor, int(record_id))
        tasks = self.repository.list_tasks(record_id=int(record_id) if record_id is not None else None,
                                           handler_id=handler_id, status=status)
        if actor.role == "admin" or not (actor.organization or "").strip():
            return tasks
        visible = []
        for task in tasks:
            record = self.repository.get(task["record_id"])
            if not (record.get("organization") or "").strip() or record["organization"] == actor.organization:
                visible.append(task)
        return visible

    # ---- 批量发补件：失败后从最后完整批次继续 ----
    def issue_rfe_batch(self, actor: Actor, payload: Dict[str, Any], idempotency_key: str = "") -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_action(actor.role, "request_evidence"):
            raise PermissionDenied("角色无权发补件")
        items = (payload or {}).get("items")
        if not isinstance(items, list) or not items:
            raise ValidationError("items必须是非空数组")
        batch_key = (idempotency_key.strip() or text({"batch_key": (payload or {}).get("batch_key", "")}, "batch_key"))

        existing = self.repository.get_batch(batch_key)
        if existing is not None and existing["total"] != len(items):
            raise Conflict("批次键%s已用于不同的批次" % batch_key)
        batch = existing or self.repository.create_batch(batch_key, len(items), actor.user_id)

        if batch is not None and existing is not None and existing["status"] == "completed":
            cached = self.repository.load_idempotent(batch_key)
            if cached is not None:
                result = dict(cached["result"])
                result["idempotent_replayed"] = True
                return result

        start_index = int(batch["last_completed_index"]) + 1
        issued: List[Dict[str, Any]] = []

        interrupted_at: Optional[int] = None
        for index in range(start_index, len(items)):
            raw = items[index]
            if not isinstance(raw, dict):
                raise ValidationError("items[%s]必须是对象" % index)
            if raw.get("_fail"):
                # 注入的写入失败：检查点停在上一个完整项，重跑同一批次从这里继续
                interrupted_at = index
                break
            item_key = "%s:item:%s" % (batch_key, index)
            task = self._build_task(actor, raw, item_key)
            result = self.repository.reserve_evidence_task(task, actor.user_id)
            issued.append({"index": index, "task_id": result["id"], "status": result["status"],
                           "record_id": result["record_id"], "handler_id": result["handler_id"]})
            self.repository.advance_batch(batch_key, index, "running")

        if interrupted_at is not None:
            return {
                "batch_key": batch_key, "status": "interrupted", "total": len(items),
                "last_completed_index": interrupted_at - 1, "resume_from_index": interrupted_at,
                "issued": issued,
            }

        self.repository.advance_batch(batch_key, len(items) - 1, "completed")
        response = {
            "batch_key": batch_key, "status": "completed", "total": len(items),
            "last_completed_index": len(items) - 1, "resume_from_index": len(items),
            "issued": issued,
        }
        self.repository.save_idempotent(batch_key, "rfe_batch", response)
        return response
