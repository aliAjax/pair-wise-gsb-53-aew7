"""移民案件期限与材料管理领域规则与状态转换。"""
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .domain import Actor, Conflict, ValidationError, boolean, choice, integer, text, text_list


INITIAL_STATE = "draft"
CREATE_ROLES = {'intake_officer'}
ACTION_ROLES = {'submit': {'legal_rep', 'case_officer'}, 'request_evidence': {'case_officer'}, 'respond': {'legal_rep'}, 'decide': {'case_officer', 'supervisor'}, 'appeal': {'legal_rep'}, 'close': {'supervisor'}}
TRANSITIONS = {'submit': {'draft': 'submitted'}, 'request_evidence': {'submitted': 'evidence_requested'}, 'respond': {'evidence_requested': 'response_received'}, 'decide': {'submitted': 'decided', 'response_received': 'decided'}, 'appeal': {'decided': 'appealed'}, 'close': {'decided': 'closed', 'appealed': 'closed'}}

# 补件任务/容量/政策相关角色：clerk 为书记员
CLERK_ROLE = 'clerk'
SUPPLEMENT_ISSUE_ROLES = {'case_officer', CLERK_ROLE}
QUOTA_CONFIRM_ROLES = {'case_officer', CLERK_ROLE}
SUPPLEMENT_RESPOND_ROLES = {'legal_rep'}
POLICY_ADMIN_ROLES = {'supervisor', 'admin'}
CAPACITY_ADMIN_ROLES = {'supervisor', 'admin'}

DEFAULT_ALLOWED_DAYS = 10


