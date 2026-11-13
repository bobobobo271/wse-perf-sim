"""Reproduce the seven fixed SSM / extended Tile IR experiments.

Run with a Python containing the current pypto extension and torch; see docs.
No simulator implementation or existing capture is modified.
"""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import csv
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import traceback

ROOT = Path(__file__).resolve().parent
CASES = [
    ("cube", "tmatmul_fp16_16x32x32", "matmul", 16, 32, 32),
    ("cube", "tmatmul_fp16_32x64x64", "matmul", 32, 64, 64),
    ("vector", "tadd_fp32_16x16", "add", 16, 16, None),
    ("vector", "texp_fp32_16x16", "exp", 16, 16, None),
    ("vector", "trowsum_fp32_16x16", "row_sum", 16, 16, None),
    ("memory", "tload_fp32_16x16", "copy", 16, 16, None),
    ("memory", "tstore_fp32_16x16", "copy", 16, 16, None),
]


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(command, directory, stem, timeout=120):
    """Preserve stdout, stderr, exact argv, cwd and timeout/exit status."""
    started = time.monotonic()
    record = dict(command=list(map(str, command)), cwd=str(directory), timeout_s=timeout)
    with (directory / (stem + ".log")).open("wb") as log:
        try:
            proc = subprocess.run(record["command"], cwd=directory, stdout=log,
                                  stderr=subprocess.STDOUT, timeout=timeout)
            record.update(exit_code=proc.returncode, timed_out=False)
        except subprocess.TimeoutExpired:
            record.update(exit_code=None, timed_out=True)
        except OSError as exc:
            log.write(str(exc).encode())
            record.update(exit_code=None, timed_out=False, error=str(exc))
    record["wall_seconds"] = time.monotonic() - started
    write_json(directory / (stem + ".command.json"), record)
    return record, (directory / (stem + ".log")).read_text(errors="replace")


def generate(case, directory):
    """Handwritten extended Tile IR; capture comes only from TileInterpreter."""
    from pypto import DataType, ir
    from pypto.workload import TensorSpec, TileInterpreter
    from pypto.workload.ssnpu import Builder

    _, name, operation, m, n, k = case
    dtype = DataType.FP16 if k else DataType.FP32
    shapes = {"A": [m, k or n], "C": [m, 1 if operation == "row_sum" else n]}
    if operation in {"matmul", "add"}:
        shapes["B"] = [k or m, n]
    ss = Builder()
    with ss.launch(name, blocks=1, threads=1) as fn:
        params = {key: fn.param(key, ir.TensorType(shape, DataType.FP32 if key == "C" else dtype))
                  for key, shape in shapes.items()}
        fn.return_type(params["C"].type)
        a = ss.load_local(params["A"], [0, 0], shapes["A"])
        if operation in {"matmul", "add"}:
            b = ss.load_local(params["B"], [0, 0], shapes["B"])
            out = ss.op(operation, a, b)
        elif operation == "row_sum":
            # Tile IR requires explicit scratch; declaration has no tile instruction.
            scratch = ss._call("tile.create", [[m, n]], ss.tile_type([m, n], dtype),
                               kwargs={"dtype": dtype}, storage="LocalTileReg")
            out = ss.op("row_sum", a, scratch)
        elif operation == "exp":
            out = ss.op("exp", a)
        else:
            out = a
        ss.store(out, params["C"], [0, 0])
        ss.return_stmt(params["C"])
    program = ir.Program([fn.get_result()], name, ir.Span.unknown())
    specs = {key: TensorSpec(tuple(shape), DataType.FP32 if key == "C" else dtype)
             for key, shape in shapes.items()}
    result = TileInterpreter().run(program, name, specs)
    result.write_capture(directory / "capture")
    (directory / "ir.txt").write_text(str(program))
    counts = Counter(op["pto_op"] for op in result.trace)
    write_json(directory / "workload.json", dict(shapes=shapes, counts=counts, trace=result.trace))
    return counts


