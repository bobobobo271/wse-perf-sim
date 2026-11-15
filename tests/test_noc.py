"""Integrated copy tests: synthetic captures, WSE40 profile, 256 B/source.

AllGather is lowered offline to gather+broadcast, then uses the sole copy ABI.
Old whole-packet timing expectations are replaced by buffer/visibility checks.
"""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from config import wse_config, validate_config
from errors import ModelError
from frontend.communication import compile_plan
from frontend.capture import TraceAdapter
from simulator import Simulator
from test_noc_planner import group_capture
from test_model import simulate, ROOT
from fixtures_wse import make_capture


class CalendarTests(unittest.TestCase):
    def setUp(self):
        (ROOT/'workspace').mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT/'workspace')
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.config = wse_config()
        self.plan = group_capture(self.path, (0,1))

    def run_model(self, plan=None, config=None):
        return simulate(self.path, config or self.config, communication_plan=plan or self.plan)

    def test_precompiled_file_load_and_execution_have_no_compiler_dependency(self):
        compiled = compile_plan(self.plan, self.config)
        path = self.path/'compiled.json'
        path.write_text(json.dumps(compiled))
        with patch('models.noc.compiler.compile_calendar', side_effect=AssertionError('compiled at runtime')), \
             patch('models.noc.routing.route_tree', side_effect=AssertionError('routed at runtime')):
            report = self.run_model(path)
        self.assertTrue(report['noc']['drained'])
        self.assertEqual(report['communication_installation']['version'], compiled['installation']['version'])

    def test_publication_wakes_consumer_after_wait(self):
        report = self.run_model()
        rows = {r['op_id']:r for r in report['timeline']}
        for core in (0,1):
            phase = 'allgather:gather' if core == 0 else 'allgather:broadcast1'
            waits = [rows[f'comm:{phase}:{core}:wait']['complete']]
            self.assertGreater(rows[f'consume{core}']['issue'], max(waits))
        for e in report['noc']['events']:
            if e['kind']=='completion' and e['remote_received']:
                writes = [w['cycle'] for w in report['noc']['events'] if w['kind']=='write_visible' and w['phase']==e['phase'] and w['node']==e['node']]
                self.assertGreaterEqual(e['cycle'], max(writes))

    def test_capacity_checked_before_communication(self):
        self.config['parameters']['shared_capacity']['value'] = 700
        with self.assertRaisesRegex(ModelError, 'Deadlock.*capacity'):
            self.run_model()

    def test_plan_hash_input_unchanged_and_platform_mismatch(self):
        original = copy.deepcopy(self.plan)
        self.run_model()
        self.assertEqual(original,self.plan)
        compiled = compile_plan(self.plan,self.config)
        self.config['wse']['link_bytes_per_cycle']['value'] = 32
        with self.assertRaisesRegex(ModelError,'platform mismatch'):
            self.run_model(compiled)

    def test_compiled_plan_rejects_changed_offset_even_with_same_operands(self):
        compiled = compile_plan(self.plan, self.config)
        compiled['phases'][0]['nodes'][0]['send']['src_offset'] += 1
        with self.assertRaisesRegex(ModelError, 'layout differs'):
            self.run_model(compiled)

    def test_clock_tick_order_independent(self):
        a = Simulator(TraceAdapter(self.path,self.config,self.plan),self.config)
        b = Simulator(TraceAdapter(self.path,self.config,self.plan),self.config)
        b.tick_order.reverse()
        self.assertEqual(a.run(),b.run())

    def test_non_speculative_contract_and_legacy_schema_rejected(self):
        for change in (dict(non_speculative=False),dict(schema='wse-communication/1')):
            with self.assertRaises(ModelError):
                self.run_model(dict(self.plan,**change))

    def test_bounded_buffers_and_wire_accounting(self):
        report = self.run_model()
        self.assertLessEqual(max(report['noc']['tx_peak'].values()),512)
        self.assertLessEqual(max(report['noc']['rx_peak'].values()),512)
        links = [e for e in report['noc']['events'] if e['kind']=='link']
        self.assertTrue(links)
        for e in links:
            self.assertEqual(e['wire_bytes'],64)
            self.assertTrue(0 < e['payload_bytes'] <= 52)
            self.assertAlmostEqual(e['end_ns']-e['time_ns'],0.5)

    def test_transit_nodes_do_not_receive_tiles(self):
        self.plan=group_capture(self.path,(0,2,16,18))
        report=self.run_model()
        self.assertTrue(any(1 in e['link'] for e in report['noc']['events'] if e['kind']=='link'))
        self.assertFalse(any(e['kind']=='write_visible' and e['node']==1 for e in report['noc']['events']))

    def test_perfetto_submit_wait_export(self):
        from analysis.perfetto import to_perfetto,to_pftrace
        report=self.run_model()
        names={e['name'] for e in to_perfetto(report['instruction_events'],cycle_ns=1e9/self.config['wse']['core_clock_hz']['value'])['traceEvents']}
        self.assertIn('request submission',names)
        self.assertIn('completion token wait',names)
        self.assertGreater(len(to_pftrace(report['instruction_events'],cycle_ns=1e9/self.config['wse']['core_clock_hz']['value'])),0)

    def test_larger_payload_preserves_allgather(self):
        self.plan=group_capture(self.path,(0,1,8,9),size=4096)
        report=self.run_model()
        self.assertTrue(report['noc']['drained'])
        self.assertEqual(len([r for r in report['timeline'] if r['op_id'].startswith('consume')]),4)

    def test_source_reference_survives_submit_retirement(self):
        trace=TraceAdapter(self.path,self.config,self.plan)
        source_keys=[]
        for entry in self.plan['phases'][0]['nodes']:
            key=tuple(entry['send']['source_version'])
            trace.versions[key].retained=False
            source_keys.append((entry['core'],key))
        report=Simulator(trace,self.config).run()
        rows={r['op_id']:r for r in report['timeline']}
        for core,key in source_keys:
            submit=rows[f'comm:allgather:gather:{core}:submit']
            freed,=[e for e in report['storage_events'] if e['action']=='free' and tuple(e['version'])==key]
            self.assertGreater(freed['cycle'],submit['source_read_complete'])
            self.assertGreater(freed['cycle'],submit['complete'])


class FullWseTests(unittest.TestCase):
    def test_allgather_40_row_column(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'workspace') as tmp:
            report=simulate(tmp,communication_plan=make_capture(tmp))
        self.assertTrue(report['noc']['drained'])
        self.assertEqual(len([r for r in report['timeline'] if str(r['op_id']).startswith('consume:')]),40)
        self.assertTrue(all(v<=1048576 for v in report['storage_peak_bytes'].values()))
