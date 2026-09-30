import json
import unittest
from datetime import datetime, timezone
from single_call_grant import match_single_call


class SingleGrantTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026,9,30,14,30,tzinfo=timezone.utc)
        self.call = {'request_id':'one-approved-intent', 'phone':'+12025550123',
                     'recipient_name':'Pat', 'email':'pat@example.com', 'mission':'Arrange an agreed meeting',
                     'capabilities':['check_calendar','create_meeting']}
        self.grant = {'expires_at':'2026-09-30T15:00:00Z','call':self.call}

    def match(self, candidate=None, grant=None):
        return match_single_call(candidate or self.call,json.dumps(grant or self.grant),self.now)

    def test_exact_match_uses_server_body(self):
        caller = self.call | {'request_id':'caller-controlled','capabilities':['send_email'],'approved_logistics':'injected'}
        self.assertEqual(self.match(caller),self.call)

    def test_recipient_email_name_and_mission_each_bound(self):
        for field in ('phone','email','recipient_name','mission'):
            with self.subTest(field=field): self.assertIsNone(self.match(self.call | {field:'different'}))

    def test_expired_and_naive_expiry_block(self):
        for expiry in ('2026-09-30T14:30:00Z','2026-09-30T14:00:00Z','2026-09-30T15:00:00','invalid'):
            with self.subTest(expiry=expiry): self.assertIsNone(self.match(grant=self.grant | {'expires_at':expiry}))

    def test_missing_or_malformed_config_blocks(self):
        for raw in ('','null','[]','{}','invalid',json.dumps(self.grant | {'extra':True})):
            with self.subTest(raw=raw): self.assertIsNone(match_single_call(self.call,raw,self.now))

    def test_no_empty_bound_identity_fields(self):
        for field in ('phone','email','recipient_name','mission','request_id'):
            self.assertIsNone(self.match(grant=self.grant | {'call':self.call | {field:''}}))