def python_runs(directory, config):
    from ssnpu_model import simulate
    from ssnpu_model.perfetto import to_pftrace
    results = []
    for repeat in (1, 2):
        try:
            report = simulate(directory / "capture", config)
        except Exception:
            error = traceback.format_exc()
            (directory / f"python-{repeat}.error.log").write_text(error)
            results.append(dict(total=None, error=error))
            continue
        write_json(directory / f"python-{repeat}.json", report)
        events = report["instruction_events"]
        (directory / f"python-{repeat}.events.jsonl").write_text(
            "".join(json.dumps(e) + "\n" for e in events))
        (directory / f"python-{repeat}.pftrace").write_bytes(to_pftrace(events))
        rows = [r for r in report["timeline"] if r["ir_op"].startswith("tile.")]
        first = min(r["issue"] for r in rows)
        last = max(r["complete"] for r in rows)
        results.append(dict(total=report["total_cycles"], first_issue=first, last_complete=last,
                            tile_interval=last-first, last_retire=max(r["retire"] for r in rows),
                            drain=report["total_cycles"]-last,
                            instruction_count=report["instruction_count"]))
    return results


def provenance(repo, destination):
    destination.mkdir(parents=True, exist_ok=True)
    for stem, argv in [("head", ["rev-parse", "HEAD"]), ("status", ["status", "--porcelain=v1"]),
                       ("diff", ["diff", "--binary"]), ("cached", ["diff", "--cached", "--binary"])]:
        proc = subprocess.run(["git", "-C", str(repo), *argv], capture_output=True)
        (destination / (stem + ".txt")).write_bytes(proc.stdout + proc.stderr)


def native_cycles(log):
    values = re.findall(r"^Total Cycles\.+:\s*(\d+)", log, re.M)
    return int(values[-1]) if values else None


def swim_events(path):
    """Stream the large pretty-printed traceEvents array using the stdlib decoder."""
    decoder = json.JSONDecoder()
    with path.open() as handle:
        buffer = ""
        while "[" not in buffer:
            chunk = handle.read(65536)
            if not chunk:
                raise ValueError("traceEvents array missing")
            buffer += chunk
        buffer = buffer.split("[", 1)[1]
        while True:
            buffer = buffer.lstrip(" \t\r\n,")
            if buffer.startswith("]"):
                return
            try:
                event, end = decoder.raw_decode(buffer)
            except json.JSONDecodeError:
                chunk = handle.read(65536)
                if not chunk:
                    raise ValueError("truncated traceEvents array")
                buffer += chunk
                continue
            yield event
            buffer = buffer[end:]


