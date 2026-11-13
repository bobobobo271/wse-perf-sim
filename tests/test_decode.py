"""Exercise PTO replay and HBM timing independently of a kernel generator."""
import copy
import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory

from api import ModelError, simulate
from config import default_config, wse_config
from examples.decode.run import main
from test_model import ROOT, fixture, op, simulate as simulate_fixture


class DecodeReplayTests(unittest.TestCase):
    def setUp(self):
        workspace = ROOT / 'workspace'
        workspace.mkdir(exist_ok=True)
        tmp = TemporaryDirectory(dir=workspace)
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name)

    def test_replay_archived_pypto_fa_without_generating_a_capture(self):
        capture = ROOT / 'examples/fa/capture'
        config = default_config()
        config['wse'] = wse_config()['wse']
        config_path = self.path / 'config.json'
        config_path.write_text(json.dumps(config))
        output = self.path / 'replay'
        with redirect_stdout(io.StringIO()):
            main([str(capture), '--config', str(config_path), '--no-plots', '--output-dir', str(output)])
        report = json.loads((output / 'report.json').read_text())
        self.assertEqual(report['instruction_count'], 320)
        self.assertEqual(report['total_cycles'], 2183)
        self.assertEqual(report['source_sha256'], simulate(capture)['source_sha256'])
        self.assertTrue((output / 'pipeline.pftrace').stat().st_size)
        self.assertFalse((output / 'capture').exists())

    def test_public_replay_rejects_synthetic_records(self):
        path = fixture(self.path / 'synthetic', [op(0)])
        with self.assertRaisesRegex(ModelError, 'Expected a PyPTO workload capture'):
            simulate(path)

    def test_hbm_arbitration_and_bandwidth(self):
        # Two cores each copy 128 FP16 elements from HBM to local SRAM.
        # The only variable between simulations is the configured chip bandwidth.
        rows = []
        for core in range(2):
            source = dict(object_id=f'src{core}', kind='tensor', shape=[1, 128],
                          dtype='float16', memory_space='HBM')
            target = dict(source, object_id=f'dst{core}', memory_space='SRAM')
            row = op(core, pe=core, inputs=[source], outputs=[target], name='memory.copy')
            row['memory_accesses'] = [dict(object_id=obj['object_id'], shape=obj['shape'])
                                      for obj in (source, target)]
            rows.append(row)
        capture = fixture(self.path / 'hbm', rows)
        manifest_path = capture / 'manifest.json'
        manifest = json.loads(manifest_path.read_text())
        for instance in manifest['instances']:
            instance['mapping'] = dict(core_id=instance['thread_id'], pe_id=0)
        manifest_path.write_text(json.dumps(manifest))
        config = wse_config()
        config['hbm'] = dict(bandwidth_bytes_per_second=256e9, startup_ns=0)
        fast = simulate_fixture(capture, config)
        slow_config = copy.deepcopy(config)
        slow_config['hbm']['bandwidth_bytes_per_second'] = 1e9
        slow = simulate_fixture(capture, slow_config)
        for report in (fast, slow):
            first, second = report['hbm_events']
            self.assertLessEqual(first['end_cycle'], second['start_cycle'])
            self.assertEqual(report['resources']['chip/hbm']['bytes'], 512)
            for row in report['timeline']:
                if row['ir_op'] == 'memory.copy':
                    hbm, sram = row['stages']
                    self.assertLess(hbm['service_end'], sram['start'])
        self.assertGreater(slow['total_cycles'], fast['total_cycles'])
