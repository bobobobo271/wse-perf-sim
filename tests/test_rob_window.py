"""Admission capacity, retirement backpressure and next-cycle protocol checks."""
import copy
import json
import pickle
import random

from api import ModelError, default_config
from simulator import Simulator
from models.ssnpu import ROB
from runtime.protocol import Admitted, Complete, Issued
from config import validate_config
from frontend.capture import TraceAdapter
from test_model import ModelTestCase, audit, fixture, op, simulate, tile


class WindowTests(ModelTestCase):
    def test_depth_two_retains_younger_completion_until_ordered_retirement(self):
        self.c['parameters']['rob_depth']['value'] = 2
        self.c['message_log'] = True
        self.c['engines']['exp'].update(throughput=1, latency=100)
        slow = op(0, outputs=[tile(0, 0, (1, 40))], name='tile.exp')
        report, rows = self.run_rows([slow, op(1), op(2), op(3)])
        self.assertEqual([rows[k]['admitted'] for k in (0, 1)], [0, 0])
        self.assertLess(rows[1]['rob_complete'], rows[0]['rob_complete'])
        self.assertEqual(rows[1]['retire'], rows[0]['retire'])
        self.assertEqual([rows[k]['admitted'] for k in (2, 3)], [rows[0]['retire']] * 2)
        self.assertEqual(rows[2]['allocation_request'], rows[2]['admitted'] + 1)
        self.assertEqual(report['wait_reason_sets']['2']['waiting_admission'], rows[2]['admission_received'])
        self.assertEqual([(m['instruction'], m['sent'], m['received']) for m in report['messages']
                          if m['type'] == 'Admitted'],
                         [(k, rows[k]['admitted'], rows[k]['admitted'] + 1) for k in range(4)])
        self.assertFalse(any(e['action'] == 'allocate' and e['version'] == ['storage', 2, 1]
                             and e['cycle'] <= rows[2]['admitted'] for e in report['storage_events']))

    def test_ready_bypass_and_width_within_window(self):
        self.c['parameters']['rob_depth']['value'] = 4
        self.c['parameters']['pe_issue_width']['value'] = 1
        self.c['parameters']['core_issue_width']['value'] = 1
        self.c['engines']['exp'].update(throughput=1, latency=100)
        slow = op(0, outputs=[tile(0, 0, (1, 40))], name='tile.exp')
        _, rows = self.run_rows([slow, op(1, inputs=slow['outputs'], name='tile.add'), op(2), op(3)])
        self.assertLess(rows[2]['issue'], rows[1]['issue'])
        self.assertLess(rows[3]['issue'], rows[1]['issue'])
        self.assertEqual([rows[k]['issue'] for k in (0, 2, 3)], [3, 4, 5])

    def test_thread_windows_independent_view_counts_and_controls_bypass(self):
        self.c['parameters']['rob_depth']['value'] = 1
        source = op(0)
        view = op(1, inputs=source['outputs'], name='tile.transpose_view',
                  outputs=[dict(source['outputs'][0], object_id=100, shape=[4, 1])])
        report, rows = self.run_rows([source, view, op(2, phase=1), op(3, pe=1)],
                                    barriers=[0, 1], threads=[0, 1])
        self.assertEqual(rows[0]['admitted'], rows[3]['admitted'])
        self.assertEqual(rows[1]['admitted'], rows[0]['retire'])
        self.assertEqual(rows[2]['admitted'], rows[1]['retire'])
        self.assertNotIn('allocation_request', rows[1])
        self.assertLess(rows[2]['admitted'], rows['barrier:0:0:0']['complete'])
        windows = report['rob_windows']['core:0/rob']
        self.assertEqual(len(windows), 2)
        self.assertTrue(all(w['peak'] == 1 and w['occupancy'] == 0 for w in windows.values()))

    def test_snapshot_and_full_cycles_are_read_only_and_exact(self):
        fixture(self.path, [op(0), op(1), op(2)])
        trace = TraceAdapter(self.path, self.c)
        rob = ROB('core:0/rob', [trace.instructions[k] for k in (0, 1, 2)], [], rob_depth=2)
        rob.tick(0)
        self.assertEqual([m for _, _, m in rob.drain_outputs()], [Admitted(0), Admitted(1)])
        for key in (0, 1):
            rob.receive('issue', Issued(key))
        rob.tick(1)
        rob.receive('complete', Complete(1))
        rob.tick(2)
        before = pickle.dumps(rob.__dict__)
        snapshot = rob.snapshot()
        rob.is_idle()
        self.assertEqual(pickle.dumps(rob.__dict__), before)
        window = next(iter(snapshot['windows'].values()))
        self.assertEqual(window, dict(capacity=2, occupancy=2, peak=2, full_cycles=3,
                                     instructions=[0, 1], retirement_head=[0], next_admission=[2]))
        self.assertEqual(snapshot['entries']['1'], 'complete')
        rob.receive('complete', Complete(0))
        rob.tick(3)
        window = next(iter(rob.snapshot()['windows'].values()))
        self.assertEqual(window['instructions'], [2])
        self.assertEqual(window['occupancy'], 1)
        self.assertEqual(window['full_cycles'], 3)
        self.assertEqual(rob.drain_outputs(), [('core:0/scheduler', 'admission', Admitted(2))])

    def test_default_missing_and_invalid_depth(self):
        self.assertIsNone(default_config()['parameters']['rob_depth']['value'])
        fixture(self.path, [op(0), op(1), op(2)])
        explicit = simulate(self.path, self.c)
        legacy = copy.deepcopy(self.c)
        del legacy['parameters']['rob_depth']
        self.assertEqual(simulate(self.path, legacy), explicit)
        self.assertNotIn('rob_depth', legacy['parameters'])
        for value in (0, -1, 1.5, '2', True, False, [], {}):
            with self.subTest(value=value), self.assertRaisesRegex(ModelError, 'rob_depth'):
                config = copy.deepcopy(self.c)
                config['parameters']['rob_depth']['value'] = value
                validate_config(config)
        for value in (None, 1, 16):
            config = copy.deepcopy(self.c)
            config['parameters']['rob_depth']['value'] = value
            self.assertEqual(validate_config(config), config)
        for malformed in (None, [], {}, {'unit': 'instruction'}):
            with self.subTest(entry=malformed), self.assertRaises(ModelError):
                config = copy.deepcopy(self.c)
                config['parameters']['rob_depth'] = malformed
                validate_config(config)
        with self.assertRaises(ModelError):
            validate_config(dict(parameters=[]))

    def test_finite_tick_order_independent(self):
        self.c['parameters']['rob_depth']['value'] = 2
        self.c['message_log'] = True
        fixture(self.path, self.shared_rows() + [op(20, phase=1)], barriers=[0, 1], threads=[0, 1, 2, 3])
        trace = TraceAdapter(self.path, self.c)
        baseline = Simulator(trace, self.c).run()
        audit(baseline, trace)
        names = list(baseline['components'])
        for seed in range(4):
            order = names[::-1]
            if seed:
                random.Random(seed).shuffle(order)
            self.assertEqual(Simulator(trace, self.c, tick_order=order).run(), baseline)

    def test_deadlock_snapshot_shows_window_and_next_instruction(self):
        self.c['parameters']['rob_depth']['value'] = 1
        self.c['parameters']['local_capacity']['value'] = 1
        fixture(self.path, [op(0), op(1)])
        with self.assertRaises(ModelError) as caught:
            simulate(self.path, self.c)
        snapshot = json.loads(str(caught.exception).split(': ', 1)[1])
        window = next(iter(snapshot['components']['core:0/rob']['windows'].values()))
        self.assertEqual(window['instructions'], [0])
        self.assertEqual(window['retirement_head'], [0])
        self.assertEqual(window['next_admission'], [1])
        self.assertEqual(snapshot['components']['core:0/scheduler']['waiting']['1']['state'], 'waiting_admission')
