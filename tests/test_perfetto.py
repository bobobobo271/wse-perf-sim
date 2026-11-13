"""Check trace time units, nesting and lossless instruction/milestone identities."""
import copy
import io
import json
import struct
from collections import Counter, defaultdict
from contextlib import redirect_stderr, redirect_stdout

from wse_perf_sim import ModelError
from wse_perf_sim.analysis.perfetto import main, to_perfetto, to_pftrace
from test_model import ModelTestCase, op, tile


class PerfettoTests(ModelTestCase):
    def report(self):
        slow = op(0, outputs=[tile(0, 0, (1, 40))], name='tile.exp')
        fast = op(1)
        self.c['engines']['exp'].update(throughput=1, latency=100)
        return self.run_rows([slow, fast, op(2, inputs=fast['outputs'], name='tile.add')])[0]

    def test_timestamp_units_and_milestones(self):
        report = self.report()
        original = copy.deepcopy(report['instruction_events'])
        trace = to_perfetto(report['instruction_events'], cycle_ns=2)
        self.assertEqual(report['instruction_events'], original)
        slices = [e for e in trace['traceEvents'] if e.get('cat') == 'ssnpu.instruction']
        self.assertEqual(len(slices), len(report['timeline']))
        rows = {r['op_id']: r for r in report['timeline']}
        for event in slices:
            row = rows[event['args']['op_id']]
            start = row.get('received', row['issue'])
            self.assertAlmostEqual(event['ts'], start * .002)
            self.assertAlmostEqual(event['dur'], (row['complete'] - start) * .002)
            self.assertEqual(event['args']['start_cycle'], start)
            self.assertEqual(event['args']['end_cycle'], row['complete'])
            self.assertEqual(event['args']['interval_start_event'], 'received' if 'received' in row else 'issue')
        instants = [e for e in trace['traceEvents'] if e['ph'] == 'i']
        self.assertEqual(Counter((e['args']['op_id'], e['name'], e['args']['cycle']) for e in instants),
                         Counter((e['op_id'], e['event'], e['cycle']) for e in original))
        self.assertTrue(all(e['s'] == 't' for e in instants))
        self.assertEqual(trace['otherData']['cycle_ns'], 2)

    def test_overlapping_instructions_and_retirement_do_not_cross(self):
        trace = to_perfetto(self.report()['instruction_events'])
        instructions = [e for e in trace['traceEvents'] if e.get('cat') == 'ssnpu.instruction']
        rows = {e['args']['op_id']: e for e in instructions}
        self.assertNotEqual(rows[0]['tid'], rows[1]['tid'])
        self.assertEqual(rows[1]['tid'], rows[2]['tid'])  # Retirement waiting does not hold the execution track.
        rob = {e['args']['op_id']: e for e in trace['traceEvents'] if e.get('cat') == 'ssnpu.rob'}
        self.assertNotEqual(rob[1]['tid'], rob[2]['tid'])
        names = {e['tid']: e['args']['name'] for e in trace['traceEvents'] if e['name'] == 'thread_name'}
        for event in trace['traceEvents']:
            if event['name'] in ('ROB notification', 'retirement wait', 'rob_complete', 'retire'):
                self.assertIn('/rob / completion/retirement', names[event['tid']])
        tracks = defaultdict(list)
        for e in trace['traceEvents']:
            if e['ph'] == 'X':
                tracks[e['pid'], e['tid']].append(e)
        for events in tracks.values():
            for a in events:
                for b in events:
                    start, end = a['args']['start_cycle'], a['args']['end_cycle']
                    other_start, other_end = b['args']['start_cycle'], b['args']['end_cycle']
                    self.assertFalse(start < other_start < end < other_end)
        self.assertTrue(any(e['name'] == 'retirement wait' and e['dur'] > 0 for e in trace['traceEvents']))

    def test_track_metadata_and_unordered_input(self):
        events = self.report()['instruction_events']
        # Add another Core while preserving the original instruction IDs' types.
        other = [e | {'op_id': f'other:{e["op_id"]}', 'core': 1,
                      'execution_component': e['execution_component'].replace('core:0/', 'core:1/')}
                 for e in events]
        trace = to_perfetto(list(reversed(events + other)))
        names = [e for e in trace['traceEvents'] if e['name'] == 'process_name']
        self.assertEqual({e['pid']: e['args']['name'] for e in names}, {1: 'Core 0', 2: 'Core 1'})
        tracks = {(e['pid'], e['tid']) for e in trace['traceEvents'] if e['name'] == 'thread_name'}
        self.assertTrue(all((e['pid'], e['tid']) in tracks for e in trace['traceEvents'] if e['ph'] != 'M'))
        self.assertEqual(trace, to_perfetto(events + other))

    def test_empty_trace(self):
        self.assertEqual(to_perfetto([])['traceEvents'], [])

    def test_admission_tracks_and_legacy_logs(self):
        events = self.report()['instruction_events']
        trace = to_perfetto(events)
        names = {e['tid']: e['args']['name'] for e in trace['traceEvents'] if e['name'] == 'thread_name'}
        for event in trace['traceEvents']:
            if event['name'] == 'admitted':
                self.assertIn('/rob /', names[event['tid']])
            elif event['name'] == 'admission_received':
                self.assertIn('/scheduler /', names[event['tid']])
        legacy = [e for e in events if e['event'] not in ('admitted', 'admission_received')]
        self.assertEqual(sum(e['ph'] == 'i' for e in to_perfetto(legacy)['traceEvents']), len(legacy))
        self.assertTrue(to_pftrace(legacy))
        invalid = [e | {'cycle': 10000} if e['event'] == 'admitted' else e for e in events]
        with self.assertRaisesRegex(ModelError, 'lifecycle order'):
            to_perfetto(invalid)

    def test_invalid_scale_or_incomplete_logs(self):
        events = self.report()['instruction_events']
        for scale in (0, -1, float('inf'), float('nan'), True):
            with self.assertRaisesRegex(ModelError, 'cycle_ns'):
                to_perfetto(events, scale)
        for missing in ('rob_complete', 'compute_end'):
            with self.assertRaisesRegex(ModelError, 'Missing|Incomplete'):
                to_perfetto([e for e in events if not (e['op_id'] == 0 and e['event'] == missing)])
        with self.assertRaisesRegex(ModelError, 'Duplicate'):
            to_perfetto(events + [events[0]])
        invalid = [e | {'cycle': 0} if e['op_id'] == 0 and e['event'] == 'rob_complete' else e for e in events]
        with self.assertRaisesRegex(ModelError, 'lifecycle order'):
            to_perfetto(invalid)
        with self.assertRaisesRegex(ModelError, 'Conflicting metadata'):
            to_perfetto(events + [events[0] | {'core': 1}])

    def test_cli_round_trip_and_errors(self):
        events = self.report()['instruction_events']
        source, target = self.path/'events.jsonl', self.path/'trace.json'
        source.write_text('\n'.join(json.dumps(e) for e in events) + '\n\n')
        with redirect_stdout(io.StringIO()):
            main([str(source), '--output', str(target), '--cycle-ns', '3'])
        self.assertEqual(json.loads(target.read_text()), to_perfetto(events, 3))
        source.write_text('{invalid}\n')
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            main([str(source), '--output', str(target)])
        self.assertEqual(caught.exception.code, 2)

    def cube_report(self):
        a = op(0, outputs=[tile(0, 0, (2, 3))])
        b = op(1, outputs=[tile(1, 1, (3, 2))])
        cubes = [op(k, inputs=a['outputs']+b['outputs'], outputs=[tile(k, k, (2, 2))], name='tile.matmul')
                 for k in (2, 3)]
        return self.run_rows([a, b, *cubes])[0]

    def test_native_track_hierarchy(self):
        report = self.cube_report()
        descriptors, _, _ = decode_trace(to_pftrace(report['instruction_events']))

        def path(uuid):
            seen, names = set(), []
            while uuid:
                self.assertNotIn(uuid, seen)
                seen.add(uuid)
                descriptor = descriptors[uuid]
                names.append(descriptor[2][0])
                uuid = descriptor.get(5, [0])[0]
            return tuple(reversed(names))

        paths = {path(uuid) for uuid in descriptors}
        cube = ('Core 0', 'PE 0', 'Cube', 'In-flight commands')
        self.assertIn(cube + ('active 0',), paths)
        self.assertIn(cube + ('active 1',), paths)
        self.assertIn(('Core 0', 'ROB', 'completion/retirement 0'), paths)
        self.assertIn(('Core 0', 'Scheduler', 'Milestones'), paths)
        for descriptor in descriptors.values():
            self.assertNotIn(3, descriptor)  # process/thread descriptors flatten native nesting.
            self.assertNotIn(4, descriptor)
            self.assertEqual(descriptor[11], [3])  # Explicit child ordering.

    def test_native_slices_and_instants_preserve_cycles(self):
        report = self.cube_report()
        events = report['instruction_events']
        original = copy.deepcopy(events)
        for scale in (2.5, .25):
            descriptors, slices, instants = decode_trace(to_pftrace(events, scale))
            instructions = [s for s in slices if s['cat'] == 'ssnpu.instruction']
            self.assertEqual(len(instructions), len(report['timeline']))
            rows = {r['op_id']: r for r in report['timeline']}
            for event in instructions:
                row = rows[event['args']['op_id']]
                start = row.get('received', row['issue'])
                self.assertEqual((event['start'], event['end']), (round(start*scale), round(row['complete']*scale)))
                self.assertEqual(event['args']['start_cycle'], start)
                self.assertEqual(event['args']['end_cycle'], row['complete'])
                self.assertIn(event['track'], descriptors)
            self.assertEqual(Counter((e['name'], e['args']['op_id'], e['args']['cycle']) for e in instants),
                             Counter((e['event'], e['op_id'], e['cycle']) for e in events))
        self.assertEqual(events, original)
        self.assertEqual(to_pftrace(events), to_pftrace(list(reversed(events))))

    def test_native_cli_and_empty_trace(self):
        events = self.cube_report()['instruction_events']
        source, target = self.path/'events.jsonl', self.path/'trace.pftrace'
        source.write_text(''.join(json.dumps(e)+'\n' for e in events))
        with redirect_stdout(io.StringIO()):
            main([str(source), '--output', str(target)])
        self.assertEqual(target.read_bytes(), to_pftrace(events))
        self.assertEqual(decode_trace(to_pftrace([])), ({}, [], []))