def audit(directory, row):
    """Check executed descriptors; do not turn unlike clocks into accuracy ratios."""
    operation = row["operation"]
    target = {"matmul": "TMATMUL", "add": "TADD", "exp": "TEXP", "row_sum": "TROWSUM"}.get(operation)
    expected = {"TLOAD": 2 if operation in {"matmul", "add"} else 1, "TSTORE": 1}
    if target:
        expected[target] = 1
    workload_path = directory / "workload.json"
    if workload_path.exists():
        workload = json.loads(workload_path.read_text())
        memory = []
        dependencies = {}
        for op in workload["trace"]:
            dependencies[str(op["op_id"])] = op["dependencies"]
            for access in op.get("memory_accesses", []):
                obj = next(o for o in op["inputs"] + op["outputs"] if o["object_id"] == access["object_id"])
                elements = 1
                for dim in access["shape"]:
                    elements *= dim
                memory.append(dict(access, dtype=obj["dtype"], bytes=elements*(2 if obj["dtype"] == "float16" else 4)))
        row["capture_audit"] = dict(memory_ranges=memory, dependencies=dependencies,
                                    read_bytes=sum(a["bytes"] for a in memory if a["mode"] == "read"),
                                    write_bytes=sum(a["bytes"] for a in memory if a["mode"] == "write"))
        row["capture_audit"]["byte_counts_match"] = all(row["capture_audit"][k] == row["work"][k]
                                                        for k in ("read_bytes", "write_bytes"))
    audits = []
    for repeat, run_record in enumerate(row.get("ssm_runs", []), 1):
        path = directory / f"gfrun-{repeat}.trace"
        trace = path.read_text(errors="replace") if path.exists() else ""
        # Only retired functional blocks, not micro-instruction spellings or speculative timing decode.
        blocks = re.findall(r"^B\d+\s+T(\d+) BPC (0x[0-9a-f]+).*?\[(TLOAD|TSTORE|TMATMUL|TADD|TEXP|TROWSUM)\]", trace, re.M)
        counts = dict(Counter(b[2] for b in blocks))
        descriptor_lines = []
        collecting = False
        for line in trace.splitlines():
            if re.match(r"^B\d+\s+T\d+ BPC", line):
                collecting = bool(re.search(r"\[(?:TLOAD|TSTORE|TMATMUL|TADD|TEXP|TROWSUM)\]", line))
            if collecting:
                descriptor_lines.append(line)
        (directory / f"gfrun-{repeat}.tile-descriptors.txt").write_text("\n".join(descriptor_lines) + "\n")
        state_path = directory / f"gfrun-{repeat}.state.json"
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
        functional_ok = (run_record["functional"]["success"] and counts == expected and
                         len(state.get("threads", [])) == 1 and state.get("core_sim_end") is True)
        swim_path = directory / f"gfsim-{repeat}.swimlane.json"
        slices = []
        swim_complete = False
        config_tables = []
        native_log = (directory / f"gfsim-{repeat}.log").read_text(errors="replace")
        for line in native_log.splitlines():
            if "Config Name" in line or re.search(r"\|.*\|", line):
                config_tables.append(line)
        (directory / f"gfsim-{repeat}.effective-config.txt").write_text("\n".join(config_tables) + "\n")
        if swim_path.exists():
            try:
                processes = []
                # Main block lifetimes only: VEC compute subtracks carry 'uops='.
                for e in swim_events(swim_path):
                    if e.get("name") == "process_name":
                        processes.append(e)
                    hint = e.get("args", {}).get("block-hint", "")
                    if (e.get("ph") == "X" and re.search(r"\bBPC 0x[0-9a-f]+", hint)
                            and "uops=" not in hint and re.search(r"\b(?:TLOAD|TSTORE|TMATMUL|TADD|TEXP|TROWSUM),", hint)):
                        slices.append(e)
                swim_complete = True
                write_json(directory / f"gfsim-{repeat}.topology.json", processes)
            except (ValueError, KeyError) as exc:
                row["notes"].append(f"swimlane parse failed: {exc}")
        slice_counts = dict(Counter(re.search(r"\b(TLOAD|TSTORE|TMATMUL|TADD|TEXP|TROWSUM),", e["args"]["block-hint"])[1]
                                    for e in slices))
        write_json(directory / f"gfsim-{repeat}.tile-events.json", slices)
        timing_ok = (run_record["timing"]["exit_code"] == 0 and run_record["timing"]["cycles"] is not None
                     and not run_record["timing"]["timed_out"] and swim_complete and slice_counts == expected)
        audits.append(dict(functional_ok=functional_ok, executed_counts=counts, expected_counts=expected,
                           executed_blocks=blocks, timing_ok=timing_ok, completed_slice_counts=slice_counts,
                           swim_complete=swim_complete,
                           candidate_residency_interval=(max(e["ts"]+e["dur"] for e in slices)-min(e["ts"] for e in slices)) if slices else None))
    row["audit"] = audits
    row["workload_status"] = "executed_counts_match; see decoded descriptors and capture_audit" if audits and all(a["functional_ok"] for a in audits) else "functional_or_count_failure"
    timings = [r["timing"] for r in row.get("ssm_runs", [])]
    if any(t["timed_out"] for t in timings):
        row["ssm_status"] = "timeout"
    elif audits and all(a["timing_ok"] and a["functional_ok"] for a in audits):
        row["ssm_status"] = "pass" if len(set(row["ssm_cycles"])) == 1 else "unstable"
    else:
        row["ssm_status"] = "incomplete_or_failed"
    row["timing"] = "native totals; tile residency candidate only (TLSU LIQ allocation != Python issue)"
    row["notes"].append("暂不可直接比较：SSM TLSU slices begin after dispatch; ratio remains null. Vector prebuilt ELF may omit RES_CHECK; gfrun exit is functional execution, not independent numerical validation.")
    if operation == "copy":
        row["notes"].append("load/store names both execute the same load→store program")
    row["notes"] = list(dict.fromkeys(row["notes"]))
    write_json(directory / "result.json", row)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ssm-root", type=Path, required=True)
    parser.add_argument("--bench-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--audit-only", action="store_true", help="Re-audit existing logs without rerunning simulators")
    parser.add_argument("--case", action="append", choices=[c[1] for c in CASES], help="Run selected fixed cases")
    parser.add_argument("--counter-sample-interval", type=int, default=8, help="SSM observation-only counter sampling")
    parser.add_argument("--merge-run", type=Path, action="append", help="Merge existing summaries; later runs replace cases")
    parser.add_argument("--timeout", type=float, default=120, help="Wall seconds per SSM process; 0 disables the limit")
    parser.add_argument("--repeat-workers", type=int, choices=(1, 2), default=1,
                        help="Independent SSM repeat processes to run concurrently")
    args = parser.parse_args()
    if args.timeout < 0:
        parser.error("timeout must be nonnegative")
    timeout = args.timeout or None
    output = args.output.resolve()
    if args.merge_run:
        output.mkdir(parents=True, exist_ok=False)
        merged = {}
        for run_path in args.merge_run:
            for row in json.loads((run_path / "summary.json").read_text()):
                row["source_run"] = str(run_path.resolve())
                merged[row["case"]] = row
        summarize(output, [merged[c[1]] for c in CASES if c[1] in merged])
        write_json(output / "merge-inputs.json", list(map(str, args.merge_run)))
        return
    if args.audit_only:
        shutil.copy2(__file__, output / "audit-runner.py")
        results = json.loads((output / "summary.json").read_text())
        for row in results:
            audit(output / row["case"], row)
        summarize(output, results)
        return
    output.mkdir(parents=True, exist_ok=False)
    ssm, bench = args.ssm_root.resolve(), args.bench_root.resolve()
    write_json(output / "invocation.json", dict(argv=sys.argv, executable=sys.executable,
               python=sys.version, pythonpath=os.environ.get("PYTHONPATH"), cwd=os.getcwd()))
    for key, repo in [("model", ROOT), ("pypto", ROOT.parent / "pypto"), ("ssm", ssm), ("bench", bench)]:
        provenance(repo, output / "provenance" / key)
    shutil.copy2(__file__, output / "runner.py")
    shutil.copytree(ssm / "configs", output / "ssm-config-source")
    config = json.loads((ROOT / "config/logic-validation.json").read_text())
    config["parameters"]["pe_count"]["value"] = 1
    config["parameters"]["rob_depth"]["value"] = None
    write_json(output / "python-config.json", config)
    # Explicit topology and identical reference-core thread count prevent hidden SMT4 work.
    overrides = ["core.threadCount=1", "core.stdPeCount=1", "core.streamCount=1",
                 "core.cube_core_num=1", "core.cube_thread_num=1", "core.vec_core_num=1",
                 "softcore.multiThreadNum=1"]
    if args.counter_sample_interval < 1:
        parser.error("counter sample interval must be positive")
    overrides.append(f"dfx.swimCounterSampleInterval={args.counter_sample_interval}")
    write_json(output / "ssm-overrides.json", overrides)
    hashes = {b: sha(ssm / "bin" / b) for b in ("gfsim", "gfrun")}
    hashes["python"] = dict(path=sys.executable, sha256=sha(Path(sys.executable)))
    import pypto.pypto_core as pypto_core
    extension = Path(pypto_core.__file__)
    hashes["pypto_core"] = dict(path=str(extension), sha256=sha(extension))
    write_json(output / "binary-hashes.json", hashes)
    results = []
    for case in CASES:
        if args.case and case[1] not in args.case:
            continue
        family, name, op, m, n, k = case
        directory = output / name
        directory.mkdir()
        print(name, flush=True)
        row = dict(case=name, operation=op, ssm_status="not_run", python_status="not_run",
                   workload_status="pending", ratio=None, timing="native; tile interval unverified",
                   configuration_difference="SSM frontend/cache/CELL/finite ROB vs synthetic tile model/unlimited ROB",
                   notes=[])
        results.append(row)
        elf = ssm / f"tests/prebuilt-elf/microbenchmark/{family}/elf/{family}/{name}.elf"
        try:
            counts = generate(case, directory)
            row["python_counts"] = counts
            row["python_runs"] = python_runs(directory, config)
            row["python_status"] = ("error" if any("error" in r for r in row["python_runs"]) else
                                    "pass" if row["python_runs"][0] == row["python_runs"][1] else "unstable")
            row["python_cycles"] = [r["total"] for r in row["python_runs"]]
        except Exception:
            row["python_status"] = "error"
            row["notes"].append(traceback.format_exc())
            (directory / "python-error.log").write_text(row["notes"][-1])
        row["work"] = dict(macs=m*n*k if k else 0,
                           elements=m*n if op not in {"matmul", "copy"} else 0,
                           reduction_adds=m*(n-1) if op == "row_sum" else 0,
                           read_bytes=(m*k+k*n)*2 if k else m*n*4*(2 if op == "add" else 1),
                           write_bytes=m*(1 if op == "row_sum" else n)*4)
        if not elf.exists():
            row.update(ssm_status="missing_elf", workload_status="blocked_missing_elf")
        else:
            shutil.copy2(elf, directory / elf.name)
            row["elf_sha256"] = sha(elf)
            for source in [bench / f"microbenchmark/{family}/src/{name}.cpp",
                           bench / f"microbenchmark/{family}/{family}_bench.hpp",
                           Path(str(elf) + ".diss")]:
                if source.exists():
                    shutil.copy2(source, directory / source.name)
            def execute_repeat(repeat):
                functional, log = run([ssm / "bin/gfrun", "-f", elf, "-t", "1", "-s",
                                      "softcore.multiThreadNum=1", "--log_file", directory / f"gfrun-{repeat}.trace",
                                      "--dump-run-state", directory / f"gfrun-{repeat}.state.json"],
                                     directory, f"gfrun-{repeat}", timeout=timeout)
                trace_path = directory / f"gfrun-{repeat}.trace"
                trace = trace_path.read_text(errors="replace") if trace_path.exists() else ""
                functional["success"] = bool(functional["exit_code"] == 0 and
                                             re.search(r"Reach the End of Benchmark! R2 = 0\b", log + trace))
                timing, log = run([ssm / "bin/gfsim", "-f", elf, "-t", "0", "--seed", "1",
                                  "--swimlane", "1", "--swimfile", directory / f"gfsim-{repeat}.swimlane.json",
                                  "-s", *overrides], directory, f"gfsim-{repeat}", timeout=timeout)
                timing["cycles"] = native_cycles(log)
                return dict(functional=functional, timing=timing)
            with ThreadPoolExecutor(max_workers=args.repeat_workers) as workers:
                row["ssm_runs"] = list(workers.map(execute_repeat, (1, 2)))
            row["ssm_cycles"] = [r["timing"]["cycles"] for r in row["ssm_runs"]]
            row["ssm_status"] = "pending_event_audit"
            audit(directory, row)
        write_json(directory / "result.json", row)
        write_json(output / "summary.json", results)
    summarize(output, results)


