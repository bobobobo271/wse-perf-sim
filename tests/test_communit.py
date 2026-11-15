"""Public copy contract: fixed WSE40 config, 512 B sources, 32 B accesses.

Fault tests change only the named request or endpoint behavior.
"""
import copy
from dataclasses import replace
import json
import unittest
from unittest.mock import patch

from errors import ModelError
from examples.communication.streaming import build_demo, ByteEndpoint, execute
from models.noc.unit import CommUnit
from runtime.communication import (DataReply, CommRequest, Rejected)


class CommUnitTests(unittest.TestCase):
    def test_execution_never_calls_route_or_calendar_compiler(self):
        unit, clients = build_demo('broadcast')
        with patch('models.noc.compiler.compile_calendar', side_effect=AssertionError('runtime compilation')), \
             patch('models.noc.routing.route_tree', side_effect=AssertionError('runtime routing')):
            execute(unit, clients)
        self.assertTrue(unit.drained())

    def test_calreg_reused_by_two_sequential_submissions(self):
        unit, clients = build_demo('broadcast')
        installation = copy.deepcopy(unit.installation)
        for node, client in list(clients.items()):
            clients[node+40] = ByteEndpoint(node, client.submission.request, client.source, 40,
                                            token=f'next:{node}', receive=client.receive_layout)
        execute(unit, clients)
        self.assertTrue(all(c.completion.status == 'success' for c in clients.values()))
        self.assertEqual(unit.installation, installation)
        aligns = [e for e in unit.events if e['kind'] == 'align']
        self.assertEqual([e['opcode'] for e in aligns], [1, 1])
        self.assertTrue(all('epoch' not in e for e in aligns))
        injects = [e['cycle'] for e in unit.events if e['kind'] == 'inject']
        first, second = aligns
        self.assertEqual([cycle-first['origin'] for cycle in injects if cycle < second['origin']],
                         [cycle-second['origin'] for cycle in injects if cycle >= second['origin']])
        self.assertTrue(any(e['kind']=='backpressure' for e in unit.events))

    def test_json_roundtrip(self):
        unit, clients = build_demo('gather')
        unit = CommUnit(json.loads(json.dumps(unit.installation)), json.loads(json.dumps(unit.platform)))
        for c in clients.values():
            r = CommRequest(**json.loads(json.dumps(c.submission.request)))
            self.assertEqual(r, c.submission.request)
        execute(unit, clients)
        self.assertTrue(unit.drained())

    def test_platform_and_bad_operands_rejected(self):
        unit, clients = build_demo('broadcast')
        for fields in (dict(route_bits=0), dict(opcode=999), dict(opcode=-1), dict(send_bytes=9)):
            unit.submit(replace(clients[0].submission, request=dict(clients[0].submission.request, **fields)))
            (_,_,reply), = unit.drain_outputs()
            self.assertIsInstance(reply, Rejected)
            self.assertFalse(reply.retryable)
        wrong = copy.deepcopy(unit.platform)
        wrong['tx_bytes'] += 1
        with self.assertRaisesRegex(ModelError, 'platform'):
            CommUnit(unit.installation, wrong)
        wrong = copy.deepcopy(unit.installation)
        wrong['calreg']['1']['stride'] += 1  # Demo transports configuration through JSON.
        with self.assertRaisesRegex(ModelError, 'installation'):
            CommUnit(wrong, unit.platform)

    def test_changed_size_is_rejected_without_replanning(self):
        unit, clients = build_demo('broadcast')
        sub = clients[0].submission
        r = dict(sub.request, send_bytes=31)
        unit.submit(replace(sub, request=r))
        (_,_,reply), = unit.drain_outputs()
        self.assertIn('differ from installed', reply.reason)
        self.assertFalse(unit.participants)

    def test_duplicate_submission_and_reply_do_not_write_new_version(self):
        unit, clients = build_demo('broadcast')
        execute(unit, clients)
        sub = clients[0].submission
        unit.submit(sub)
        (_,_,reply), = unit.drain_outputs()
        self.assertIn('duplicate', reply.reason)
        unit.receive('data', DataReply('req:1', 'write_done', 0))
        unit.tick(1000)
        self.assertIsNotNone(unit.failure)

    def test_sender_completion_precedes_receiver_publication(self):
        unit, clients = build_demo('broadcast')
        execute(unit, clients)
        source = next(e['cycle'] for e in unit.events if e['kind']=='completion' and e['node']==0)
        published = [e['cycle'] for e in unit.events if e['kind']=='completion' and e['node'] != 0]
        self.assertLess(source, min(published))

    def test_request_ids_are_local_to_each_node(self):
        unit, clients = build_demo('broadcast')
        for client in clients.values():
            client.submission = replace(client.submission, token='same-local-id')
        execute(unit, clients)
        self.assertTrue(all(c.completion.status == 'success' for c in clients.values()))
        self.assertEqual(bytes(clients[1].destination),clients[0].source)
