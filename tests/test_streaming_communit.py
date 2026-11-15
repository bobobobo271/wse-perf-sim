"""Issue #4: deterministic WSE40 runs with finite 64/128 B endpoint buffers."""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

from examples.communication.streaming import build_demo, demo_layouts, execute, run_demo
from errors import ModelError
from runtime.communication import ReceiveLayout, Rejected, request_for
from analysis.pipeline import service_intervals
from models.noc.routing import route_bitmap
from models.noc.compiler import compile_calendar
from test_model import fixture, op, simulate, tile
from tiles import version_key


class CommUnitTests(unittest.TestCase):
    def test_broadcast_and_gather_match_bytes_with_bounded_buffers(self):
        for mode, remote in (('broadcast', {1: 512, 2: 512, 3: 512}), ('gather', {3: 1536})):
            for size in (64, 128):
                with self.subTest(mode=mode, buffer_bytes=size):
                    report = run_demo(mode, buffer_bytes=size)
                    events = report['events']
                    self.assertTrue(report['drained'])
                    self.assertLessEqual(max(report['tx_peak'].values()), size)
                    self.assertLessEqual(max(report['rx_peak'].values()), size)
                    aligned, = [e for e in events if e['kind'] == 'align']
                    self.assertGreater(aligned['cycle'], 33)  # Last rank submits at core cycle 33.
                    self.assertLess(next(e['cycle'] for e in events if e['kind'] == 'inject'),
                                    max(e['cycle'] for e in events if e['kind'] == 'read_done'))
                    if size == 128:
                        first_write = min(e['cycle'] for e in events if e['kind'] == 'write')
                        last_inject = max(e['cycle'] for e in events if e['kind'] == 'inject')
                        self.assertLess(first_write, last_inject)
                    for node, nbytes in remote.items():
                        writes = [e for e in events if e['kind'] == 'write_visible' and e['node'] == node]
                        self.assertEqual(sum(e['nbytes'] for e in writes), nbytes)
                        completion, = [e for e in events if e['kind'] == 'completion' and e['node'] == node]
                        self.assertEqual(completion['cycle'], max(e['cycle'] for e in writes))

    def test_late_read_fault_keeps_bindings_quarantined(self):
        unit, clients = build_demo('broadcast', read_delay=50)
        execute(unit, clients)
        self.assertIn('missed Calendar slot', unit.failure)
        self.assertFalse(unit.drained())
        self.assertFalse(any(c.published for c in clients.values()))
        self.assertTrue(all(c.completion.status == 'fault' and not c.completion.safe_to_release
                            for c in clients.values()))

    def test_rejects_wrong_byte_count_and_overlapping_layout(self):
        unit, _ = build_demo('gather')
        opcode, layouts = demo_layouts('gather')
        wrong = dict(layouts)
        wrong[3] = (None, ReceiveLayout('OUTPUT', 1536, 1535, ((0, 1536),)))
        with self.assertRaisesRegex(ModelError, 'expVal'):
            compile_calendar(opcode, wrong, unit.platform)
        wrong = dict(layouts)
        wrong[1] = (replace(layouts[1][0], self_off=0), None)
        with self.assertRaisesRegex(ModelError, 'Overlapping'):
            compile_calendar(opcode, wrong, unit.platform)

    def test_communit_rejects_wrong_size_and_duplicate_node(self):
        unit, clients = build_demo('broadcast')
        sub = clients[0].submission
        unit.submit(replace(sub, request=dict(sub.request, send_bytes=511)))
        (_, _, rejected), = unit.drain_outputs()
        self.assertIsInstance(rejected, Rejected)
        self.assertFalse(rejected.retryable)
        self.assertFalse(unit.participants)
        unit.submit(sub)
        unit.drain_outputs()
        unit.submit(replace(sub, token='duplicate-node'))
        (_, _, rejected), = unit.drain_outputs()
        self.assertIsInstance(rejected, Rejected)
        self.assertEqual(len(unit.participants), 1)

    def test_capture_comm_port_and_tilereg_share_stream_contract(self):
        work = Path(__file__).resolve().parents[1] / 'workspace/streaming-tests'
        work.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=work) as tmp:
            path = Path(tmp)
            x = tile('x', 'produce', shape=(1, 32))
            y = tile('y', None, shape=(1, 32), shared=True)
            fixture(path, [
                op('produce', outputs=[x]),
                op('receiver', pe=1),
                op('consume', pe=1, inputs=[y], name='tile.adds'),
            ])
            manifest = json.loads((path / 'manifest.json').read_text())
            for instance in manifest['instances']:
                instance['mapping'] = dict(core_id=instance['thread_id'], pe_id=0)
            (path / 'manifest.json').write_text(json.dumps(manifest))
            plan = dict(schema='wse-communication/stream-v1', non_speculative=True,
                        buffers=dict(access_bytes=32, tx_bytes=64, rx_bytes=64),
                        phases=[dict(name='broadcast', opcode=1, nodes=[
                            dict(core=0, owner='produce', send=dict(source_version=list(version_key(x)),
                                nbytes=128, route_bits=route_bitmap((0, 1), (1,)))),
                            dict(core=1, owner='receiver', receive=dict(target=y, exp_val=128)),
                        ])])
            from config import wse_config
            config = wse_config()
            config['message_log'] = True
            report = simulate(path, config, communication_plan=plan)
        self.assertTrue(report['noc']['drained'])
        self.assertTrue(any(i['lane'] == 'NoC/link:0->1' for i in service_intervals(report)))
        events = report['noc']['events']
        self.assertEqual(sum(e['nbytes'] for e in events if e['kind'] == 'write_visible'), 128)
        links = [e for e in events if e['kind'] == 'link']
        self.assertEqual([e['payload_bytes'] for e in links], [52, 52, 24])
        self.assertEqual([e['wire_bytes'] for e in links], [64, 64, 64])
        messages = report['messages']
        submitted = {m['node']: m for m in messages if m['type'] == 'CommSubmit'}
        self.assertEqual(submitted[0]['request'],
                         dict(opcode=1, route_bits=route_bitmap((0,1),(1,)), send_bytes=128))
        self.assertEqual(submitted[1]['request'],
                         dict(opcode=1, expected_receive_bytes=128))
        self.assertFalse(any(m['type'] == 'Accepted' or m.get('kind') == 'prepare' for m in messages))
        sent = [e for e in events if e['kind'] == 'sent'][-1]
        done = {e['node']: e['cycle'] for e in events if e['kind'] == 'completion'}
        self.assertEqual(done[0], sent['cycle'])
        self.assertEqual(done[1], max(e['cycle'] for e in events if e['kind'] == 'write_visible'))
        rows = {row['op_id']: row for row in report['timeline']}
        for node in (0, 1):
            self.assertEqual(rows[f'comm:broadcast:{node}:wait']['wait_start'], submitted[node]['sent'])
        self.assertLess(rows['comm:broadcast:0:submit']['complete'], rows['comm:broadcast:0:wait']['complete'])
        self.assertLess(rows['comm:broadcast:1:wait']['complete'], rows['consume']['issue'])

    def test_last_ready_submission_starts_alignment(self):
        # Same broadcast; change only the last endpoint's submission time.
        unit, clients = build_demo('broadcast')
        clients[3].arrival = 80
        execute(unit, clients)
        aligned, = [e for e in unit.events if e['kind'] == 'align']
        self.assertEqual(aligned['cycle'], 81)
        self.assertEqual(bytes(clients[3].destination), clients[0].source)

    def test_failed_write_never_publishes_destination(self):
        from examples.communication.streaming import ByteEndpoint
        class FailedWrite(ByteEndpoint):
            def access(self, command):
                if command.kind == 'write':
                    self.reply(command, 'fault', 'injected write failure')
                else:
                    super().access(command)
        unit, clients = build_demo('broadcast')
        old = clients[3]
        clients[3] = FailedWrite(3, old.submission.request, old.source, old.arrival, receive=old.receive_layout)
        execute(unit, clients)
        self.assertIn('injected write failure', unit.failure)
        self.assertFalse(clients[3].published)
        self.assertFalse(clients[3].completion.safe_to_release)

    def test_service_delay_within_bound_preserves_installed_slots(self):
        # Only add one cycle to fake core ReadDone; fixed slots and data unchanged.
        a, clients_a = build_demo('broadcast')
        b, clients_b = build_demo('broadcast', read_delay=1)
        execute(a, clients_a)
        execute(b, clients_b)
        self.assertEqual([e['cycle'] for e in a.events if e['kind']=='inject'],
                         [e['cycle'] for e in b.events if e['kind']=='inject'])
        self.assertEqual(bytes(clients_a[3].destination), bytes(clients_b[3].destination))

    def test_strided_source_and_padded_destination_copy_exact_bytes(self):
        # Keep 16x32 valid bytes and buffer/clock settings; add 8 B source and
        # 16 B destination padding per row. Padding must never be copied/count.
        from examples.communication.streaming import ByteEndpoint
        from models.noc.compiler import make_installation
        from models.noc.unit import CommUnit
        unit, clients = build_demo('broadcast')
        opcode, layouts = demo_layouts('broadcast')
        valid = tuple((row*48,row*48+32) for row in range(16))
        send = replace(layouts[0][0], src_row_stride=40, recv_row_stride=48)
        receive = ReceiveLayout('OUTPUT',768,512,valid)
        layouts = {0:(send,None), **dict.fromkeys((1,2,3),(None,receive))}
        installation = make_installation([compile_calendar(opcode,layouts,unit.platform)],unit.platform)
        unit = CommUnit(installation,unit.platform)
        source = bytes((i*13)%256 for i in range(640))
        expected = bytearray(768)
        for row in range(16):
            expected[row*48:row*48+32] = source[row*40:row*40+32]
        for node,old in list(clients.items()):
            request = request_for(opcode, *layouts[node])
            clients[node] = ByteEndpoint(node, request, source if node==0 else b'', old.arrival,
                                         receive=layouts[node][1])
        execute(unit,clients)
        for node in (1,2,3):
            self.assertEqual(clients[node].destination,expected)
