"""Executable SSNPU dense one-head FA forward with explicit online state."""

import argparse
from collections import Counter
from pathlib import Path

from pypto import DataType, ir
from pypto.workload import TensorSpec, TileInterpreter
from pypto.workload.ssnpu import Builder


def build_fa():
    ss = Builder()
    with ss.launch("fa", blocks=2) as fn:
        q, k, v = [
            fn.param(name, ir.TensorType([256, 64], DataType.FP16))
            for name in ("Q", "K", "V")
        ]
        output = fn.param("O", ir.TensorType([256, 64], DataType.FP32))
        fn.return_type(output.type)
        bid, tid = ss.block_id(), ss.thread_id()
        span = ir.Span.unknown()
        row = ir.Add(
            ir.Mul(bid, ss.expr(128), DataType.INDEX, span),
            ir.Mul(tid, ss.expr(32), DataType.INDEX, span),
            DataType.INDEX,
            span,
        )
        ks = ss.shared_tile([128, 64], DataType.FP16)
        vs = ss.shared_tile([128, 64], DataType.FP16)
        qi = ss.load_local(q, [row, 0], [32, 64])
        m = ss.full([32, 1], -float("inf"))
        normalizer = ss.full([32, 1], 0)
        o = ss.full([32, 64], 0)
        scratch = ss.full([32, 128], 0)
        j = ss.var("j", ir.ScalarType(DataType.INDEX))
        with ss.for_loop(j, 0, 2, 1) as loop:
            mi, li, oi = [
                loop.iter_arg(name, value)
                for name, value in zip(("m", "l", "o"), (m, normalizer, o))
            ]
            mf, lf, of = [loop.return_var(name) for name in ("mf", "lf", "of")]
            offset = ir.Mul(j, ss.expr(128), DataType.INDEX, span)
            kj = ss.load_into(ks, k, [offset, 0])
            vj = ss.load_into(vs, v, [offset, 0])
            ss.barrier()
            kt = ss.op("transpose_view", kj)
            scores = ss.op("muls", ss.op("matmul", qi, kt), 0.125)
            mn = ss.op("maximum", mi, ss.op("row_max", scores, scratch))
            alpha = ss.op("exp", ss.op("sub", mi, mn))
            p = ss.op("exp", ss.op("row_expand_sub", scores, mn))
            ln = ss.op("add", ss.op("mul", alpha, li), ss.op("row_sum", p, scratch))
            pv = ss.op("matmul", ss.op("cast", p, dtype=DataType.FP16), vj)
            on = ss.op("add", ss.op("row_expand_mul", oi, alpha), pv)
            ss.barrier()
            ss.yield_(mn, ln, on)
        out = ss.op("row_expand_div", of, lf)
        ss.store(out, output, [row, 0])
        ss.return_stmt(output)
    return ir.Program([fn.get_result()], "shared_kv", span), {
        **{name: TensorSpec((256, 64), DataType.FP16) for name in ("Q", "K", "V")},
        "O": TensorSpec((256, 64), DataType.FP32),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output-dir', type=Path, default=Path('workspace/captures/fa'))
    args = parser.parse_args()
    program, inputs = build_fa()
    result = TileInterpreter().run(program, "fa", inputs)
    output = args.output_dir
    result.write_capture(output)
    (output / "ir.txt").write_text(str(program), encoding="utf-8")
    counts = Counter(op["ir_op"] for op in result.trace)
    assert counts["tile.matmul"] == 32
    assert counts["tile.row_expand_div"] == 8
    print(f"FA: {len(result.trace)} operations, {counts['tile.matmul']} QK/PV matmuls")


if __name__ == "__main__":
    main()
