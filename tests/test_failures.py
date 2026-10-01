import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, PermissionDenied


CREATE_DATA = {'applicant_id': 'A-900', 'case_type': 'family', 'received_day': 100, 'deadline_days': 30, 'response_day': 110, 'representation_active': True, 'required_documents': ['passport', 'sponsor_letter']}
FLOW = [('submit', 'legal_rep', {'documents': ['passport', 'sponsor_letter']}, 'submitted'), ('request_evidence', 'case_officer', {'evidence_request_day': 115, 'allowed_days': 10, 'evidence_request': '补充收入证明'}, 'evidence_requested'), ('respond', 'legal_rep', {'response_day': 120, 'documents': ['income_proof']}, 'response_received'), ('decide', 'case_officer', {'decision': 'granted', 'decision_reason': '材料充分'}, 'decided')]


class FailureTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))

    def tearDown(self):
        self.temp.cleanup()

    def test_permission_and_duplicate(self):
        with self.assertRaises(PermissionDenied):
            self.service.create(Actor("outsider", "outsider"), "IMM-29001", CREATE_DATA)
        self.service.create(Actor("creator", "intake_officer"), "IMM-29001", CREATE_DATA)
        with self.assertRaises(Conflict):
            self.service.create(Actor("creator", "intake_officer"), "IMM-29001", CREATE_DATA)

    def test_stale_version_is_rejected(self):
        record = self.service.create(Actor("creator", "intake_officer"), "IMM-29001", CREATE_DATA)
        first = FLOW[0]
        record = self.service.act(Actor("operator", first[1]), record["id"], record["version"], first[0], first[2])
        second = FLOW[1]
        with self.assertRaises(Conflict):
            self.service.act(Actor("operator", second[1]), record["id"], record["version"] - 1, second[0], second[2])