def protobuf_fields(data):
    """Independent protobuf wire reader for the official fields used by the exporter."""
    fields, offset = defaultdict(list), 0

    def varint():
        nonlocal offset
        value, shift = 0, 0
        while True:
            byte = data[offset]
            offset += 1
            value |= (byte & 127) << shift
            if byte < 128:
                return value
            shift += 7
            assert shift < 70, 'Invalid varint'

    while offset < len(data):
        tag = varint()
        number, wire = tag >> 3, tag & 7
        assert number
        if wire == 0:
            value = varint()
        elif wire == 2:
            size = varint()
            value = data[offset:offset+size]
            assert len(value) == size
            offset += size
        elif wire == 1:
            value = struct.unpack_from('<d', data, offset)[0]
            offset += 8
        else:
            raise AssertionError(f'Unexpected protobuf wire type {wire}')
        fields[number].append(value)
    assert offset == len(data)
    return dict(fields)


def decode_trace(data):
    """Decode descriptors and execute TrackEvent BEGIN/END stacks, not JSON X events."""
    descriptors, slices, instants, stacks = {}, [], [], defaultdict(list)
    packets = [protobuf_fields(p) for p in protobuf_fields(data)[1]]
    assert packets[0][10] == [1] and packets[0][13] == [1]
    previous_ts = 0
    for packet in packets:
        assert packet[10] == [1]
        if 60 in packet:
            descriptor = protobuf_fields(packet[60][0])
            uuid = descriptor[1][0]
            assert uuid not in descriptors
            assert not descriptor.get(5) or descriptor[5][0] in descriptors
            descriptor[2][0] = descriptor[2][0].decode()
            descriptors[uuid] = descriptor
        elif 11 in packet:
            assert packet[13] == [2]
            ts = packet[8][0]
            assert ts >= previous_ts
            previous_ts = ts
            event = protobuf_fields(packet[11][0])
            track, kind = event[11][0], event[9][0]
            assert track in descriptors
            args = {}
            for raw in event.get(4, []):
                annotation = protobuf_fields(raw)
                name = annotation[10][0].decode()
                field = next(k for k in (2, 3, 4, 5, 6) if k in annotation)
                value = annotation[field][0]
                if field == 2:
                    value = bool(value)
                elif field == 4 and value >= 1 << 63:
                    value -= 1 << 64
                elif field == 6:
                    value = value.decode()
                args[name] = value
            if kind == 2:
                assert stacks[track], 'END without BEGIN'
                begin = stacks[track].pop()
                assert ts >= begin['start']
                slices.append(begin | {'end': ts})
            else:
                entry = dict(track=track, name=event[23][0].decode(), cat=event[22][0].decode(), args=args)
                if kind == 1:
                    stacks[track].append(entry | {'start': ts})
                else:
                    assert kind == 3
                    instants.append(entry | {'ts': ts})
    assert all(not stack for stack in stacks.values()), 'Unclosed native slice'
    return descriptors, slices, instants
