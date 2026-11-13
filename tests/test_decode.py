"""Decode validation conditions live with each test; generated artifacts use workspace/."""
import copy
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from api import simulate, ModelError
from workloads.decode import DecodeSpec, make_decode_capture
from frontend.capture import TraceAdapter
from simulator import Simulator

WORK = Path(__file__).resolve().parents[1] / 'workspace/decode-tests'


class DecodeTimingTests(unittest.TestCase):
    def setUp(self):
        WORK.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=WORK)
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.spec = DecodeSpec(query_heads=4, kv_heads=1, head_dim=8, sequence_length=19,
                               sequence_splits=3, tile_tokens=4, hbm_chunk_tokens=8)

    def build(self, spec=None, name='capture'):
        path = self.path / name
        config, plan, layout = make_decode_capture(path, spec or self.spec)
        return path, config, plan, layout

    def test_bytes_completion_and_single_output_owner(self):
        s = self.spec
        path, config, plan, layout = self.build()
        report = simulate(path, config, communication_plan=plan)
        # Cold FP16 K/V read once; Q explicitly replicated from HBM to each
        # sequence worker; one FP32 output per head written back to HBM.
        expected = 4*s.batch*s.kv_heads*s.sequence_length*s.head_dim
        expected += 2*s.batch*s.query_heads*s.head_dim*s.sequence_splits
        expected += 4*s.batch*s.query_heads*s.head_dim
        self.assertEqual(sum(e['bytes'] for e in report['hbm_events']), expected)
        self.assertEqual(report['resources']['chip/hbm']['bytes'], expected)
        self.assertEqual(sum(e['direction'] == 'write' for e in report['hbm_events']), s.batch*s.kv_heads)
        state_bytes = 4*s.query_heads*(s.head_dim+2)
        phase = report['noc']['phases']['split_kv:0']
        self.assertEqual(phase['status'], 'done')
        self.assertEqual(set(phase['remote_committed_bytes'].values()), {state_bytes*(s.sequence_splits-1)})
        self.assertEqual(sum(m['end']-m['start'] for m in layout['mapping']), s.sequence_length)

    def test_hbm_is_one_global_serial_server_and_bandwidth_is_parameterized(self):
        # Same capture, clocks, SRAM, compute and routes; only chip HBM BW varies.
        path, config, plan, _ = self.build()
        fast = simulate(path, config, communication_plan=plan)
        slow_config = copy.deepcopy(config)
        slow_config['hbm']['bandwidth_bytes_per_second'] = 1e9
        slow = simulate(path, slow_config, communication_plan=plan)
        events = sorted(slow['hbm_events'], key=lambda e: e['start_cycle'])
        self.assertTrue(all(a['end_cycle'] <= b['start_cycle'] for a, b in zip(events, events[1:])))
        self.assertGreater(slow['resources']['chip/hbm']['service_cycles'], fast['resources']['chip/hbm']['service_cycles'])
        self.assertGreater(slow['total_cycles'], fast['total_cycles'])
        self.assertEqual(slow['resources']['chip/hbm']['bytes'], fast['resources']['chip/hbm']['bytes'])

    def test_tick_order_does_not_change_hbm_arbitration(self):
        path, config, plan, _ = self.build()
        a = Simulator(TraceAdapter(path, config, plan), config)
        b = Simulator(TraceAdapter(path, config, plan), config)
        b.tick_order.reverse()
        self.assertEqual(a.run(), b.run())

    def test_memory_stages_and_pipeline_export_use_report_times(self):
        from analysis.pipeline import service_intervals, export_resource_trace
        from analysis.perfetto import to_pftrace
        from collections import defaultdict
        import json
        path, config, plan, _ = self.build()
        trace = TraceAdapter(path, config, plan)
        report = Simulator(trace, config).run()
        # Check all scheduler dependencies and all resource occupancy intervals,
        # including HBM endpoint writes contending with ordinary SRAM reads.
        timeline = {row['op_id']: row for row in report['timeline']}
        services = defaultdict(list)
        for key, instruction in trace.instructions.items():
            row = timeline[key]
            self.assertTrue(all(timeline[dep]['complete']+2 <= row['issue'] for dep in instruction.deps | instruction.sync))
            for stage in row['stages']:
                services[stage['resource']].append((stage['start'], stage['service_end']))
        for resource, spans in services.items():
            spans.sort()
            self.assertTrue(all(a[1] <= b[0] for a, b in zip(spans, spans[1:])), resource)
        for pool, peak in report['storage_peak_bytes'].items():
            self.assertLessEqual(peak, config['parameters']['shared_capacity' if pool.endswith('/shared') else 'local_capacity']['value'])
        intervals = service_intervals(report)
        self.assertTrue({'HBM', 'SRAM', 'Cube', 'Vector', 'Calendar'} <= {i['kind'] for i in intervals})
        hbm = [event for event in intervals if event['kind'] == 'HBM']
        self.assertAlmostEqual(sum(i['end_us']-i['start_us'] for i in hbm),
            report['resources']['chip/hbm']['service_cycles']*1e6/config['wse']['core_clock_hz']['value'])
        export_resource_trace(report, self.path)
        data = json.loads((self.path/'resources.trace.json').read_text())
        self.assertEqual(sum(e['ph'] == 'X' for e in data['traceEvents']), len(intervals))
        self.assertTrue(to_pftrace(report['instruction_events'], cycle_ns=1e9/config['wse']['core_clock_hz']['value']))

    def test_double_buffer_reuse_waits_for_readers(self):
        # Five HBM chunks require repeated reuse of both SRAM slots. Increase
        # compute relative to HBM so premature overwrite would be exposed.
        spec = replace(self.spec, sequence_length=41, sequence_splits=1)
        path, config, plan, _ = self.build(spec)
        config['engines']['cube']['throughput'] = 64
        trace = TraceAdapter(path, config, plan)
        report = Simulator(trace, config).run()
        rows = {r['op_id']: r for r in report['timeline']}
        reused = 0
        for instruction in trace.instructions.values():
            if instruction.raw['ir_op'] == 'memory.copy' and instruction.raw['dependencies']:
                if instruction.raw['outputs'][0]['memory_space'] == 'SRAM':
                    reused += 1
                    self.assertTrue(all(rows[d]['complete'] < rows[instruction.id]['issue'] for d in instruction.raw['dependencies']))
        self.assertGreaterEqual(reused, 4)

    def test_empty_sequence_shards_and_waves(self):
        # 21 KV heads * 2 splits exceeds 40 cores: wave 1 reuses wave 0 cores.
        spec = replace(self.spec, query_heads=21, kv_heads=21, sequence_length=1, sequence_splits=2)
        path, config, plan, layout = self.build(spec)
        report = simulate(path, config, communication_plan=plan)
        self.assertEqual({entry['wave'] for entry in layout['mapping']}, {0, 1})
        self.assertTrue(all(phase['status'] == 'done' for phase in report['noc']['phases'].values()))

    def test_invalid_dimensions_and_bandwidth(self):
        for change in (dict(query_heads=5, kv_heads=2), dict(sequence_splits=41),
                       dict(hbm_gbps=0), dict(hbm_gbps=float('nan')), dict(sequence_length=0)):
            with self.assertRaises(ModelError):
                self.build(replace(self.spec, **change))


if __name__ == '__main__':
    unittest.main()
