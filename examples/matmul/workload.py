"""Executable handwritten SSNPU matmul. Requires a pypto build with SSNPU workload support."""

import argparse
from collections import Counter
from pathlib import Path

from pypto import DataType, ir
from pypto.workload import TensorSpec, TileInterpreter
from pypto.workload.ssnpu import Builder, reuse


def build_matmul():
    ss = Builder()
    with ss.launch("matmul", blocks=2) as fn:
        a = fn.param("A", ir.TensorType([512, 512], DataType.FP16))
        b = fn.param("B", ir.TensorType([512, 128], DataType.FP16))
        c = fn.param("C", ir.TensorType([512, 128], DataType.FP32))
        fn.return_type(c.type)
        bid, tid = ss.block_id(), ss.thread_id()
        row = ir.Add(
            ir.Mul(bid, ss.expr(256), DataType.INDEX, ir.Span.unknown()),
            ir.Mul(tid, ss.expr(32), DataType.INDEX, ir.Span.unknown()),
            DataType.INDEX,
            ir.Span.unknown(),
        )
        row1 = ir.Add(row, ss.expr(128), DataType.INDEX, ir.Span.unknown())
        shared = ss.shared_tile([128, 128], DataType.FP16)
        c0 = ss.full([32, 128], 0)
        c1 = ss.full([32, 128], 0)
        k = ss.var("k", ir.ScalarType(DataType.INDEX))
        with ss.for_loop(k, 0, 4, 1) as loop:
            acc0, acc1 = loop.iter_arg("acc0", c0), loop.iter_arg("acc1", c1)
            final0, final1 = loop.return_var("final0"), loop.return_var("final1")
            offset = ir.Mul(k, ss.expr(128), DataType.INDEX, ir.Span.unknown())
            bk = ss.load_into(shared, b, [offset, 0])
            a0 = ss.load_local(a, [row, offset], [32, 128])
            a1 = ss.load_local(a, [row1, offset], [32, 128])
            ss.barrier()
            out0 = ss.op("matmul_acc", acc0, a0, reuse(bk))
            out1 = ss.op("matmul_acc", acc1, a1, bk)
            ss.barrier()
            ss.yield_(out0, out1)
        ss.store(final0, c, [row, 0])
        ss.store(final1, c, [row1, 0])
        ss.return_stmt(c)
    return ir.Program([fn.get_result()], "shared_b", ir.Span.unknown()), {
        "A": TensorSpec((512, 512), DataType.FP16),
        "B": TensorSpec((512, 128), DataType.FP16),
        "C": TensorSpec((512, 128), DataType.FP32),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output-dir', type=Path, default=Path('workspace/captures/matmul'))
    args = parser.parse_args()
    program, inputs = build_matmul()
    result = TileInterpreter().run(program, "matmul", inputs)
    output = args.output_dir
    result.write_capture(output)
    (output / "ir.txt").write_text(str(program), encoding="utf-8")
    for block in range(2):
        counts = Counter(op["pto_op"] for op in result.trace if op["block_id"] == block)
        assert counts == {
            "pto.tload": 36,
            "pto.tmatmul.acc": 32,
            "pto.tstore": 8,
            "pto.tdup": 8,
        }
        print(f"block {block}: {dict(counts)}")


if __name__ == "__main__":
    main()
