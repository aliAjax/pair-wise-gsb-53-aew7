"""补件任务、承办容量、政策版本与批次续跑的集成测试。"""
import tempfile
import threading
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, PermissionDenied
from src.repository import (
    TASK_CONFIRMED,
    TASK_QUEUED,
    TASK_RESERVED,
    TASK_RESPONDED,
    TASK_VOIDED,
)


CREATE_DATA = {'applicant_id': 'A-900', 'case_type': 'family', 'received_day': 100, 'deadline_days': 30, 'response_day': 110, 'representation_active': True, 'required_documents': ['passport', 'sponsor_letter']}


def make_case(service, reference, org='org-a'):
    data = dict(CREATE_DATA)
    data['applicant_id'] = 'A-' + reference
    return service.create(Actor('creator', 'intake_officer', org), reference, data)


def submit_case(service, record, org='org-a'):
    return service.act(Actor('rep', 'legal_rep', org), record['id'], record['version'], 'submit',
                       {'documents': ['passport', 'sponsor_letter']})


class EvidenceCapacityTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / 'test.db'))

    def tearDown(self):
        self.temp.cleanup()

    def test_reserve_holds_one_slot_and_over_capacity_queues(self):
        r1 = submit_case(self.service, make_case(self.service, 'IMM-CAP-1'))
        r2 = submit_case(self.service, make_case(self.service, 'IMM-CAP-2'))
        self.service.set_capacity(Actor('sup', 'supervisor', 'org-a'),
                                  {'handler_id': 'H-1', 'day': 120, 'capacity': 1})
        t1 = self.service.reserve_evidence(Actor('off', 'case_officer', 'org-a'),
                                           {'record_id': r1['id'], 'handler_id': 'H-1', 'day': 120, 'evidence_request': '收入证明'},
                                           idempotency_key='rfe-1')
        self.assertEqual(t1['status'], TASK_RESERVED)
        cap = self.service.get_capacity(Actor('off', 'case_officer', 'org-a'), 'H-1', 120)
        self.assertEqual(cap['held'], 1)
        self.assertEqual(cap['available'], 0)

        # 两名书记员同时提交：第二个必须排队，不能把当日名额排超
        t2 = self.service.reserve_evidence(Actor('off2', 'case_officer', 'org-a'),
                                           {'record_id': r2['id'], 'handler_id': 'H-1', 'day': 120, 'evidence_request': '居住证明'},
                                           idempotency_key='rfe-2')
        self.assertEqual(t2['status'], TASK_QUEUED)
        cap = self.service.get_capacity(Actor('off', 'case_officer', 'org-a'), 'H-1', 120)
        self.assertEqual(cap['held'], 1)

        # 容量提升后排队任务自动补上
        self.service.set_capacity(Actor('sup', 'supervisor', 'org-a'),
                                  {'handler_id': 'H-1', 'day': 120, 'capacity': 2})
        t2 = self.service.repository.get_task(t2['id'])
        self.assertEqual(t2['status'], TASK_RESERVED)

    def test_concurrent_reservations_never_oversell(self):
        records = []
        for i in range(6):
            rec = submit_case(self.service, make_case(self.service, 'IMM-CC-%s' % i))
            records.append(rec)
        self.service.set_capacity(Actor('sup', 'supervisor', 'org-a'),
                                  {'handler_id': 'H-9', 'day': 130, 'capacity': 3})
        results = []
        errors = []
        barrier = threading.Barrier(len(records))

        def worker(index, rec):
            barrier.wait()
            try:
                task = self.service.reserve_evidence(
                    Actor('off%s' % index, 'case_officer', 'org-a'),
                    {'record_id': rec['id'], 'handler_id': 'H-9', 'day': 130, 'evidence_request': 'doc'},
                    idempotency_key='cc-%s' % index)
                results.append(task['status'])
            except Exception as exc:  # pragma: no cover - 仅在测试失败时出现
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i, rec)) for i, rec in enumerate(records)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertFalse(errors)
        self.assertEqual(sorted(results).count(TASK_RESERVED), 3)
        self.assertEqual(sorted(results).count(TASK_QUEUED), 3)
        cap = self.service.get_capacity(Actor('off', 'case_officer', 'org-a'), 'H-9', 130)
        self.assertEqual(cap['held'], 3)

    def test_duplicate_request_does_not_consume_slot(self):
        r1 = submit_case(self.service, make_case(self.service, 'IMM-IDEM-1'))
        self.service.set_capacity(Actor('sup', 'supervisor', 'org-a'),
                                  {'handler_id': 'H-2', 'day': 120, 'capacity': 1})
        payload = {'record_id': r1['id'], 'handler_id': 'H-2', 'day': 120, 'evidence_request': '收入证明'}
        first = self.service.reserve_evidence(Actor('off', 'case_officer', 'org-a'), payload, idempotency_key='same-key')
        second = self.service.reserve_evidence(Actor('off', 'case_officer', 'org-a'), payload, idempotency_key='same-key')
        self.assertEqual(first['id'], second['id'])
        self.assertTrue(second.get('idempotent_replayed'))
        cap = self.service.get_capacity(Actor('off', 'case_officer', 'org-a'), 'H-2', 120)
        self.assertEqual(cap['held'], 1)

    def test_two_clerks_confirm_same_slot_only_one_passes(self):
        r1 = submit_case(self.service, make_case(self.service, 'IMM-CONF-1'))
        self.service.set_capacity(Actor('sup', 'supervisor', 'org-a'),
                                  {'handler_id': 'H-3', 'day': 120, 'capacity': 1})
        task = self.service.reserve_evidence(Actor('off', 'case_officer', 'org-a'),
                                             {'record_id': r1['id'], 'handler_id': 'H-3', 'day': 120, 'evidence_request': '材料'},
                                             idempotency_key='conf-1')
        outcome = []

        def confirm(officer):
            try:
                result = self.service.confirm_task(Actor(officer, 'case_officer', 'org-a'),
                                                   task['id'], task['version'], idempotency_key='confirm-%s' % officer)
                outcome.append(('ok', result['status']))
            except Conflict:
                outcome.append(('conflict', None))

        t1 = threading.Thread(target=confirm, args=('off1',))
        t2 = threading.Thread(target=confirm, args=('off2',))
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        self.assertEqual(sorted(outcome), [('conflict', None), ('ok', TASK_CONFIRMED)])

    def test_cross_org_case_is_denied(self):
        r1 = submit_case(self.service, make_case(self.service, 'IMM-ORG-1', org='org-a'), org='org-a')
        with self.assertRaises(PermissionDenied):
            self.service.get_record(Actor('spy', 'case_officer', 'org-b'), r1['id'])
        with self.assertRaises(PermissionDenied):
            self.service.reserve_evidence(Actor('spy', 'case_officer', 'org-b'),
                                          {'record_id': r1['id'], 'handler_id': 'H-1', 'day': 120, 'evidence_request': 'x'})
        # 本机构仍可访问
        self.assertEqual(self.service.get_record(Actor('own', 'case_officer', 'org-a'), r1['id'])['id'], r1['id'])


class PolicyLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / 'test.db'))

    def tearDown(self):
        self.temp.cleanup()

    def test_policy_update_voids_open_tasks_and_keeps_responded_basis(self):
        open_rec = submit_case(self.service, make_case(self.service, 'IMM-POL-1'))
        answered_rec = submit_case(self.service, make_case(self.service, 'IMM-POL-2'))
        self.service.set_capacity(Actor('sup', 'supervisor', 'org-a'),
                                  {'handler_id': 'H-1', 'day': 120, 'capacity': 2})
        open_task = self.service.reserve_evidence(Actor('off', 'case_officer', 'org-a'),
                                                  {'record_id': open_rec['id'], 'handler_id': 'H-1', 'day': 120, 'evidence_request': '新材料A'},
                                                  idempotency_key='pt-1')
        answered_task = self.service.reserve_evidence(Actor('off', 'case_officer', 'org-a'),
                                                      {'record_id': answered_rec['id'], 'handler_id': 'H-1', 'day': 120, 'evidence_request': '新材料B'},
                                                      idempotency_key='pt-2')
        answered_task = self.service.confirm_task(Actor('off', 'case_officer', 'org-a'),
                                                  answered_task['id'], answered_task['version'], idempotency_key='pt-2-c')
        answered_task = self.service.respond_task(Actor('rep', 'legal_rep', 'org-a'),
                                                  answered_task['id'], answered_task['version'],
                                                  {'response_day': 125, 'documents': ['income_proof']},
                                                  idempotency_key='pt-2-r')
        self.assertEqual(answered_task['status'], TASK_RESPONDED)
        self.assertEqual(answered_task['policy_version'], 'baseline')

        result = self.service.publish_policy(Actor('sup', 'supervisor', 'org-a'),
                                             {'version': '2026-reform', 'effective_day': 120, 'evidence_days': 5})
        self.assertEqual(result['voided'], 1)
        self.assertEqual(result['reissued'], 1)

        old_open = self.service.repository.get_task(open_task['id'])
        self.assertEqual(old_open['status'], TASK_VOIDED)
        # 已回应任务保留原判，政策依据不变
        old_answered = self.service.repository.get_task(answered_task['id'])
        self.assertEqual(old_answered['status'], TASK_RESPONDED)
        self.assertEqual(old_answered['policy_version'], 'baseline')

        tasks = self.service.list_tasks(Actor('off', 'case_officer', 'org-a'), record_id=open_rec['id'])
        reissued = [t for t in tasks if t['status'] in (TASK_RESERVED, TASK_QUEUED)]
        self.assertEqual(len(reissued), 1)
        self.assertEqual(reissued[0]['policy_version'], '2026-reform')
        self.assertEqual(reissued[0]['due_day'], 125)

    def test_new_task_after_policy_uses_new_basis(self):
        r1 = submit_case(self.service, make_case(self.service, 'IMM-POL-3'))
        self.service.publish_policy(Actor('sup', 'supervisor', 'org-a'),
                                    {'version': 'p2', 'effective_day': 150, 'evidence_days': 7})
        self.service.set_capacity(Actor('sup', 'supervisor', 'org-a'),
                                  {'handler_id': 'H-7', 'day': 150, 'capacity': 1})
        task = self.service.reserve_evidence(Actor('off', 'case_officer', 'org-a'),
                                             {'record_id': r1['id'], 'handler_id': 'H-7', 'day': 150, 'evidence_request': 'x'},
                                             idempotency_key='new-policy')
        self.assertEqual(task['policy_version'], 'p2')
        self.assertEqual(task['due_day'], 157)

    def test_case_without_policy_backfilled_to_baseline_and_history_kept(self):
        # 直接造一条旧数据：案件缺 policy_version，且已有一条 responded 的历史任务
        repo = self.service.repository
        import json
        with repo._connect() as conn:
            now = '2026-01-01T00:00:00+00:00'
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                "INSERT INTO records(reference,state,version,organization,policy_version,payload,created_by,updated_by,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                ('IMM-LEGACY-1', 'submitted', 3, 'org-a', '',
                 json.dumps(CREATE_DATA, sort_keys=True), 'legacy', 'legacy', now, now))
            rid = int(cur.lastrowid)
            conn.execute(
                "INSERT INTO evidence_tasks(record_id,policy_version,handler_id,day,due_day,evidence_request,status,idempotency_key,documents,response_day,version,created_by,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (rid, 'old-2025', 'H-OLD', 110, 120, '历史补件', TASK_RESPONDED, 'legacy-key',
                 json.dumps(['old_doc']), 118, 2, 'legacy', now, now))
            conn.commit()

        # 重新初始化仓储，触发旧数据回填
        from src.repository import Repository
        from src.audit import AuditRecorder
        repo2 = Repository(str(Path(self.temp.name) / 'test.db'))
        self.service.repository = repo2
        self.service.audit = AuditRecorder(repo2)
        record = repo2.get(1)
        self.assertEqual(record['policy_version'], 'baseline')
        task = repo2.list_tasks(record_id=1)[0]
        # 历史已回应任务保留原判与原依据
        self.assertEqual(task['status'], TASK_RESPONDED)
        self.assertEqual(task['policy_version'], 'old-2025')


class BatchResumeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / 'test.db'))

    def tearDown(self):
        self.temp.cleanup()

    def test_batch_resumes_from_last_complete_batch(self):
        records = []
        for i in range(4):
            records.append(submit_case(self.service, make_case(self.service, 'IMM-BAT-%s' % i)))
        self.service.set_capacity(Actor('sup', 'supervisor', 'org-a'),
                                  {'handler_id': 'H-B', 'day': 140, 'capacity': 10})

        def items(fail_index=None):
            result = []
            for i, rec in enumerate(records):
                item = {'record_id': rec['id'], 'handler_id': 'H-B', 'day': 140, 'evidence_request': '材料%s' % i}
                if fail_index == i:
                    item = dict(item)
                    item['_fail'] = True
                result.append(item)
            return result

        first = self.service.issue_rfe_batch(Actor('off', 'case_officer', 'org-a'),
                                             {'items': items(fail_index=2)}, idempotency_key='batch-1')
        self.assertEqual(first['status'], 'interrupted')
        self.assertEqual(first['last_completed_index'], 1)
        self.assertEqual(first['resume_from_index'], 2)
        self.assertEqual(len(first['issued']), 2)

        # 重跑相同批次：前两项不重复占名额，从第3项继续直到完成
        second = self.service.issue_rfe_batch(Actor('off', 'case_officer', 'org-a'),
                                              {'items': items()}, idempotency_key='batch-1')
        self.assertEqual(second['status'], 'completed')
        self.assertEqual([item['index'] for item in second['issued']], [2, 3])

        all_tasks = self.service.list_tasks(Actor('off', 'case_officer', 'org-a'), handler_id='H-B')
        self.assertEqual(len(all_tasks), 4)
        cap = self.service.get_capacity(Actor('off', 'case_officer', 'org-a'), 'H-B', 140)
        self.assertEqual(cap['held'], 4)

        # 完成后重复请求直接回放，不再占名额
        third = self.service.issue_rfe_batch(Actor('off', 'case_officer', 'org-a'),
                                             {'items': items()}, idempotency_key='batch-1')
        self.assertTrue(third.get('idempotent_replayed'))
        cap = self.service.get_capacity(Actor('off', 'case_officer', 'org-a'), 'H-B', 140)
        self.assertEqual(cap['held'], 4)


if __name__ == '__main__':  # pragma: no cover
    unittest.main()
