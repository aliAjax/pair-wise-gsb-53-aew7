"""补件任务—承办容量—政策版本联动测试：预留、排队、并发确认、机构隔离。"""
import tempfile
import threading
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, PermissionDenied, ValidationError
from src.repository import (
    SLOT_CONFIRMED, SLOT_RESERVED, TASK_CONFIRMED, TASK_QUEUED, TASK_RESERVED,
    TASK_RESPONDED, TASK_VOIDED,
)


CREATE_DATA = {'applicant_id': 'A-900', 'case_type': 'family', 'received_day': 100,
               'deadline_days': 30, 'response_day': 110, 'representation_active': True,
               'required_documents': ['passport', 'sponsor_letter']}


class SupplementLinkageTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))
        self.supervisor = Actor("sup", "supervisor", org := "org-a")
        self.officer = Actor("off", "case_officer", org)
        self.clerk1 = Actor("clerk-1", "clerk", org)
        self.clerk2 = Actor("clerk-2", "clerk", org)
        self.rep = Actor("rep", "legal_rep", org)

    def tearDown(self):
        self.temp.cleanup()

    def _case(self, index, org="org-a"):
        actor = Actor("creator-%d" % index, "intake_officer", org)
        data = dict(CREATE_DATA)
        data["applicant_id"] = "A-%d" % index
        return self.service.create(actor, "IMM-%d" % index, data)

    def _policy(self, version="p1", effective_day=0, allowed_days=10):
        return self.service.publish_policy(
            self.supervisor, {"version": version, "effective_day": effective_day,
                              "basis": {"allowed_days": allowed_days}})

    def test_issue_reserves_slot_and_over_capacity_queues(self):
        self._policy()
        case1 = self._case(1)
        case2 = self._case(2)
        case3 = self._case(3)
        self.service.set_capacity(self.supervisor, {"officer_id": "officer-li", "day": 115, "capacity": 2})
        t1 = self.service.issue_supplement(self.clerk1, case1["id"],
                                           {"officer_id": "officer-li", "day": 115, "request": "补收入证明"})
        t2 = self.service.issue_supplement(self.clerk2, case2["id"],
                                           {"officer_id": "officer-li", "day": 115, "request": "补居住证明"})
        t3 = self.service.issue_supplement(self.clerk1, case3["id"],
                                           {"officer_id": "officer-li", "day": 115, "request": "补体检报告"})
        self.assertEqual(t1["status"], TASK_RESERVED)
        self.assertEqual(t2["status"], TASK_RESERVED)
        self.assertEqual(t3["status"], TASK_QUEUED)
        view = self.service.capacity_view(self.supervisor, "officer-li", 115)
        self.assertEqual(view["used"], 2)
        self.assertEqual(view["available"], 0)
        self.assertEqual(view["queued"], 1)
        self.assertIsNotNone(t1["slot_id"])
        self.assertIsNone(t3["slot_id"])
        self.assertEqual(t1["policy_version"], "p1")

    def test_concurrent_issues_never_exceed_capacity(self):
        self._policy()
        cases = [self._case(i)["id"] for i in range(1, 9)]
        self.service.set_capacity(self.supervisor, {"officer_id": "officer-li", "day": 120, "capacity": 3})
        results, errors = [], []
        barrier = threading.Barrier(len(cases))

        def issue(cid, clerk):
            barrier.wait()
            try:
                task = self.service.issue_supplement(
                    clerk, cid, {"officer_id": "officer-li", "day": 120, "request": "材料%s" % cid})
                results.append(task)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=issue, args=(cid, self.clerk1 if i % 2 else self.clerk2))
                   for i, cid in enumerate(cases)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        reserved = [task for task in results if task["status"] == TASK_RESERVED]
        queued = [task for task in results if task["status"] == TASK_QUEUED]
        self.assertEqual(len(reserved), 3)
        self.assertEqual(len(queued), 5)
        slots = {task["slot_id"] for task in reserved}
        self.assertEqual(len(slots), 3)
        view = self.service.capacity_view(self.supervisor, "officer-li", 120)
        self.assertEqual(view["used"], 3)

    def test_concurrent_confirm_same_slot_only_one_passes(self):
        self._policy()
        case1 = self._case(1)
        self.service.set_capacity(self.supervisor, {"officer_id": "officer-li", "day": 115, "capacity": 1})
        task = self.service.issue_supplement(
            self.clerk1, case1["id"], {"officer_id": "officer-li", "day": 115, "request": "补材料"})
        outcomes = []
        barrier = threading.Barrier(2)

        def confirm(clerk):
            barrier.wait()
            try:
                outcomes.append(("ok", self.service.confirm_quota(clerk, task["id"])))
            except Conflict as exc:
                outcomes.append(("conflict", str(exc)))

        threads = [threading.Thread(target=confirm, args=(self.clerk1,)),
                   threading.Thread(target=confirm, args=(self.clerk2,))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        statuses = sorted(outcome[0] for outcome in outcomes)
        self.assertEqual(statuses, ["conflict", "ok"])
        refreshed = self.service.get_supplement(self.officer, task["id"])
        self.assertEqual(refreshed["status"], TASK_CONFIRMED)

    def test_queue_promoted_on_release_and_capacity_growth(self):
        self._policy()
        cases = [self._case(i) for i in range(1, 4)]
        self.service.set_capacity(self.supervisor, {"officer_id": "officer-li", "day": 115, "capacity": 1})
        t1 = self.service.issue_supplement(self.clerk1, cases[0]["id"],
                                           {"officer_id": "officer-li", "day": 115, "request": "a"})
        t2 = self.service.issue_supplement(self.clerk2, cases[1]["id"],
                                           {"officer_id": "officer-li", "day": 115, "request": "b"})
        self.assertEqual(t2["status"], TASK_QUEUED)
        released = self.service.release_quota(self.officer, t1["id"])
        self.assertEqual(released["status"], TASK_QUEUED)
        promoted = self.service.get_supplement(self.officer, t2["id"])
        self.assertEqual(promoted["status"], TASK_RESERVED)
        # 扩容后 t1 也获得预留
        self.service.set_capacity(self.supervisor, {"officer_id": "officer-li", "day": 115, "capacity": 2})
        t1_refresh = self.service.get_supplement(self.officer, t1["id"])
        self.assertEqual(t1_refresh["status"], TASK_RESERVED)

    def test_duplicate_idem_key_does_not_consume_another_slot(self):
        self._policy()
        cases = [self._case(i) for i in range(1, 3)]
        self.service.set_capacity(self.supervisor, {"officer_id": "officer-li", "day": 115, "capacity": 2})
        payload = {"officer_id": "officer-li", "day": 115, "request": "同一份补件",
                   "idem_key": "idem-XYZ"}
        first = self.service.issue_supplement(self.clerk1, cases[0]["id"], payload)
        replay = self.service.issue_supplement(self.clerk2, cases[1]["id"], payload)
        self.assertEqual(first["id"], replay["id"])
        self.assertTrue(replay["replayed"])
        view = self.service.capacity_view(self.supervisor, "officer-li", 115)
        self.assertEqual(view["used"], 1)

    def test_cross_org_access_denied(self):
        self._policy()
        case_a = self._case(1, org="org-a")
        case_b = self._case(2, org="org-b")
        clerk_b = Actor("clerk-b", "clerk", "org-b")
        with self.assertRaises(PermissionDenied):
            self.service.issue_supplement(clerk_b, case_a["id"],
                                          {"officer_id": "off-b", "day": 115, "request": "越权"})
        with self.assertRaises(PermissionDenied):
            self.service.get_record(clerk_b, case_a["id"])
        # 本机构可见自己案件
        self.assertEqual(self.service.get_record(clerk_b, case_b["id"])["organization"], "org-b")
        # 列表也按机构过滤
        self.assertEqual(len(self.service.list_records(clerk_b)), 1)

    def test_responded_task_retains_original_basis_after_policy_change(self):
        self._policy("p1", effective_day=0, allowed_days=10)
        case1 = self._case(1)
        case2 = self._case(2)
        self.service.set_capacity(self.supervisor, {"officer_id": "officer-li", "day": 115, "capacity": 2})
        t_open = self.service.issue_supplement(self.clerk1, case1["id"],
                                               {"officer_id": "officer-li", "day": 115, "request": "a"})
        t_done = self.service.issue_supplement(self.clerk2, case2["id"],
                                               {"officer_id": "officer-li", "day": 115, "request": "b"})
        self.service.confirm_quota(self.clerk2, t_done["id"])
        responded = self.service.respond_supplement(
            self.rep, t_done["id"], {"response_day": 120, "documents": ["income"]})
        self.assertEqual(responded["status"], TASK_RESPONDED)
        # 政策更新：补件期限从10天变30天
        self.service.publish_policy(self.supervisor, {"version": "p2", "effective_day": 116,
                                                      "basis": {"allowed_days": 30}})
        voided = self.service.get_supplement(self.officer, t_open["id"])
        self.assertEqual(voided["status"], TASK_VOIDED)
        self.assertEqual(voided["policy_version"], "p1")
        replacements = self.service.list_supplements(self.officer, record_id=case1["id"])
        new_task = next(task for task in replacements if task["supersedes_id"] == t_open["id"])
        self.assertEqual(new_task["status"], TASK_RESERVED)
        self.assertEqual(new_task["policy_version"], "p2")
        self.assertEqual(new_task["due_day"], 145)  # request_day 115 + 30
        # 已回应任务保留原判
        done_refresh = self.service.get_supplement(self.officer, t_done["id"])
        self.assertEqual(done_refresh["status"], TASK_RESPONDED)
        self.assertEqual(done_refresh["policy_version"], "p1")
        self.assertEqual(done_refresh["due_day"], 125)
        # 重复回应回放原结果
        replay = self.service.respond_supplement(
            self.rep, t_done["id"], {"response_day": 120, "documents": ["income"]})
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["response_docs"], ["income"])

    def test_policy_update_promotes_recreated_tasks_within_capacity(self):
        self._policy("p1", allowed_days=5)
        cases = [self._case(i) for i in range(1, 3)]
        self.service.set_capacity(self.supervisor, {"officer_id": "off-wang", "day": 130, "capacity": 2})
        t1 = self.service.issue_supplement(self.clerk1, cases[0]["id"],
                                           {"officer_id": "off-wang", "day": 130, "request": "a"})
        t2 = self.service.issue_supplement(self.clerk2, cases[1]["id"],
                                           {"officer_id": "off-wang", "day": 130, "request": "b"})
        self.service.publish_policy(self.supervisor, {"version": "p2", "effective_day": 200,
                                                      "basis": {"allowed_days": 20}})
        new_tasks = [task for task in self.service.list_supplements(self.officer)
                     if task["supersedes_id"] in (t1["id"], t2["id"])]
        self.assertEqual(len(new_tasks), 2)
        self.assertTrue(all(task["status"] == TASK_RESERVED for task in new_tasks))
        view = self.service.capacity_view(self.supervisor, "off-wang", 130)
        self.assertEqual(view["used"], 2)

    def test_respond_late_is_rejected(self):
        self._policy("p1", allowed_days=10)
        case1 = self._case(1)
        self.service.set_capacity(self.supervisor, {"officer_id": "officer-li", "day": 115, "capacity": 1})
        task = self.service.issue_supplement(self.clerk1, case1["id"],
                                             {"officer_id": "officer-li", "day": 115, "request": "a"})
        self.service.confirm_quota(self.clerk1, task["id"])
        with self.assertRaises(ValidationError):
            self.service.respond_supplement(
                self.rep, task["id"], {"response_day": 126, "documents": ["x"]})

    def test_duplicate_confirm_with_same_idem_key_is_replayed(self):
        self._policy()
        case1 = self._case(1)
        self.service.set_capacity(self.supervisor, {"officer_id": "officer-li", "day": 115, "capacity": 1})
        task = self.service.issue_supplement(
            self.clerk1, case1["id"], {"officer_id": "officer-li", "day": 115, "request": "补材料"})
        first = self.service.confirm_quota(self.clerk1, task["id"], idem_key="confirm-1")
        self.assertEqual(first["status"], TASK_CONFIRMED)
        # 同一请求（相同幂等键）重复提交：回放成功，不重复操作
        replay = self.service.confirm_quota(self.clerk1, task["id"], idem_key="confirm-1")
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["id"], task["id"])

    def test_confirm_queued_task_rejected(self):
        self._policy()
        cases = [self._case(i) for i in range(1, 3)]
        self.service.set_capacity(self.supervisor, {"officer_id": "officer-li", "day": 115, "capacity": 1})
        self.service.issue_supplement(self.clerk1, cases[0]["id"],
                                      {"officer_id": "officer-li", "day": 115, "request": "a"})
        queued = self.service.issue_supplement(self.clerk2, cases[1]["id"],
                                               {"officer_id": "officer-li", "day": 115, "request": "b"})
        with self.assertRaises(Conflict):
            self.service.confirm_quota(self.clerk2, queued["id"])
