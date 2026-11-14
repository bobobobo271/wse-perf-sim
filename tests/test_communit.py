"""Public copy contract: fixed WSE40 config, 512 B sources, 32 B accesses.

Fault tests change only the named request or endpoint behavior.
"""
import copy
from dataclasses import asdict, replace
import json
import unittest
from unittest.mock import patch

from errors import ModelError
from examples.communication.streaming import build_demo, ByteEndpoint, execute
from models.noc.unit import CommUnit
from runtime.communication import (CommSubmit, DataReply, Completion, Rejected, request_from_dict)


class CommUnitTests(unittest.TestCase):
    def test_execution_never_calls_route_or_calendar_compiler(self):
        unit, clients = build_demo('broadcast')
        with patch('models.noc.compiler.compile_calendar', side_effect=AssertionError('runtime compilation')), \
             patch('models.noc.routing.route_tree', side_effect=AssertionError('runtime routing')):
            execute(unit, clients)
        self.assertTrue(unit.drained())

    def test_calreg_reused_by_two_epochs_with_distinct_bindings(self):
        unit, clients = build_demo('broadcast')
        installation = copy.deepcopy(unit.installation)
        for node, client in list(clients.items()):
            r = client.submission.request
            r = replace(r, request_id=f'next:{node}', epoch=2,
                send=replace(r.send, binding=replace(r.send.binding, version=2)) if r.send else None,
                receive=replace(r.receive, binding=replace(r.receive.binding, version=2)) if r.receive else None)
            clients[node+40] = ByteEndpoint(node, r, client.source, 8)
        execute(unit, clients)
        self.assertTrue(all(c.completion.status == 'success' for c in clients.values()))
        self.assertEqual(unit.installation, installation)
        aligns = [e for e in unit.events if e['kind'] == 'align']
        self.assertEqual([e['epoch'] for e in aligns], [1, 2])
        times = [[e['cycle']-a['origin'] for e in unit.events if e['kind']=='inject' and e['phase']==(1,a['epoch'])] for a in aligns]
        self.assertEqual(*times)
        self.assertTrue(any(e['kind']=='backpressure' for e in unit.events))

    def test_json_roundtrip(self):
        unit, clients = build_demo('gather')
        unit = CommUnit(json.loads(json.dumps(unit.installation)), json.loads(json.dumps(unit.platform)))
        for c in clients.values():
            r = request_from_dict(json.loads(json.dumps(asdict(c.submission.request))))
            self.assertEqual(r, c.submission.request)
        execute(unit, clients)
        self.assertTrue(unit.drained())

    def test_version_platform_and_missing_opcode_rejected(self):
        unit, clients = build_demo('broadcast')
        for fields in (dict(version='bad'), dict(opcode=999), dict(epoch=-1), dict(members=(0,1))):
            unit.submit(replace(clients[0].submission, request=replace(clients[0].submission.request, **fields)))
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

    def test_changed_layout_is_rejected_without_replanning(self):
        unit, clients = build_demo('broadcast')
        sub = clients[0].submission
        r = replace(sub.request, send=replace(sub.request.send, layout=replace(sub.request.send.layout, row_bytes=31)))
        unit.submit(replace(sub, request=r))
        (_,_,reply), = unit.drain_outputs()
        self.assertIn('differs from installed', reply.reason)
        self.assertFalse(unit.participants)

    def test_retired_epoch_and_duplicate_reply_do_not_write_new_version(self):
        unit, clients = build_demo('broadcast')
        execute(unit, clients)
        sub = clients[0].submission
        unit.submit(replace(sub, request=replace(sub.request, request_id='new-id')))
        (_,_,reply), = unit.drain_outputs()
        self.assertIn('retired', reply.reason)
        unit.receive('data', DataReply('req:1', 'write_done', 0))
        unit.tick(1000)
        self.assertIsNotNone(unit.failure)

    def test_sender_completion_precedes_receiver_publication(self):
        unit, clients = build_demo('broadcast')
        execute(unit, clients)
        source = next(e['cycle'] for e in unit.events if e['kind']=='completion' and e['node']==0)
        published = [e['cycle'] for e in unit.events if e['kind']=='published']
        self.assertLess(source, min(published))

    def test_request_ids_are_local_to_each_node(self):
        unit, clients = build_demo('broadcast')
        for client in clients.values():
            client.submission = replace(client.submission, request=replace(client.submission.request, request_id='same-local-id'))
        execute(unit, clients)
        self.assertTrue(all(c.completion.status == 'success' for c in clients.values()))
        self.assertEqual(bytes(clients[1].destination),clients[0].source)