def summarize(output, results):
    for row in results:
        row["operation_counts"] = dict(python=row.get("python_counts"),
                                       ssm=[a["executed_counts"] for a in row.get("audit", [])])
        row["tile_intervals"] = dict(
            python=[r.get("tile_interval") for r in row.get("python_runs", [])],
            ssm_candidate=[a["candidate_residency_interval"] for a in row.get("audit", [])])
    write_json(output / "summary.json", results)
    paired = [r for r in results if r["ssm_status"] == "pass" and r["python_status"] == "pass"
              and r.get("capture_audit", {}).get("byte_counts_match")
              and all(a["functional_ok"] and a["timing_ok"] for a in r.get("audit", []))]
    established = (any(r["operation"] == "matmul" for r in paired) and
                   any(r["operation"] in {"add", "exp", "row_sum"} for r in paired))
    write_json(output / "acceptance.json", dict(
        all_seven_statuses_recorded=len(results) == 7,
        paired_native_baseline_established=established,
        directly_comparable_interval_available=False,
        paired_cases=[r["case"] for r in paired],
        classification="native-cycle task baseline" if established else "environment/interface investigation"))
    fields = ["case", "ssm_status", "python_status", "workload_status", "timing", "ssm_cycles",
              "python_cycles", "ratio", "operation_counts", "work", "tile_intervals",
              "configuration_difference", "notes", "source_run"]
    with (output / "summary.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in results:
            writer.writerow({key: json.dumps(row[key], ensure_ascii=False) if isinstance(row.get(key), (list, dict))
                             else row.get(key) for key in fields})


if __name__ == "__main__":
    main()
