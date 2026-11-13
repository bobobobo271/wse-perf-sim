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
from collections import defaultdict
from pathlib import Path

from api import ModelError, wse_config
from frontend.communication import allgather_group, compile_phase, multicast_routes, route_tree
from simulator import Simulator
from frontend.capture import TraceAdapter
from test_model import fixture, op, simulate, tile

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
        intervals = defaultdict(list)
        for event in events:
            if event['kind'] in ('link', 'port'):
                intervals[event['resource']].append((event['time_ns'], event['end_ns']))
            elif event['kind'] == 'write_reservation':
                intervals[f'shared_write:{event["node"]}'].append((event['time_ns'], event['end_ns']))
        for resource, spans in intervals.items():
            for (_, end), (start, _) in zip(spans, spans[1:]):
                self.assertLessEqual(end, start, resource)
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

    def test_opposite_links_run_together_while_shared_writes_queue(self):
        plan = group_capture(self.path, (0, 1))
        self.config['engines']['shared_write']['throughput'] = 16
        report = simulate(self.path, self.config, communication_plan=plan)
        events = report['noc']['events']
        links = {e['resource']: e for e in events if e['kind'] == 'link' and e['flit'] == 0}
        self.assertEqual(links['link:0->1']['time_ns'], links['link:1->0']['time_ns'])
        for node in (0, 1):
            local, remote = [e for e in events if e['kind'] == 'write_reservation' and e['node'] == node]
            self.assertEqual(local['source'], node)
            self.assertEqual(local['service_cycles'], 16)
            self.assertLess(remote['ready_cycle'], local['start_cycle'] + local['service_cycles'])
            self.assertEqual(remote['start_cycle'], local['start_cycle'] + local['service_cycles'])
        self.assertEqual(report['noc']['phases']['allgather']['remote_committed_bytes'], {0: 256, 1: 256})

    def test_same_ejection_port_serializes_packets_from_distinct_links(self):
        phase = dict(routing='auto', nodes=[
            dict(core=core, nbytes=256, destinations=[1]) for core in (0, 1, 2)])
        compile_phase(phase, self.config)
        first, local, second = [entry['routes'][0] for entry in phase['nodes']]
        self.assertEqual(first['start_cycle'], 0)
        self.assertEqual(local['start_cycle'], 0)
        self.assertEqual(second['start_cycle'], math.ceil(256 / 52))

    def test_shared_directed_link_checks_each_routes_hop_offset(self):
        phase = dict(routing='auto', nodes=[
            dict(core=core, nbytes=256, destinations=[target])
            for core, target in ((0, 2), (1, 3), (2, 2), (3, 3))])
        compile_phase(phase, self.config)
        first, second, *_ = [entry['routes'][0] for entry in phase['nodes']]
        self.assertEqual(first['start_cycle'], 0)
        # Link 1->2 is the second hop of the first packet and the first hop of the second.
        self.assertEqual(second['start_cycle'], 1 + math.ceil(256 / 52))
        self.assertLess(second['start_cycle'], first['end_cycle'])

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
