"""Protocol tests drive components directly, without a running Simulator."""
import random
import pickle
import io
from contextlib import redirect_stdout
from collections import Counter

from api import ModelError
from simulator import Simulator
from runtime.clock import Clock
from cli import main
from runtime.component import Component
from models.ssnpu import Cube, TLSU, ROB, Scheduler, Sync, TileReg, Vector
from runtime.protocol import (
    Access, AccessDone, Admitted, Allocate, Allocated, Complete, Completed, Consumed, Dispatch, Issued,
)
from frontend.capture import TraceAdapter
from test_model import ModelTestCase, audit, fixture, op, tile


class Echo(Component):
    def tick(self, cycle):
        for _, message in self.inputs(cycle):
            self.send('sink', message)


class ProtocolTests(ModelTestCase):
    def trace(self, rows, barriers=(), threads=None):
        fixture(self.path, rows, barriers, threads)
        return TraceAdapter(self.path, self.c)

    def test_clock_next_cycle_and_named_ports(self):
        source, relay, sink = Component('source'), Echo('relay'), Component('sink')
        clock = Clock(dict(source=source, relay=relay, sink=sink), True)
        source.send('relay', Dispatch(7), 'dispatch')
        clock.collect(0)
        clock.deliver(0)
        self.assertFalse(relay.inbox)
        clock.deliver(1)
        self.assertEqual(list(relay.inbox), [('dispatch', Dispatch(7))])
        relay.tick(1)
        clock.collect(1)
        self.assertFalse(sink.inbox)
        clock.deliver(2)
        self.assertEqual(list(sink.inbox), [('commands', Dispatch(7))])
        self.assertEqual([(m['sent'], m['received']) for m in clock.messages], [(0, 1), (1, 2)])

    def test_rob_completion_bypasses_thread_retirement(self):
        trace = self.trace([op(0), op(1)])
        rob = ROB('rob', [trace.instructions[1], trace.instructions[0]], ['scheduler', 'sync'])
        rob.tick(0)
        self.assertEqual([m for _, _, m in rob.drain_outputs()], [Admitted(0), Admitted(1)])
        rob.receive('issue', Issued(1))
        rob.receive('issue', Issued(0))
        rob.tick(1)
        rob.receive('completion', Complete(1))
        rob.tick(2)
        self.assertEqual(rob.entries, {1: 'complete', 0: 'issued'})
        self.assertEqual(rob.drain_outputs(), [
            ('scheduler', 'completion', Completed(1)), ('sync', 'completion', Completed(1))])
        self.assertEqual(rob.observations[-1], (2, 1, dict(rob_complete=2, component='rob', state='rob_complete')))
        rob.receive('completion', Complete(0))
        rob.tick(3)
        self.assertEqual([k for _, k, fields in rob.observations if 'retire' in fields], [0, 1])
        rob.drain_outputs()
        self.assertTrue(rob.is_idle())

    def test_scheduler_requests_once_and_reserves_before_dispatch(self):
        trace = self.trace([op(0), op(1)])
        scheduler = Scheduler('core:0/scheduler', [trace.instructions[k] for k in (0, 1)], self.c)
        scheduler.tick(0)
        self.assertEqual(scheduler.drain_outputs(), [])
        for key in (0, 1):
            scheduler.receive('admission', Admitted(key))
        scheduler.tick(1)
        requests = scheduler.drain_outputs()
        self.assertEqual([m.instruction for _, _, m in requests], [0, 1])
        self.assertTrue(all(isinstance(m, Allocate) for _, _, m in requests))
        for cycle in range(2, 4):
            scheduler.tick(cycle)
            self.assertEqual(scheduler.drain_outputs(), [])
        for _, _, request in requests:
            scheduler.receive('allocation', Allocated(request.instruction, request.request))
        scheduler.tick(4)
        outputs = scheduler.drain_outputs()
        self.assertEqual(sum(isinstance(m, Dispatch) for _, _, m in outputs), 2)
        self.assertTrue(scheduler.is_idle())

    def test_tile_allocation_is_atomic_and_snapshot_is_read_only(self):
        self.c['parameters']['local_capacity']['value'] = 32
        rows = [op(0), op(1, outputs=[tile(1, 1), tile(2, 1)])]
        trace = self.trace(rows)
        reg = TileReg('core:0/tile', trace.instructions, trace.versions, self.c)
        reg.receive('allocate', Allocate(0, 'first'))
        reg.receive('allocate', Allocate(1, 'pair'))
        reg.tick(0)
        self.assertEqual([m.request for _, _, m in reg.drain_outputs()], ['first'])
        self.assertEqual(reg.active, trace.instructions[0].outputs)
        before = pickle.dumps(reg.__dict__)
        reg.snapshot()
        reg.is_idle()
        self.assertEqual(pickle.dumps(reg.__dict__), before)
        reg.receive('consume', Consumed(0))
        reg.tick(1)
        self.assertEqual([m.request for _, _, m in reg.drain_outputs()], ['pair'])
        self.assertEqual(reg.active, trace.instructions[1].outputs)
        self.assertEqual(reg.usage['core:0/pe:0/local'], 32)

    def test_shared_ports_and_complete_consumer_set(self):
        a = op(0, outputs=[tile(0, 0, shared=True)])
        b = op(1, inputs=a['outputs'], name='tile.add')
        c = op(2, inputs=a['outputs'], name='tile.add')
        trace = self.trace([a, b, c])
        key = next(iter(trace.instructions[0].outputs))
        # This fixture represents a last-use group without changing the trace adapter contract.
        trace.versions[key].retained = False
        reg = TileReg('core:0/tile', trace.instructions, trace.versions, self.c)
        reg.receive('allocation', Allocate(0, 'alloc'))
        reg.tick(0)
        reg.drain_outputs()
        reg.receive('access', Access(0, 'write', key, True, 16, 'float32'))
        reg.tick(1)
        self.assertEqual(reg.drain_outputs(), [])
        for cycle in range(2, 8):
            reg.tick(cycle)
        self.assertEqual([m for _, _, m in reg.drain_outputs()], [AccessDone(0, 'write')])
        reg.receive('consumption', Consumed(0))
        reg.receive('access', Access(1, 'read1', key, False, 16, 'float32'))
        reg.tick(8)
        reg.drain_outputs()
        for cycle in range(9, 15):
            reg.tick(cycle)
        self.assertEqual([m for _, _, m in reg.drain_outputs()], [AccessDone(1, 'read1')])
        reg.receive('consumption', Consumed(1))
        reg.tick(15)
        self.assertIn(key, reg.active)  # No active reader, but a future consumer remains.
        reg.receive('access', Access(2, 'read2', key, False, 16, 'float32'))
        reg.receive('access', Access(2, 'read3', key, False, 16, 'float32'))
        reg.tick(16)
        for cycle in range(17, 27):
            reg.tick(cycle)
        self.assertEqual([m.request for _, _, m in reg.drain_outputs()], ['read2', 'read3'])
        reads = [fields['stage'] for _, _, fields in reg.observations
                 if fields.get('stage', {}).get('engine') == 'shared_read']
        self.assertEqual([s['start'] for s in reads], [8, 16, 20])
        reg.receive('consumption', Consumed(2))
        reg.tick(27)
        self.assertNotIn(key, reg.active)
        self.assertEqual(sum(e['action'] == 'free' for e in reg.storage_events), 1)

    def test_vector_waiting_operand_does_not_block_ready_command(self):
        source = op(0)
        blocked = op(1, inputs=source['outputs'], name='tile.exp')
        ready = op(2)
        trace = self.trace([source, blocked, ready])
        vector = Vector('core:0/pe:0/vector', trace.instructions, self.c)
        vector.receive('dispatch', Dispatch(1))
        vector.receive('dispatch', Dispatch(2))
        vector.tick(0)
        requests = vector.drain_outputs()
        self.assertIsInstance(requests[0][2], Access)
        self.assertEqual(vector.commands[1].state, 'operands')
        self.assertEqual(vector.commands[2].state, 'computing')
        vector.tick(3)
        outputs = vector.drain_outputs()
        write = next(m for _, _, m in outputs if isinstance(m, Access) and m.write)
        self.assertEqual(write.instruction, 2)
        vector.receive('access', AccessDone(2, write.request))
        vector.tick(5)
        self.assertTrue(any(isinstance(m, Complete) and m.instruction == 2 for _, _, m in vector.drain_outputs()))
        self.assertIn(1, vector.commands)

    def test_shared_read_and_write_ports_progress_independently(self):
        a = op(0, outputs=[tile(0, 0, shared=True)])
        b = op(1, inputs=a['outputs'], outputs=[tile(1, 1, shared=True)], name='tile.add')
        c = op(2, outputs=[tile(2, 2, shared=True)])
        trace = self.trace([a, b, c])
        reg = TileReg('core:0/tile', trace.instructions, trace.versions, self.c)
        keys = [next(iter(trace.instructions[k].outputs)) for k in (0, 1, 2)]
        for k in (0, 1, 2):
            reg.receive('allocation', Allocate(k, f'alloc:{k}'))
        reg.tick(0)
        reg.drain_outputs()
        reg.receive('access', Access(0, 'seed', keys[0], True, 16, 'float32'))
        for cycle in range(1, 8):
            reg.tick(cycle)
        reg.drain_outputs()
        reg.receive('access', Access(1, 'read', keys[0], False, 16, 'float32'))
        reg.receive('access', Access(1, 'write1', keys[1], True, 16, 'float32'))
        reg.receive('access', Access(2, 'write2', keys[2], True, 16, 'float32'))
        for cycle in range(8, 19):
            reg.tick(cycle)
        self.assertEqual([m.request for _, _, m in reg.drain_outputs()], ['read', 'write1', 'write2'])
        stages = [f['stage'] for cycle, _, f in reg.observations if cycle >= 8 and 'stage' in f]
        self.assertEqual([(s['engine'], s['start']) for s in stages],
                         [('shared_read', 8), ('shared_write', 8), ('shared_write', 12)])

    def test_cube_operand_order_and_result_latency_overlap(self):
        a = op(0, outputs=[tile(0, 0, (2, 3))])
        b = op(1, outputs=[tile(1, 1, (3, 2))])
        rows = [a, b] + [op(k, inputs=a['outputs'] + b['outputs'], name='tile.matmul') for k in (2, 3)]
        self.c['engines']['cube'].update(throughput=12, latency=5)
        trace = self.trace(rows)
        cube = Cube('core:0/pe:0/cube', trace.instructions,
                    self.c)
        for k in (2, 3):
            cube.receive('dispatch', Dispatch(k))
        cube.tick(0)
        requests = [m for _, _, m in cube.drain_outputs()]
        self.assertEqual([m.version for m in requests], [('storage', 0, 1)] * 2)
        for request in requests:
            cube.receive('access', AccessDone(request.instruction, request.request))
        cube.tick(2)
        requests = [m for _, _, m in cube.drain_outputs()]
        self.assertEqual([m.version for m in requests], [('storage', 1, 1)] * 2)
        for request in requests:
            cube.receive('access', AccessDone(request.instruction, request.request))
        cube.tick(4)
        cube.tick(5)
        events = [f['stage'] for _, _, f in cube.observations if 'stage' in f]
        self.assertEqual([(e['start'], e['service_end'], e['complete']) for e in events], [(4, 5, 10), (5, 6, 11)])

    def test_lsu_store_completes_after_memory_result(self):
        a = op(0)
        tensor = dict(object_id=99, kind='tensor', shape=[1, 4], dtype='float32', memory_space='DDR')
        store = op(1, inputs=a['outputs'], outputs=[tensor], name='tile.store')
        store['memory_accesses'] = [dict(object_id=99, shape=[1, 4])]
        trace = self.trace([a, store])
        lsu = TLSU('core:0/tlsu', trace.instructions, self.c)
        lsu.receive('dispatch', Dispatch(1))
        lsu.tick(0)
        read = lsu.drain_outputs()[0][2]
        lsu.receive('access', AccessDone(1, read.request))
        lsu.tick(2)
        self.assertEqual(lsu.drain_outputs(), [])
        for cycle in range(3, 8):
            lsu.tick(cycle)
            self.assertEqual(lsu.drain_outputs(), [])
        lsu.tick(8)  # 16 bytes / 4 + 2 latency, starting at 2.
        self.assertEqual([type(m) for _, _, m in lsu.drain_outputs()], [Complete, Consumed])
        self.assertTrue(lsu.is_idle())

    def test_sync_empty_phases_and_tail_without_scheduler(self):
        trace = self.trace([], barriers=[0, 1], threads=[0, 1])
        sync = Sync('core:0/sync', list(trace.instructions.values()), self.c)
        sync.tick(0)
        self.assertEqual([type(m) for _, _, m in sync.drain_outputs()], [Issued])
        sync.tick(2)
        self.assertEqual([m for _, _, m in sync.drain_outputs()], [Complete('barrier:0:0:0')])
        sync.receive('completion', Completed('barrier:0:0:0'))
        sync.tick(4)
        self.assertEqual([m for _, _, m in sync.drain_outputs()], [Issued('barrier:0:0:1')])
        sync.tick(6)
        sync.drain_outputs()
        sync.receive('completion', Completed('barrier:0:0:1'))
        sync.tick(8)
        self.assertEqual([type(m) for _, _, m in sync.drain_outputs()], [Issued, Complete])
        self.assertTrue(sync.is_idle())

    def test_tick_order_and_messages_are_identical(self):
        rows = self.shared_rows()
        rows.append(op(20, inputs=rows[1]['outputs'], name='tile.exp', phase=1))
        trace = self.trace(rows, barriers=[0, 1], threads=[0, 1, 2, 3])
        self.c['message_log'] = True
        baseline = Simulator(trace, self.c).run()
        audit(baseline, trace)
        names = list(baseline['components'])
        orders = [names[::-1]]
        for seed in range(3):
            shuffled = names.copy()
            random.Random(seed).shuffle(shuffled)
            orders.append(shuffled)
        for order in orders:
            self.assertEqual(baseline, Simulator(trace, self.c, tick_order=order).run())
        self.assertTrue(all(m['received'] == m['sent'] + 1 for m in baseline['messages']))
        completions = Counter(m['instruction'] for m in baseline['messages'] if m['type'] == 'Complete')
        self.assertEqual(completions, Counter({k: 1 for k in trace.instructions}))
        self.assertTrue(all(c['inbox'] == c['outbox'] == 0 for c in baseline['components'].values()))

    def test_early_completion_wakes_consumer_before_retirement(self):
        slow = op(0, outputs=[tile(0, 0, (1, 40))], name='tile.exp')
        fast = op(1)
        use = op(2, inputs=fast['outputs'], name='tile.add')
        self.c['engines']['exp'].update(throughput=1, latency=100)
        report, t = self.run_rows([slow, fast, use])
        self.assertLess(t[2]['issue'], t[1]['retire'])
        self.assertLess(t[1]['complete'], t[0]['complete'])
        self.assertEqual(t[0]['retire'], t[1]['retire'])
        frees = [e for e in report['storage_events'] if e['action'] == 'free' and e['version'] == ['storage', 1, 1]]
        self.assertLess(frees[0]['cycle'], t[1]['retire'])
        self.assertLess(t[1]['rob_complete'], t[1]['retire'])

    def test_instruction_event_log_matches_timeline_and_cli(self):
        import json

        report, timeline = self.run_rows([op(0)])
        events = report['instruction_events']
        self.assertEqual([e['cycle'] for e in events], sorted(e['cycle'] for e in events))
        for key, row in timeline.items():
            for event in ('issue', 'complete', 'rob_complete', 'retire'):
                matching = [e for e in events if e['op_id'] == key and e['event'] == event]
                self.assertEqual(len(matching), 1)
                self.assertEqual(matching[0]['cycle'], row[event])
                self.assertEqual(matching[0]['execution_component'], row['execution_component'])
        self.assertEqual({e['event']: e['cycle'] for e in events if e['op_id'] == 0 and
                          e['event'] in ('issue', 'complete', 'rob_complete', 'retire')},
                         dict(issue=3, complete=9, rob_complete=10, retire=10))
        config, output, log = (self.path / name for name in ('config.json', 'report.json', 'events.jsonl'))
        config.write_text(json.dumps(self.c))
        with redirect_stdout(io.StringIO()):
            main([str(self.path), '--config', str(config), '--output', str(output), '--event-log', str(log)])
        self.assertEqual([json.loads(line) for line in log.read_text().splitlines()], events)
        self.assertEqual(json.loads(output.read_text())['instruction_events'], events)

    def test_deadlock_identifies_component_capacity_and_unfinished_entries(self):
        self.c['parameters']['local_capacity']['value'] = 1
        trace = self.trace([op(0)])
        with self.assertRaises(ModelError) as caught:
            Simulator(trace, self.c).run()
        error = str(caught.exception)
        for detail in ('core:0/tile', 'capacity:core:0/pe:0/local', 'allocations', 'consumers', 'core:0/rob'):
            self.assertIn(detail, error)
