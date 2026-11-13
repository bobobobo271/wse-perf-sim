"""Controlled Calendar tests: two-core cases isolate mechanisms; full WSE40 integrates them."""
import copy
import json
import math
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

from ssnpu_model import ModelError, simulate, default_config
from ssnpu_model.config import wse_config, validate_config
from ssnpu_model.noc_plan import route_bitmap, route_tree, operation_id
from ssnpu_model.simulator import Simulator
from ssnpu_model.trace import TraceAdapter
from ssnpu_model.tiles import version_key
from ssnpu_model.components.protocol import (ReceivePrepare, PortFence, WriteReservations,
    RouterWrite, PublishReceive, Access)
from examples.wse_allgather import make_capture
from test_model import fixture, op, tile

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / 'workspace/noc-tests'


def two_core(path, size=256, sram=False, barrier=False):
    sources = [tile(f'source{c}', f'produce{c}', shape=(1, size // 4), shared=True) for c in range(2)]
    targets = [tile(f'target{c}', None, shape=(1, size // 2), shared=True) for c in range(2)]
    rows = []
    for c in range(2):
        first = op(f'produce{c}', pe=c, outputs=[sources[c]])
        if sram:
            memory = dict(object_id=f'mem{c}', kind='tensor', dtype='float32', shape=[1, size // 4], memory_space='SRAM')
            first.update(ir_op='tile.load', inputs=[memory],
                         memory_accesses=[dict(object_id=memory['object_id'], shape=memory['shape'], offset_bytes=0)])
        rows.extend([first, op(f'consume{c}', pe=c, inputs=[targets[c]], name='tile.adds', phase=1 if barrier else 0,
                              outputs=[tile(f'out{c}', f'consume{c}', shape=(1, size // 2))])])
    fixture(path, rows, barriers=(0,) if barrier else ())
    manifest = json.loads((path / 'manifest.json').read_text())
    for entry in manifest['instances']:
        entry['mapping'] = dict(core_id=entry['thread_id'], pe_id=0)
    (path / 'manifest.json').write_text(json.dumps(manifest))
    plan = dict(schema='wse-communication/1', non_speculative=True, phases=[dict(
        name='pair', opcode=1, epoch=0, nodes=[dict(core=c, owner=f'produce{c}',
            source_version=list(version_key(sources[c])), target=targets[c], route_bits=route_bitmap({0, 1}, {0, 1}),
            window=c, nbytes=size, source_offset=0, destination_offset=c * size) for c in range(2)])])
    return plan


class CalendarTests(unittest.TestCase):
    def setUp(self):
        WORK.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=WORK)
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.plan = two_core(self.path)
        self.config = wse_config()

    def run_model(self, plan=None, config=None):
        return simulate(self.path, config or self.config, communication_plan=plan or self.plan)

    def test_two_core_payload_wire_and_endpoints(self):
        r = self.run_model()
        self.assertEqual(sum(v['flits'] for v in r['noc']['links'].values()), 10)
        self.assertEqual(sum(v['wire_bytes'] for v in r['noc']['links'].values()), 640)
        self.assertEqual(sum(v['payload_bytes'] for v in r['noc']['links'].values()), 512)
        self.assertFalse(any(k.endswith('/memory') for k in r['resources']))
        self.assertEqual(r['noc']['phases']['pair']['remote_committed_bytes'], {0: 256, 1: 256})
        self.assertEqual(r['noc']['adapter_reserved_peak_bytes'],
                         {'core:0/tx': 256, 'core:0/rx': 256, 'core:1/tx': 256, 'core:1/rx': 256})
        for e in r['noc']['events']:
            if e['kind'] == 'link':
                self.assertAlmostEqual(e['end_ns'] - e['time_ns'], .5)
                self.assertEqual(e['payload_bytes'] + e['header_bytes'] + e['padding_bytes'], 64)

    def test_publication_waits_for_all_segments_and_wakes_consumer(self):
        r = self.run_model()
        rows = {i['op_id']: i for i in r['timeline']}
        events = r['noc']['events']
        for c in range(2):
            writes = [e for e in events if e['kind'] == 'write_visible' and e['node'] == c]
            publish, = [e for e in events if e['kind'] == 'receive_published' and e['node'] == c]
            self.assertEqual(len(writes), 2)
            self.assertGreaterEqual(publish['time_ns'], max(e['time_ns'] for e in writes))
            receive = rows[operation_id('pair', c, 'receive')]
            self.assertGreaterEqual(rows[f'consume{c}']['issue'], receive['complete'] + 2)
            self.assertLess(rows[operation_id('pair', c, 'post')]['complete'], receive['complete'])

    def test_bandwidth_changes_flit_count_and_tail_only(self):
        fast = self.run_model()
        slow_config = copy.deepcopy(self.config)
        slow_config['wse']['link_bytes_per_cycle']['value'] = 32
        slow = self.run_model(config=slow_config)
        self.assertEqual(sum(v['flits'] for v in slow['noc']['links'].values()), 2 * math.ceil(256 / 20))
        self.assertGreater(slow['total_cycles'], fast['total_cycles'])
        self.assertEqual(fast['noc']['phases'], slow['noc']['phases'])

    def test_background_shared_write_waits_for_reserved_window(self):
        path = self.path / 'thread_0.jsonl'
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        background = op('background', deps=['produce0'],
                        outputs=[tile('background-tile', 'background', shape=(1, 40000), shared=True)])
        background.update(instance_op_sequence=2, order_dependencies=['consume0'])
        rows.append(background)
        path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
        manifest = json.loads((self.path / 'manifest.json').read_text())
        manifest['instances'][0]['operation_count'] = 3
        (self.path / 'manifest.json').write_text(json.dumps(manifest))
        r = self.run_model()
        background, = [row for row in r['timeline'] if row['op_id'] == 'background']
        write, = [stage for stage in background['stages'] if stage['engine'] == 'shared_write']
        reservations = [e for e in r['noc']['events'] if e['kind'] == 'write_reservation' and e['node'] == 0]
        for reservation in reservations:
            a, b = reservation['start_cycle'], reservation['start_cycle'] + reservation['service_cycles']
            self.assertTrue(write['service_end'] <= a or b <= write['start'])
        self.assertGreater(write['start'], background['writeback_start'] + 1)
        self.assertEqual(r['noc']['phases']['pair']['status'], 'done')

    def test_transit_only_router_does_not_write_a_tile(self):
        manifest = json.loads((self.path / 'manifest.json').read_text())
        manifest['instances'][1]['mapping']['core_id'] = 2
        (self.path / 'manifest.json').write_text(json.dumps(manifest))
        self.plan['phases'][0]['nodes'][1]['core'] = 2
        for e in self.plan['phases'][0]['nodes']:
            e['route_bits'] = route_bitmap({0, 1, 2}, {0, 2})
        r = self.run_model()
        self.assertEqual(sum(v['flits'] for v in r['noc']['links'].values()), 20)
        self.assertFalse(any(e['kind'] == 'write_visible' and e['node'] == 1 for e in r['noc']['events']))
        root = next(e for e in r['noc']['events'] if e['kind'] == 'link' and e['resource'] == 'link:0->1' and e['flit'] == 0)
        hop = next(e for e in r['noc']['events'] if e['kind'] == 'link' and e['resource'] == 'link:1->2' and e['flit'] == 0)
        self.assertEqual(hop['time_ns'] - root['time_ns'], .5)

    def test_core_frequency_does_not_change_link_duration(self):
        self.config['wse']['core_clock_hz']['value'] = 2000000000
        r = self.run_model()
        self.assertTrue(all(abs(e['end_ns'] - e['time_ns'] - .5) < 1e-8
                            for e in r['noc']['events'] if e['kind'] == 'link'))

    def test_component_tick_order_is_irrelevant(self):
        a = Simulator(TraceAdapter(self.path, self.config, self.plan), self.config)
        b = Simulator(TraceAdapter(self.path, self.config, self.plan), self.config)
        b.tick_order.reverse()
        self.assertEqual(a.run(), b.run())

    def test_sram_startup_is_serial_service_and_noc_has_no_memory_charge(self):
        self.plan = two_core(self.path, sram=True)
        r = self.run_model()
        for c in range(2):
            memory = r['resources'][f'core:{c}/memory']
            self.assertEqual(memory['service_cycles'], math.ceil((100 + 256 / 1000) * 1.4))
            self.assertEqual(memory['bytes'], 256)
        for row in r['timeline']:
            if row['ir_op'].startswith('calendar.'):
                self.assertFalse(any(s['engine'] == 'memory' for s in row['stages']))

    def test_source_can_release_before_tx_drains(self):
        trace = TraceAdapter(self.path, self.config, self.plan)
        for c in range(2):
            trace.versions[tuple(self.plan['phases'][0]['nodes'][c]['source_version'])].retained = False
        r = Simulator(trace, self.config).run()
        for c in range(2):
            key = self.plan['phases'][0]['nodes'][c]['source_version']
            freed, = [e for e in r['storage_events'] if e['action'] == 'free' and e['version'] == key]
            drained, = [e for e in r['noc']['events'] if e['kind'] == 'tx_drained' and e['source'] == c]
            self.assertLess(freed['cycle'] * 5 / 7, drained['time_ns'])

    def test_capacity_is_enforced_before_admission(self):
        self.config['parameters']['shared_capacity']['value'] = 700  # 256 source + 512 destination > 700
        with self.assertRaisesRegex(ModelError, 'Deadlock.*capacity'):
            self.run_model()

    def test_window_violation_is_not_silently_rescheduled(self):
        self.config['wse']['slot_cycles']['value'] = 1
        with self.assertRaisesRegex(ModelError, 'window exceeded'):
            self.run_model()

    def test_plan_rejects_holes_overlaps_and_wrong_scope(self):
        for field, value in [('destination_offset', 1), ('destination_offset', 256), ('source_offset', 1)]:
            p = copy.deepcopy(self.plan)
            p['phases'][0]['nodes'][0][field] = value
            with self.assertRaises(ModelError):
                self.run_model(plan=p)
        p = copy.deepcopy(self.plan)
        p['phases'][0]['nodes'][0]['target']['memory_space'] = 'LocalTileReg'
        with self.assertRaisesRegex(ModelError, 'SharedTileReg'):
            self.run_model(plan=p)

    def test_routes_reject_cycle_disconnection_and_illegal_bits(self):
        for bits in (route_bitmap({0, 1, 8, 9}, {0, 1}), route_bitmap({0, 2}, {0, 2}), 1):
            with self.assertRaises(ModelError):
                route_tree(0, bits)

    def test_perfetto_exports_calendar_without_inventing_compute_stages(self):
        from ssnpu_model.perfetto import to_perfetto, to_pftrace
        r = self.run_model()
        trace = to_perfetto(r['instruction_events'], cycle_ns=5/7)
        names = {e['name'] for e in trace['traceEvents']}
        self.assertIn('Calendar wait + TX drain', names)
        self.assertIn('receive wait + publication', names)
        self.assertGreater(len(to_pftrace(r['instruction_events'], cycle_ns=5/7)), 0)

    def test_non_speculative_contract_required(self):
        self.plan['non_speculative'] = False
        with self.assertRaisesRegex(ModelError, 'non_speculative'):
            self.run_model()

    def test_capture_barrier_includes_inserted_communication(self):
        self.plan = two_core(self.path, barrier=True)
        r = self.run_model()
        rows = {i['op_id']: i for i in r['timeline']}
        for c in range(2):
            self.assertGreaterEqual(rows['barrier:0:0:0']['issue'],
                                    rows[operation_id('pair', c, 'receive')]['complete'] + 2)
            self.assertGreaterEqual(rows[f'consume{c}']['issue'], rows['barrier:0:0:0']['complete'] + 2)

    def test_plan_file_hash_and_input_are_preserved(self):
        before = copy.deepcopy(self.plan)
        path = self.path / 'communication.json'
        text = json.dumps(self.plan, indent=2)
        path.write_text(text)
        r = simulate(self.path, self.config, communication_plan=path)
        import hashlib
        self.assertEqual(r['source_sha256']['communication_plan'], hashlib.sha256(text.encode()).hexdigest())
        self.assertEqual(r['communication_plan'], before)
        self.assertEqual(self.plan, before)

    def test_sram_read_and_write_share_one_serial_resource(self):
        self.plan = two_core(self.path, sram=True)
        for c in range(2):
            path = self.path / f'thread_{c}.jsonl'
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            memory = dict(object_id=f'destination{c}', kind='tensor', shape=[1, 128],
                          dtype='float32', memory_space='SRAM')
            rows[1].update(ir_op='tile.store', outputs=[memory],
                           memory_accesses=[dict(object_id=memory['object_id'], shape=memory['shape'], offset_bytes=256)])
            path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
        r = self.run_model()
        for c in range(2):
            self.assertEqual(r['resources'][f'core:{c}/memory']['service_cycles'], 141 + 141)
            self.assertEqual(r['resources'][f'core:{c}/memory']['bytes'], 256 + 512)

    def test_config_rejects_invalid_payload_and_missing_provenance(self):
        self.config['wse']['header_bytes']['value'] = 64
        with self.assertRaisesRegex(ModelError, 'payload'):
            validate_config(self.config)
        self.config = wse_config()
        self.config['wse']['slot_cycles'].pop('source')
        with self.assertRaisesRegex(ModelError, 'provenance'):
            validate_config(self.config)

    def prepared_tile(self):
        sim = Simulator(TraceAdapter(self.path, self.config, self.plan), self.config)
        t = sim.components['core:0/tile']
        receive = operation_id('pair', 0, 'receive')
        post = operation_id('pair', 0, 'post')
        self.assertIsNone(t.allocate(sim.nodes[post]))
        version, = sim.nodes[receive].outputs
        t.calendar_message(ReceivePrepare(receive, version, ((0, 0, 256), (1, 256, 256))))
        t.calendar_message(PortFence(receive))
        t.calendar_message(WriteReservations(receive, ((0, 10, 1, 2), (1, 20, 1, 2))))
        return sim, t, receive, version

    def test_reserved_write_gaps_and_independent_read_port(self):
        sim, t, receive, version = self.prepared_tile()
        source = sim.nodes['produce0']
        t.allocate(source)
        key, = source.outputs
        request = Access(source.id, 'ordinary-write', key, True, 512, 'float32')
        t.cycle = 9  # Two-cycle write would overlap [10,11).
        self.assertIsNone(t.start_access(request))
        # Reads use another port; exercise a legal consumer's read request.
        send = sim.nodes[operation_id('pair', 0, 'send')]
        read = Access(send.id, 'read', key, False, 256, 'float32')
        self.assertIsNotNone(t.start_access(read))
        t.cycle = 11
        self.assertIsNotNone(t.start_access(request))

    def test_partial_pending_duplicate_and_stale_generation(self):
        _, t, receive, version = self.prepared_tile()
        t.cycle = 10
        write = RouterWrite(receive, version, 0, 0, 256)
        t.calendar_message(write)
        t.calendar_message(write)  # Duplicate while write is pending.
        self.assertEqual(t.service.bytes['core:0/shared_write'], 256)
        with self.assertRaisesRegex(ModelError, 'Premature'):
            t.calendar_message(PublishReceive(receive))
        with self.assertRaisesRegex(ModelError, 'Stale'):
            t.calendar_message(RouterWrite(receive, ('shared', 'old'), 1, 256, 256))
        with self.assertRaisesRegex(ModelError, 'authorized'):
            t.calendar_message(RouterWrite(receive, version, 0, 1, 256))
        t.tick(13)
        self.assertNotIn(version, t.ready)
        t.cycle = 20
        t.calendar_message(RouterWrite(receive, version, 1, 256, 256))
        t.tick(23)
        t.calendar_message(PublishReceive(receive))
        self.assertIn(version, t.ready)
        t.calendar_message(write)  # Duplicate after commit also consumes no service.
        self.assertEqual(t.service.bytes['core:0/shared_write'], 512)

    def test_late_write_fails(self):
        _, t, receive, version = self.prepared_tile()
        t.cycle = 11
        with self.assertRaisesRegex(ModelError, 'missed'):
            t.calendar_message(RouterWrite(receive, version, 0, 0, 256))


class FullWseTests(unittest.TestCase):
    def test_archived_pypto_capture_can_supply_noc_sources(self):
        # Adapter integration only: keep the capture's synthetic memory paths;
        # this test makes no DDR -> local SRAM hardware interpretation.
        for name in ('matmul', 'fa'):
            with self.subTest(capture=name):
                config = default_config()
                config['wse'] = wse_config()['wse']
                capture = ROOT / 'examples' / name / 'capture'
                original = TraceAdapter(capture, config)
                latest = {}
                for version in original.versions.values():
                    if version.pool.endswith('/shared'):
                        latest[original.instructions[version.producer].core] = version
                entries = [dict(core=core, owner=v.producer, source_version=list(v.key),
                    target=tile(f'archive-recv-{core}', None, shape=(1, 128), shared=True),
                    route_bits=route_bitmap({0, 1}, {0, 1}), window=core,
                    nbytes=256, source_offset=0, destination_offset=core * 256)
                    for core, v in latest.items()]
                plan = dict(schema='wse-communication/1', non_speculative=True,
                            phases=[dict(name='archive', opcode=1, epoch=0, nodes=entries)])
                report = simulate(capture, config, communication_plan=plan)
                self.assertEqual(report['noc']['phases']['archive']['remote_committed_bytes'], {0: 256, 1: 256})
                self.assertEqual(report['instruction_count'], sum(not i.control for i in original.instructions.values()) + 6)

    def test_allgather_40_256B(self):
        WORK.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=WORK) as directory:
            plan = make_capture(directory)
            report = simulate(directory, communication_plan=plan)
        self.assertEqual(report['instruction_count'], 320)  # 80 capture + 240 Calendar commands.
        for phase, amount in [('row', 1792), ('column', 8192)]:
            state = report['noc']['phases'][phase]
            self.assertEqual(state['status'], 'done')
            self.assertEqual(state['remote_committed_bytes'], dict.fromkeys(range(40), amount))
        links = report['noc']['links'].values()
        # 5 rows * 8 sources * 7 edges * 5 flits + 8 columns * 5 sources * 4 edges * 40 flits.
        self.assertEqual(sum(v['flits'] for v in links), 7800)
        self.assertEqual(sum(v['wire_bytes'] for v in links), 499200)
        self.assertEqual(sum(v['payload_bytes'] for v in links), 399360)
        for e in report['noc']['events']:
            if e['kind'] == 'write_reservation':
                self.assertEqual(e['service_cycles'], 1 if e['phase'] == 'row' else 8)
                self.assertEqual(e['visible_cycle'], e['start_cycle'] + e['service_cycles'] + 2)
        self.assertTrue(all(v <= 1048576 for v in report['storage_peak_bytes'].values()))
        self.assertEqual(report['total_time_ns_exact'], str(Fraction(report['total_cycles'] * 5, 7)))
        self.assertFalse(any(k.endswith('/memory') for k in report['resources']))


if __name__ == '__main__':
    unittest.main()
