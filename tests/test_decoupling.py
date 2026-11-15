"""Public input compatibility and independently injected communication peers."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from api import simulate, simulate_core
from config import default_config
from runtime.communication import CommRequest, DataAccess
from runtime.runner import Runner
from examples.communication.fake_communit import FakeCommUnit
from examples.communication.streaming import ByteEndpoint
from test_model import ROOT, fixture, op


class DecouplingTests(unittest.TestCase):
    def setUp(self):
        (ROOT / "workspace").mkdir(exist_ok=True)

    def test_source_metadata_optional_and_execution_equal(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'workspace') as tmp:
            path = fixture(Path(tmp), [op(0)])
            baseline = simulate_core(path)
            config_path = path / 'capture_config.json'
            config_path.write_text(json.dumps(dict(capture_trace=False, mode='handwritten', entry='')))
            manifest = json.loads((path / 'manifest.json').read_text())
            manifest['config_file'] = config_path.name
            (path / 'manifest.json').write_text(json.dumps(manifest))
            result = simulate(path)
            for field in ('timeline', 'instruction_events', 'total_cycles', 'storage_events', 'resources'):
                self.assertEqual(baseline[field], result[field])

    def test_core_import_and_run_do_not_load_network(self):
        code = """
import sys
from api import simulate_core
r = simulate_core('examples/ooo/capture')
assert r['total_cycles'] == 23
assert not any(k.startswith(('models.noc', 'models.memory')) for k in sys.modules)
"""
        subprocess.run([sys.executable, '-S', '-c', code], cwd=ROOT, check=True, capture_output=True)

    @staticmethod
    def copy_script(sub, peer):
        reply = yield DataAccess(sub.token, 'read', 0, ((0,16),), sub.node)
        yield DataAccess(sub.token, 'write', 1, ((0,16),), sub.node, reply.detail)

    def test_fake_peer_uses_read_write_contract(self):
        from runtime.communication import ReceiveLayout
        request = CommRequest(opcode=1, route_bits=0, send_bytes=16, expected_receive_bytes=0)
        source = bytes(range(16))
        peer = FakeCommUnit(self.copy_script)
        client = ByteEndpoint(0, request, source, 0, receive=ReceiveLayout('OUT',16,0,((0,16),)))
        Runner({'communit':peer,client.name:client}).run()
        self.assertEqual(bytes(client.destination),source)
        self.assertTrue(client.published)
        self.assertEqual(client.completion.status,'success')

    def test_core_backend_injection_and_missing_backend(self):
        from config import wse_config
        from frontend.communication import allgather_group
        from frontend.capture import TraceAdapter
        from simulator import Simulator
        from test_model import tile
        from errors import ModelError
        with tempfile.TemporaryDirectory(dir=ROOT / 'workspace') as tmp:
            source = tile('src', 'produce', shared=True)
            target = tile('dst', None, shared=True)
            path = fixture(Path(tmp), [op('produce', outputs=[source]),
                                      op('consume', inputs=[target], name='tile.add')])
            config = wse_config()
            config.pop('hbm')
            plan = allgather_group({0: source}, {0: target}, {0: 'produce'}, 16)
            trace = TraceAdapter(path, config, plan)
            with self.assertRaisesRegex(ModelError, 'require comm_backend'):
                Simulator(trace, config, core_only=True)
            peer = FakeCommUnit(self.copy_script)
            report = Simulator(trace, config, core_only=True, comm_backend=peer).run()
            self.assertNotIn('noc', report)
            self.assertTrue(peer.is_idle())
            consumer = next(row for row in report['timeline'] if row['op_id'] == 'consume')
            self.assertEqual(consumer['state'], 'retired')