class DomainRules:
    INITIAL_STATE = INITIAL_STATE

    def known_role(self, role: str) -> bool:
        all_roles = set(CREATE_ROLES) | {CLERK_ROLE}
        for roles in ACTION_ROLES.values():
            all_roles.update(roles)
        return role == "admin" or role in all_roles

    def role_can_create(self, role: str) -> bool:
        return role == "admin" or role in CREATE_ROLES

    def role_can_action(self, role: str, action: str) -> bool:
        return role == "admin" or role in ACTION_ROLES.get(action, set())

    def validate_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload)
        text(p, "applicant_id")
        choice(p, "case_type", ["asylum", "family", "work"])
        integer(p, "received_day", 0)
        integer(p, "deadline_days", 1)
        integer(p, "response_day", 0)
        boolean(p, "representation_active")
        text_list(p, "required_documents", 1)
        return p

    def prepare_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = self.validate_create(payload)
        p["deadline_day"] = int(p["received_day"]) + int(p["deadline_days"])
        p["days_remaining"] = int(p["deadline_day"]) - int(p["response_day"])
        p["overdue"] = p["days_remaining"] < 0
        p["submitted_documents"] = []
        p["missing_documents"] = list(p["required_documents"])
        return p

    def check_create_conflicts(self, payload: Dict[str, Any], existing: Iterable[Dict[str, Any]]) -> None:
        for item in existing:
            if item["state"] not in {"closed", "decided"} and item["payload"].get("applicant_id") == payload.get("applicant_id") and item["payload"].get("case_type") == payload.get("case_type"):
                raise Conflict("同一申请人同类型案件仍在处理中")

    def require_transition(self, record: Dict[str, Any], action: str) -> str:
        allowed = TRANSITIONS.get(action, {}).get(record["state"])
        if allowed is None:
            raise Conflict("当前状态不允许执行%s" % action)
        return allowed

    # ---- 政策版本 ----

    def validate_policy(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload or {})
        text(p, "version")
        integer(p, "effective_day", 0)
        basis = p.get("basis", {})
        if basis is None:
            basis = {}
        if not isinstance(basis, dict):
            raise ValidationError("basis必须是对象")
        allowed = basis.get("allowed_days", DEFAULT_ALLOWED_DAYS)
        if isinstance(allowed, bool) or not isinstance(allowed, int) or allowed < 1:
            raise ValidationError("basis.allowed_days必须是正整数")
        p["basis"] = dict(basis)
        return p

    def resolve_policy(self, policies: List[Dict[str, Any]], day: int) -> Optional[Dict[str, Any]]:
        """返回 day 时点生效的政策：effective_day <= day 中最新的；都未生效则取最早的。"""
        if not policies:
            return None
        applicable = [policy for policy in policies if int(policy["effective_day"]) <= int(day)]
        if applicable:
            return max(applicable, key=lambda policy: int(policy["effective_day"]))
        return min(policies, key=lambda policy: int(policy["effective_day"]))

    def allowed_days(self, policy: Optional[Dict[str, Any]], override: Any = None) -> int:
        if isinstance(override, bool) or not isinstance(override, int):
            if policy is not None:
                override = int(policy.get("basis", {}).get("allowed_days", DEFAULT_ALLOWED_DAYS))
            else:
                override = DEFAULT_ALLOWED_DAYS
        if override < 1:
            raise ValidationError("allowed_days必须是正整数")
        return int(override)

    # ---- 承办容量 ----

    def validate_capacity(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload or {})
        text(p, "officer_id")
        integer(p, "day", 0)
        integer(p, "capacity", 0)
        return p

    # ---- 补件任务 ----

    def validate_supplement(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload or {})
        text(p, "officer_id")
        day = integer(p, "day", 0)
        request_day = p.get("request_day", day)
        if isinstance(request_day, bool) or not isinstance(request_day, int) or request_day < 0:
            raise ValidationError("request_day必须是非负整数")
        p["request_day"] = int(request_day)
        text(p, "request")
        if "allowed_days" in p and p["allowed_days"] is not None:
            integer(p, "allowed_days", 1)
        return p

    def validate_response(self, payload: Dict[str, Any], due_day: int) -> Dict[str, Any]:
        p = dict(payload or {})
        response_day = integer(p, "response_day", 0)
        if response_day > int(due_day):
            raise ValidationError("补件回应超过期限")
        p["documents"] = text_list(p, "documents", 1)
        return p

    def apply_action(self, record: Dict[str, Any], action: str, data: Dict[str, Any]) -> Tuple[str, Dict[str, Any], str]:
        new_state = self.require_transition(record, action)
        data = dict(data or {})
        p = dict(record["payload"])
        changes: Dict[str, Any] = {}
        summary = ""
        if action == "submit":
            docs = text_list(data, "documents", 1)
            missing = [doc for doc in p["required_documents"] if doc not in docs]
            if missing and not boolean(data, "supervisor_waiver"):
                raise ValidationError("缺少材料：" + ", ".join(missing))
            if p["overdue"] and not boolean(data, "supervisor_waiver"):
                raise ValidationError("案件已超过提交期限")
            changes["submitted_documents"] = docs
            changes["missing_documents"] = missing
            changes["waiver_used"] = boolean(data, "supervisor_waiver")
            summary = "申请材料已提交"
        elif action == "request_evidence":
            request_day = integer(data, "evidence_request_day", p["response_day"])
            allowed_days = integer(data, "allowed_days", 1)
            changes["evidence_request_day"] = request_day
            changes["evidence_due_day"] = request_day + allowed_days
            changes["evidence_request"] = text(data, "evidence_request")
            summary = "补件要求已发出"
        elif action == "respond":
            docs = text_list(data, "documents", 1)
            if int(data.get("response_day", p["response_day"])) > int(p["evidence_due_day"]):
                raise ValidationError("补件回应超过期限")
            changes["response_day"] = int(data["response_day"])
            changes["evidence_documents"] = docs
            summary = "补件已回应"
        elif action == "decide":
            changes["decision"] = choice(data, "decision", ["granted", "denied", "withdrawn"])
            changes["decision_reason"] = text(data, "decision_reason")
            summary = "案件已作出决定"
        elif action == "appeal":
            appeal_day = integer(data, "appeal_day", 0)
            if appeal_day > int(p["deadline_day"]) + 30:
                raise ValidationError("上诉窗口已关闭")
            changes["appeal_day"] = appeal_day
            changes["appeal_reason"] = text(data, "appeal_reason")
            summary = "上诉已登记"
        elif action == "close":
            changes["closure_note"] = text(data, "closure_note")
            summary = "案件归档"
        p.update(changes)
        return new_state, p, summary or ("已执行%s" % action)
