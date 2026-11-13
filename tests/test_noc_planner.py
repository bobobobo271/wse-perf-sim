"""Behavioral auto-routing tests using explicit synthetic captures and WSE40 config.

Baseline: 5x8 mesh, 1.4/2 GHz core/NoC, 64 B/flit, 12 B header,
1 NoC cycle/hop, 256 B/core-cycle Shared write port. Vary one named condition.
"""
import copy
import itertools
import json
import math
import tempfile
import unittest
from pathlib import Path

from wse_perf_sim import ModelError, simulate, wse_config
from wse_perf_sim.frontend.communication import allgather_group, multicast_routes, route_tree
from wse_perf_sim.simulator import Simulator
from wse_perf_sim.frontend.capture import TraceAdapter
from test_model import fixture, op, tile

WORK = Path(__file__).resolve().parents[1] / 'workspace/calendar-integration/tests'


def group_capture(path, members, size=256):
    sources, targets, owners, rows = {}, {}, {}, []
    for thread, core in enumerate(members):
        owners[core] = f'produce{core}'
        sources[core] = tile(f'source{core}', owners[core], shape=(1, size // 4), shared=True)
        targets[core] = tile(f'target{core}', None, shape=(1, len(members) * size // 4), shared=True)
        rows.extend([op(owners[core], pe=thread, outputs=[sources[core]]),
                     op(f'consume{core}', pe=thread, inputs=[targets[core]], name='tile.adds',
                        outputs=[tile(f'out{core}', f'consume{core}')])])
    fixture(path, rows)
    manifest = json.loads((path / 'manifest.json').read_text())
    for entry in manifest['instances']:
        entry['mapping'] = dict(core_id=members[entry['thread_id']], pe_id=0)
    (path / 'manifest.json').write_text(json.dumps(manifest))
    return allgather_group(sources, targets, owners, size)


class AutomaticCalendarTests(unittest.TestCase):
    def setUp(self):
        WORK.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=WORK)
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.plan = group_capture(self.path, (0, 1, 8, 9))
        self.config = wse_config()

    def test_square_routes_transfer_and_publish_once(self):
        original = copy.deepcopy(self.plan)
        report = simulate(self.path, self.config, communication_plan=self.plan)
        schedule = report['noc']['schedule']['allgather']
        self.assertEqual(len(schedule), 8)
        expected_hops = 0
        for source in (0, 1, 8, 9):
            destinations = []
            for entry in schedule:
                if entry['source'] == source:
                    children, landing, _ = route_tree(source, entry['route_bits'])
                    destinations.extend(landing)
                    expected_hops += sum(map(len, children.values())) * math.ceil(256 / 52)
            self.assertEqual(sorted(destinations), [0, 1, 8, 9])
        self.assertEqual(sum(v['flits'] for v in report['noc']['links'].values()), expected_hops)
        self.assertEqual(report['noc']['phases']['allgather']['remote_committed_bytes'],
                         dict.fromkeys((0, 1, 8, 9), 768))
        events = report['noc']['events']
        writes = [e for e in events if e['kind'] == 'write_visible']
        self.assertEqual(len(writes), 16)
        self.assertEqual(sum(e['bytes'] for e in writes), 4096)
        rows = {row['op_id']: row for row in report['timeline']}
        for core in (0, 1, 8, 9):
            published, = [e for e in events if e['kind'] == 'receive_published' and e['node'] == core]
            self.assertGreaterEqual(published['time_ns'], max(e['time_ns'] for e in writes if e['node'] == core))
            self.assertGreater(rows[f'consume{core}']['issue'], rows[f'comm:allgather:{core}:wait']['complete'])
        self.assertEqual(self.plan, original)
        self.assertEqual(report['communication_plan'], original)

    def test_all_subsets_of_local_square_are_covered_without_duplicates(self):
        for size in range(1, 5):
            for destinations in itertools.combinations((0, 1, 8, 9), size):
                landed = []
                for bits in multicast_routes(0, destinations):
                    _, targets, _ = route_tree(0, bits)
                    landed.extend(targets)
                self.assertEqual(sorted(landed), list(destinations))

    def test_nonadjacent_group_uses_transit_routers(self):
        plan = group_capture(self.path, (0, 2, 16, 18))
        report = simulate(self.path, self.config, communication_plan=plan)
        self.assertTrue(any('1' in link.split(':')[1].split('->') for link in report['noc']['links']))
        self.assertFalse(any(e['kind'] == 'write_visible' and e['node'] == 1 for e in report['noc']['events']))

    def test_large_payload_gets_larger_windows(self):
        plan = group_capture(self.path, (0, 1, 8, 9), size=65536)
        report = simulate(self.path, self.config, communication_plan=plan)
        self.assertEqual(report['noc']['phases']['allgather']['remote_committed_bytes'],
                         dict.fromkeys((0, 1, 8, 9), 3 * 65536))
        self.assertTrue(all(e['end_cycle'] - e['start_cycle'] > 1024
                            for e in report['noc']['schedule']['allgather']))

    def test_slower_link_replans_windows_and_preserves_coverage(self):
        fast = simulate(self.path, self.config, communication_plan=self.plan)
        self.config['wse']['link_bytes_per_cycle']['value'] = 32
        slow = simulate(self.path, self.config, communication_plan=self.plan)
        self.assertGreater(slow['total_cycles'], fast['total_cycles'])
        self.assertEqual(slow['noc']['phases'], fast['noc']['phases'])
        self.assertGreater(slow['noc']['schedule']['allgather'][-1]['end_cycle'],
                           fast['noc']['schedule']['allgather'][-1]['end_cycle'])

    def test_component_order_is_independent_with_multi_tree(self):
        a = Simulator(TraceAdapter(self.path, self.config, self.plan), self.config)
        b = Simulator(TraceAdapter(self.path, self.config, self.plan), self.config)
        b.tick_order.reverse()
        self.assertEqual(a.run(), b.run())

    def test_invalid_automatic_inputs_fail(self):
        for field, value in [('destinations', [0, 0]), ('destinations', [39]), ('route_bits', 3)]:
            plan = copy.deepcopy(self.plan)
            plan['phases'][0]['nodes'][0][field] = value
            with self.assertRaises(ModelError):
                simulate(self.path, self.config, communication_plan=plan)
