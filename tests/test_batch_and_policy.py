"""批量补发断点续跑、幂等与旧数据政策回填测试。"""
import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, ValidationError
from src.repository import TASK_RESERVED, TASK_VOIDED
from src.repository import Repository


CREATE_DATA = {'applicant_id': 'A-900', 'case_type': 'family', 'received_day': 100,
               'deadline_days': 30, 'response_day': 110, 'representation_active': True,
               'required_documents': ['passport', 'sponsor_letter']}


class BatchResumeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))
        self.supervisor = Actor("sup", "supervisor", "org-a")
        self.clerk = Actor("clerk", "clerk", "org-a")
        self.service.publish_policy(Actor("sup", "supervisor", "org-a"),
                                    {"version": "p1", "effective_day": 0, "basis": {"allowed_days": 10}})
        self.service.set_capacity(self.supervisor, {"officer_id": "off-li", "day": 115, "capacity": 10})
        self.record_ids = []
        for i in range(1, 7):
            data = dict(CREATE_DATA)
            data["applicant_id"] = "A-%d" % i
            record = self.service.create(Actor("c", "intake_officer", "org-a"), "IMM-%d" % i, data)
            self.record_ids.append(record["id"])

    def tearDown(self):
        self.temp.cleanup()

    def _items(self):
        return [{"record_id": rid, "officer_id": "off-li", "day": 115,
                 "request": "补件%s" % rid} for rid in self.record_ids]

    def test_batch_resumes_from_last_complete_batch_after_write_failure(self):
        repo: Repository = self.service.repository
        # 让第 4 个条目（索引3）写入失败：整批回滚，断点停在索引 -1
        repo.set_batch_fail_point("BATCH-1", 3)
        result = self.service.issue_supplement_batch(self.clerk, {"run_key": "BATCH-1", "items": self._items()})
        self.assertFalse(result["done"])
        self.assertTrue(result.get("write_failed"))
        run = self.service.repository.get_batch_run("BATCH-1")
        self.assertEqual(run["last_completed_index"], -1)
        self.assertEqual(run["status"], "failed:写入失败，索引3未完成")
        # 失败期间没有任何任务落库
        self.assertEqual(self.service.list_supplements(self.clerk), [])
        # 清除故障后重试：从最后完整批次（索引0）继续，一次跑完
        self.service.repository._batch_fail_points.pop("BATCH-1", None)
        retry = self.service.issue_supplement_batch(self.clerk, {"run_key": "BATCH-1", "items": self._items()})
        self.assertTrue(retry["done"])
        self.assertEqual(retry["run"]["last_completed_index"], 5)
        tasks = self.service.list_supplements(self.clerk)
        self.assertEqual(len(tasks), 6)
        self.assertTrue(all(task["status"] == TASK_RESERVED for task in tasks))

    def test_failure_after_two_items_resumes_at_index_two(self):
        repo: Repository = self.service.repository
        # 先成功跑一批 3 条
        items3 = self._items()[:3]
        ok = self.service.issue_supplement_batch(self.clerk, {"run_key": "BATCH-OK", "items": items3})
        self.assertTrue(ok["done"])
        self.assertEqual(ok["run"]["last_completed_index"], 2)
        # 第二批 3 条，在索引 2（批内）失败：索引0、1的写入随事务回滚
        items_next = self._items()[3:]
        repo.set_batch_fail_point("BATCH-NEXT", 2)
        failed = self.service.issue_supplement_batch(self.clerk, {"run_key": "BATCH-NEXT", "items": items_next})
        self.assertFalse(failed["done"])
        run = self.service.repository.get_batch_run("BATCH-NEXT")
        self.assertEqual(run["last_completed_index"], -1)
        repo._batch_fail_points.pop("BATCH-NEXT", None)
        retry = self.service.issue_supplement_batch(self.clerk, {"run_key": "BATCH-NEXT", "items": items_next})
        self.assertTrue(retry["done"])
        # 总共 6 条任务（重试没有重复占名额）
        self.assertEqual(len(self.service.list_supplements(self.clerk)), 6)

    def test_duplicate_batch_request_replays_same_run(self):
        first = self.service.issue_supplement_batch(self.clerk, {"run_key": "DUP", "items": self._items()[:2]})
        self.assertTrue(first["done"])
        second = self.service.issue_supplement_batch(self.clerk, {"run_key": "DUP", "items": self._items()[:2]})
        self.assertTrue(second["replayed"])
        self.assertTrue(second["done"])
        self.assertEqual(len(self.service.list_supplements(self.clerk)), 2)


class BackfillTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp.name) / "test.db")
        self.service = build_service(self.db_path)

    def tearDown(self):
        self.temp.cleanup()

    def _old_case(self, ref, received_day, actor_id="creator"):
        # 直接用仓储写入，模拟没有政策版本的旧数据
        data = dict(CREATE_DATA)
        data["applicant_id"] = ref
        data["received_day"] = received_day
        prepared = self.service.rules.prepare_create(data)
        return self.service.repository.create(ref, "draft", prepared, actor_id, organization="org-old", policy_version=None)

    def test_legacy_records_backfilled_by_creation_time_policy(self):
        old1 = self._old_case("OLD-1", 50)
        old2 = self._old_case("OLD-2", 150)
        sup = Actor("sup", "supervisor", "org-old")
        # 发布两版政策：p1 自第0天，p2 自第100天
        self.service.publish_policy(sup, {"version": "p1", "effective_day": 0, "basis": {"allowed_days": 10}})
        self.service.publish_policy(sup, {"version": "p2", "effective_day": 100, "basis": {"allowed_days": 20}})
        # 旧数据回填：按案件创建时（received_day）生效的政策一次性解析
        count = self.service.backfill_policy_versions(sup)
        self.assertEqual(count, 2)
        r1 = self.service.repository.get(old1["id"])
        r2 = self.service.repository.get(old2["id"])
        self.assertEqual(r1["policy_version"], "p1")  # 创建于第50天 → p1
        self.assertEqual(r2["policy_version"], "p2")  # 创建于第150天 → p2
        # 回填幂等：再跑一次不会重复处理
        self.assertEqual(self.service.backfill_policy_versions(sup), 0)

    def test_responded_legacy_tasks_keep_original_judgment_after_policy_publish(self):
        sup = Actor("sup", "supervisor", "org-old")
        clerk = Actor("clerk", "clerk", "org-old")
        rep = Actor("rep", "legal_rep", "org-old")
        # 先发一版政策、建案、走完整补件流程
        self.service.publish_policy(sup, {"version": "p1", "effective_day": 0, "basis": {"allowed_days": 10}})
        record = self.service.create(Actor("c", "intake_officer", "org-old"), "CASE-1", CREATE_DATA)
        self.service.set_capacity(sup, {"officer_id": "off-li", "day": 115, "capacity": 1})
        task = self.service.issue_supplement(clerk, record["id"],
                                             {"officer_id": "off-li", "day": 115, "request": "补收入"})
        self.service.confirm_quota(clerk, task["id"])
        self.service.respond_supplement(rep, task["id"], {"response_day": 120, "documents": ["income"]})
        # 新政策发布：已回应历史任务保留原判，不失效
        self.service.publish_policy(sup, {"version": "p2", "effective_day": 116, "basis": {"allowed_days": 99}})
        kept = self.service.repository.get_supplement(task["id"])
        self.assertEqual(kept["status"], "responded")
        self.assertEqual(kept["policy_version"], "p1")
        self.assertEqual(kept["due_day"], 125)
        self.assertEqual(kept["response_docs"], ["income"])

    def test_no_policy_does_not_break_create_and_issue_requires_policy(self):
        record = self.service.create(Actor("c", "intake_officer", "org-x"), "CASE-X", CREATE_DATA)
        self.assertIsNone(record["policy_version"])
        self.service.set_capacity(Actor("sup", "supervisor", "org-x"),
                                  {"officer_id": "off-li", "day": 115, "capacity": 1})
        with self.assertRaises(ValidationError):
            self.service.issue_supplement(Actor("clerk", "clerk", "org-x"), record["id"],
                                          {"officer_id": "off-li", "day": 115, "request": "补"})
