"""Whole-core budget checks: same work/dtype/clock, vary PE count only."""
import unittest
from types import SimpleNamespace

from ssnpu_model.config import default_config, validate_config, wse_config
from ssnpu_model.components.cube import Cube
from ssnpu_model.components.vector import Vector


class CoreBudgetTests(unittest.TestCase):
    def test_cube_partition_preserves_aggregate_rate(self):
        # Each active PE executes 32*64*32 MACs. Four partitions must not
        # provide four times the documented 4096 MAC/cycle/core target.
        instruction = SimpleNamespace(raw={'inputs': [
            {'shape': [32, 64], 'dtype': 'float16'},
            {'shape': [64, 32], 'dtype': 'float16'}]})
        for count in (1, 2, 4, 8):
            config = wse_config()
            config['parameters']['pe_count']['value'] = count
            validate_config(config)
            stages = [Cube(f'pe:{pe}', {}, config).start(instruction, 0, 0) for pe in range(count)]
            elapsed = max(stage['service_end'] for stage in stages)
            self.assertEqual(sum(stage['work'] for stage in stages) / elapsed, 4096)

    def test_vector_bytes_and_operation_caps(self):
        # Same 4096 elements. FP32 consumes twice FP16's vector byte budget;
        # exp is also limited by its explicitly synthetic aggregate rate.
        for count in (1, 4):
            config = wse_config()
            config['parameters']['pe_count']['value'] = count
            for dtype, width in (('float16', 2), ('float32', 4)):
                for op in ('adds', 'exp', 'cast'):
                    obj = dict(shape=[1, 4096], dtype=dtype)
                    instruction = SimpleNamespace(raw=dict(ir_op='tile.' + op, inputs=[obj], outputs=[obj]))
                    stage = Vector('vector', {}, config).start(instruction, 0, 0)
                    self.assertLessEqual(stage['work'] * width * count / stage['service_end'], 256)
                    if op == 'exp':
                        self.assertLessEqual(stage['work'] * count / stage['service_end'], 64)

    def test_legacy_profile_stays_per_pe(self):
        config = default_config()
        validate_config(config)
        self.assertEqual(config['engines']['cube']['scope'], 'PE')
        self.assertEqual(config['engines']['vector']['unit'], 'element/cycle')


if __name__ == '__main__':
    unittest.main()
