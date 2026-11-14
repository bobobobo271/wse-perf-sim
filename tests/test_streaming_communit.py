"""Issue #4: deterministic WSE40 runs with finite 32/64 B endpoint buffers."""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

from examples.communication.streaming import build_demo, demo_layouts, execute, run_demo
from errors import ModelError
from runtime.communication import ReceiveLayout, Rejected
from analysis.pipeline import service_intervals
from models.noc.routing import route_bitmap
from models.noc.compiler import compile_calendar
from test_model import fixture, op, simulate, tile
from tiles import version_key


class CommUnitTests(unittest.TestCase):
    def test_broadcast_and_gather_match_bytes_with_bounded_buffers(self):
        for mode, remote in (('broadcast', {1: 512, 2: 512, 3: 512}), ('gather', {3: 1536})):
            for size in (32, 64):
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
                    if size == 64:
                        first_write = min(e['cycle'] for e in events if e['kind'] == 'write')
                        last_inject = max(e['cycle'] for e in events if e['kind'] == 'inject')
                        self.assertLess(first_write, last_inject)
                    for node, nbytes in remote.items():
                        writes = [e for e in events if e['kind'] == 'write_visible' and e['node'] == node]
                        self.assertEqual(sum(e['nbytes'] for e in writes), nbytes)
                        publish, = [e for e in events if e['kind'] == 'published' and e['node'] == node]
                        self.assertGreater(publish['cycle'], max(e['cycle'] for e in writes))
                        completion, = [e for e in events if e['kind'] == 'completion' and e['node'] == node]
                        self.assertGreaterEqual(completion['cycle'], publish['cycle'])

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

    def test_communit_rejects_stale_plan_and_duplicate_node(self):
        unit, clients = build_demo('broadcast')
        sub = clients[0].submission
        unit.submit(replace(sub, request=replace(sub.request, version='stale')))
        (_, _, rejected), = unit.drain_outputs()
        self.assertIsInstance(rejected, Rejected)
        self.assertFalse(rejected.retryable)
        self.assertFalse(unit.participants)
        unit.submit(sub)
        unit.drain_outputs()
        unit.submit(replace(sub, request=replace(sub.request, request_id='duplicate-node')))
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
                        phases=[dict(name='broadcast', opcode=1, epoch=1, nodes=[
                            dict(core=0, owner='produce', send=dict(source_version=list(version_key(x)),
                                src_offset=0, row_count=1, row_bytes=128, src_row_stride=128,
                                recv_sym_base='OUTPUT', self_off=0, recv_row_stride=128,
                                route_bits=route_bitmap((0, 1), (1,)))),
                            dict(core=1, owner='receiver', receive=dict(target=y, recv_sym_base='OUTPUT', exp_val=128)),
                        ])])
            report = simulate(path, communication_plan=plan)
        self.assertTrue(report['noc']['drained'])
        self.assertTrue(any(i['lane'] == 'NoC/link:0->1' for i in service_intervals(report)))
        events = report['noc']['events']
        self.assertEqual(sum(e['nbytes'] for e in events if e['kind'] == 'write_visible'), 128)
        rows = {row['op_id']: row for row in report['timeline']}
        self.assertLess(rows['comm:broadcast:0:submit']['complete'], rows['comm:broadcast:0:wait']['complete'])
        self.assertLess(rows['comm:broadcast:1:wait']['complete'], rows['consume']['issue'])

    def test_prepare_waits_for_capacity_before_align(self):
        # Same broadcast/config/data, only node 3's prepare service waits until 80.
        from examples.communication.streaming import ByteEndpoint
        class LateCapacity(ByteEndpoint):
            held = None
            def access(self, command):
                if command.kind == 'prepare' and self.cycle < 80:
                    self.held = command
                else:
                    super().access(command)
            def tick(self, cycle):
                super().tick(cycle)
                if cycle >= 80 and self.held is not None:
                    command, self.held = self.held, None
                    super().access(command)
        unit, clients = build_demo('broadcast')
        old = clients[3]
        clients[3] = LateCapacity(3, old.submission.request, old.source, old.arrival)
        execute(unit, clients)
        aligned, = [e for e in unit.events if e['kind'] == 'align']
        self.assertGreater(aligned['cycle'], 80)
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
        clients[3] = FailedWrite(3, old.submission.request, old.source, old.arrival)
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
        from runtime.communication import ReceiveDesc, SendDesc
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
            r=old.submission.request
            r=replace(r,version=installation['version'],
                      send=SendDesc(r.send.binding,send) if r.send else None,
                      receive=ReceiveDesc(r.receive.binding,receive) if r.receive else None)
            clients[node]=ByteEndpoint(node,r,source if node==0 else b'',old.arrival)
        execute(unit,clients)
        for node in (1,2,3):
            self.assertEqual(clients[node].destination,expected)
