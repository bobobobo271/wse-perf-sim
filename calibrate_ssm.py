"""Fit effective dtype costs to the fixed single-PE SSM microbenchmarks.

Standard library only. Calibrates the observed tile windows, not whole ELF time.
The exact fixed-chain timing reduction is checked against the real simulator.
"""
from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import json
import math
from pathlib import Path
import random
import shutil
import subprocess
import sys

from ssnpu_model import simulate
from ssnpu_model.errors import ModelError
from ssnpu_model.perfetto import to_pftrace

ROOT = Path(__file__).resolve().parent
TRAIN = ("tmatmul_fp16_16x32x32", "tmatmul_fp16_32x64x64", "tadd_fp32_16x16",
         "texp_fp32_16x16", "tload_fp32_16x16")
METRICS = ("span", "inputs_ready", "compute_window", "store_window")
PARAMS = ("memory_fp32_rate", "memory_fp32_latency", "memory_fp16_rate", "memory_fp16_latency",
          "cube_fp16_rate", "cube_fp16_latency", "vector_fp32_latency", "exp_fp32_latency")
BASE = [256, 8, 256, 8, 4096, 4, 2, 4]


def save(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n")


def ssm_metrics(events):
    loads = [e for e in events if "TLOAD" in e["name"]]
    stores = [e for e in events if "TSTORE" in e["name"]]
    computes = [e for e in events if "TLOAD" not in e["name"] and "TSTORE" not in e["name"]]
    assert loads and len(stores) == 1 and len(computes) <= 1
    first = min(e["ts"] for e in loads)
    return [max(e["ts"] + e["dur"] for e in events) - first,
            max(e["ts"] + e["dur"] for e in loads) - first,
            computes[0]["dur"] if computes else None, stores[0]["dur"]]


def python_metrics(report):
    tiles = [r for r in report["timeline"] if r["ir_op"].startswith("tile.")]
    loads = [r for r in tiles if r["ir_op"] == "tile.load"]
    stores = [r for r in tiles if r["ir_op"] == "tile.store"]
    computes = [r for r in tiles if r["ir_op"] not in ("tile.load", "tile.store")]
    first = min(r["issue"] for r in loads)
    return [max(r["complete"] for r in tiles) - first,
            max(r["complete"] for r in loads) - first,
            computes[0]["complete"] - computes[0]["issue"] if computes else None,
            stores[0]["complete"] - stores[0]["issue"]]


def predict(p):
    """Exact for these fixed chains, private tiles, 1 PE and unlimited ROB.

    Dispatch + operand/output handshakes give load/store +3, binary +7,
    unary +5; dependency wake-up/output reservation gives each gap +4.
    Independent loads serialize service on one memory port, not result latency.
    """
    r32, l32, r16, l16, rc, lc, lv, le = p
    service = lambda work, rate: math.ceil(work / rate)
    result = []
    for a, b, mac, c in [(1024, 2048, 16384, 2048), (4096, 8192, 131072, 8192)]:
        load = service(a, r16) + service(b, r16) + l16 + 3
        compute = service(mac, rc) + lc + 7
        store = service(c, r32) + l32 + 3
        result.append([load + compute + store + 8, load, compute, store])
    load = 2 * service(1024, r32) + l32 + 3
    compute = 1 + lv + 7  # keep Vector throughput=256: only one observed shape
    store = service(1024, r32) + l32 + 3
    result.append([load + compute + store + 8, load, compute, store])
    load = service(1024, r32) + l32 + 3
    compute = 4 + le + 5  # keep exp throughput=64: only one observed shape
    result.append([load + compute + store + 8, load, compute, store])
    result.append([load + store + 4, load, None, store])
    return result


def rate_domain(workloads, lower, upper):
    # Ceiling costs change only at work / integer-service boundaries. Keep
    # millibyte/MAC precision, rounding upward so a boundary isn't crossed down.
    values = {lower, upper}
    for work in workloads:
        for count in range(math.ceil(work / upper), math.ceil(work / lower) + 1):
            rate = math.ceil(work / count * 1000) / 1000
            if lower <= rate <= upper:
                values.add(rate)
    return sorted(values)


def domains():
    memory = rate_domain([1024, 2048, 4096, 8192], 32, 256)
    return [memory, list(range(81)), memory, list(range(101)),
            rate_domain([16384, 131072], 384, 2048), list(range(129)),
            list(range(41)), list(range(65))]


def loss(p, targets, stage_weight):
    score = 0.0
    for actual, expected in zip(predict(p), targets):
        for index, (a, b) in enumerate(zip(actual, expected)):
            if b is not None:
                score += (2 if index == 0 else stage_weight) * ((a - b) / b) ** 2
    return score


def fit(targets, stage_weight, seed=0):
    choices = domains()
    rng = random.Random(seed)
    # Data-derived initial estimates: two shapes identify an effective slope
    # and intercept. They don't establish physical parameter identifiability.
    small, large, add, exp, copy_target = targets
    cube_rate = (131072 - 16384) / (large[2] - small[2])
    cube_latency = small[2] - math.ceil(16384 / cube_rate) - 7
    memory32_rate = (8192 - 2048) / (large[3] - small[3])
    memory32_latency = small[3] - math.ceil(2048 / memory32_rate) - 3
    memory16_rate = (12288 - 3072) / (large[1] - small[1])
    memory16_latency = small[1] - math.ceil(1024 / memory16_rate) - math.ceil(2048 / memory16_rate) - 3
    unit_seed = [memory32_rate, memory32_latency, memory16_rate, memory16_latency,
                 cube_rate, cube_latency, add[2] - 8, exp[2] - 9]
    memory_sum = (copy_target[0] - 10) // 2
    memory_service = add[0] - 2 * memory_sum - (add[2] - 7) - 21
    span_rate = 1024 / memory_service
    span_latency = memory_sum - memory_service
    wanted_inputs = [t[0] - t[2] - math.ceil(size / span_rate) - span_latency - 3 - 8
                     for t, size in [(small, 2048), (large, 8192)]]
    span16_rate = (12288 - 3072) / (wanted_inputs[1] - wanted_inputs[0])
    span16_latency = wanted_inputs[0] - math.ceil(1024 / span16_rate) - math.ceil(2048 / span16_rate) - 3
    span_seed = [span_rate, span_latency, span16_rate, span16_latency,
                 cube_rate, cube_latency, add[2] - 8, exp[0] - 2 * memory_sum - 23]
    seeds = [BASE, unit_seed, span_seed, [128, 20, 128, 40, 1024, 40, 12, 28]]
    seeds += [[rng.choice(d) for d in choices] for _ in range(8)]
    best = None
    evaluations = 0
    def score(p):
        return loss(p, targets, stage_weight), loss(p, targets, 1)
    def better(a, b):
        return a[0] < b[0] - 1e-12 or (abs(a[0] - b[0]) <= 1e-12 and a[1] < b[1] - 1e-12)
    for initial in seeds:
        p = initial.copy()
        current = score(p)
        for _ in range(20):
            changed = False
            for index, domain in enumerate(choices):
                original = winner = p[index]
                cost = current
                for value in domain:
                    p[index] = value
                    candidate = score(p)
                    evaluations += 1
                    if better(candidate, cost):
                        winner, cost = value, candidate
                p[index] = winner
                changed |= winner != original
                current = cost
            if not changed:
                break
        if best is None or better(current, best[0]):
            best = (current, p.copy())
    return best[1], dict(loss=best[0][0], tie_break_stage_loss=best[0][1], objective_evaluations=evaluations,
                         stage_weight=stage_weight, span_weight=2, seed=seed,
                         search="deterministic multi-start coordinate search; no global-optimum claim",
                         bounds=[[min(d), max(d)] for d in choices])


def make_config(base, p, profile):
    config = copy.deepcopy(base)
    config["name"] = f"ssm-{profile}-calibrated-single-pe"
    source = f"effective fit to SSM 637bde0 single-PE tile windows; {profile}; in-sample only"
    for engine, dtype, rate, latency in [
        ("memory", "float32", p[0], p[1]), ("memory", "float16", p[2], p[3]),
        ("cube", "float16", p[4], p[5]), ("vector", "float32", 256, p[6]),
        ("exp", "float32", 64, p[7]),
    ]:
        config["engines"][engine]["dtype_overrides"][dtype] = dict(
            throughput=rate, latency=latency, source=source)
    config["calibration"] = dict(
        training_cases=list(TRAIN), target="SSM LIQ-start to final store response; Python issue to complete",
        convention="boundary differences folded into effective costs; not hardware cycle accuracy",
        duplicate_case="tstore_fp32_16x16 excluded from fitting: same workload as tload",
        excluded_case="trowsum_fp32_16x16: performance model rejects tile.create",
        held_fixed="issue widths, ROB, capacities, message timing, Vector/exp rates and all unobserved dtype costs",
        parameters=dict(zip(PARAMS, p)), validation="training-data replay; no independent holdout")
    return config


def verify_reduction(baseline, config):
    rng = random.Random(17)
    candidates = [BASE]
    choices = domains()
    candidates += [[rng.choice(d) for d in choices] for _ in range(4)]
    for p in candidates:
        cfg = make_config(config, p, "check")
        for case, expected in zip(TRAIN, predict(p)):
            assert python_metrics(simulate(baseline / case / "capture", cfg)) == expected, (case, p)


def metrics_summary(rows, profile):
    # Five distinct workloads; don't count two identical copy programs twice.
    observations = [r for r in rows if r["case"] in TRAIN]
    spans = [abs(r[profile][0] - r["ssm"][0]) / r["ssm"][0] for r in observations]
    phases = [abs(r[profile][i] - r["ssm"][i]) / r["ssm"][i]
              for r in observations for i in (1, 2, 3) if r["ssm"][i] is not None]
    return dict(span_mape_percent=100 * sum(spans) / len(spans),
                span_max_error_percent=100 * max(spans),
                stage_mape_percent=100 * sum(phases) / len(phases),
                stage_max_error_percent=100 * max(phases))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    baseline, output = args.baseline.resolve(), args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    shutil.copy2(__file__, output / "calibrate_ssm.py")
    save(output / "invocation.json", dict(argv=sys.argv, executable=sys.executable))
    for stem, argv in [("status", ["status", "--porcelain=v1"]), ("diff", ["diff", "--binary"])]:
        (output / f"model-{stem}.txt").write_bytes(subprocess.run(
            ["git", "-C", str(ROOT), *argv], capture_output=True, check=True).stdout)
    original = json.loads((baseline / "python-config.json").read_text())
    save(output / "before-config.json", original)
    shutil.copy2(baseline / "binary-hashes.json", output / "ssm-binary-hashes.json")
    summary = json.loads((baseline / "summary.json").read_text())
    targets, hashes = {}, {}
    for row in summary:
        assert row["ssm_status"] == "pass"
        case = row["case"]
        paths = [baseline / case / f"gfsim-{r}.tile-events.json" for r in (1, 2)]
        pair = [ssm_metrics(json.loads(path.read_text())) for path in paths]
        assert pair[0] == pair[1]
        targets[case] = pair[0]
        for path in paths:
            hashes[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
    hashes[str(baseline / "summary.json")] = hashlib.sha256((baseline / "summary.json").read_bytes()).hexdigest()
    save(output / "input-sha256.json", hashes)
    save(output / "targets.json", dict(metrics=METRICS, cases=targets))
    print("Checking timing reduction against real Simulator (25 replays)", flush=True)
    verify_reduction(baseline, original)
    profiles = {"before": original}
    for profile, stage_weight in [("span", 0), ("stage", 1)]:
        print(f"Fitting {profile}", flush=True)
        p, search = fit([targets[c] for c in TRAIN], stage_weight)
        config = make_config(original, p, profile)
        config["calibration"]["search"] = search
        profiles[profile] = config
        save(output / f"ssm-{profile}-calibrated.json", config)
        print(profile, dict(zip(PARAMS, p)), search, flush=True)
    rows = []
    for row in summary:
        case = row["case"]
        entry = dict(case=case, ssm=targets[case])
        rows.append(entry)
        for profile, config in profiles.items():
            directory = output / profile / case
            directory.mkdir(parents=True)
            reports, errors = [], []
            for repeat in (1, 2):
                try:
                    report = simulate(baseline / case / "capture", config)
                except ModelError as exc:
                    errors.append(str(exc))
                    (directory / f"{repeat}.error.txt").write_text(str(exc) + "\n")
                    continue
                reports.append(report)
                save(directory / f"{repeat}.json", report)
                events = report["instruction_events"]
                (directory / f"{repeat}.events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
                (directory / f"{repeat}.pftrace").write_bytes(to_pftrace(events))
            if errors:
                assert len(errors) == 2 and errors[0] == errors[1]
                entry[profile] = None
                entry[profile + "_error"] = errors[0]
            else:
                assert reports[0] == reports[1], (profile, case)
                entry[profile] = python_metrics(reports[0])
                entry[profile + "_total_cycles"] = reports[0]["total_cycles"]
                if case in TRAIN and profile != "before":
                    expected = predict([config["calibration"]["parameters"][name] for name in PARAMS])
                    assert entry[profile] == expected[TRAIN.index(case)]
        print(case, {key: entry.get(key) for key in ("ssm", "before", "span", "stage")}, flush=True)
    errors = {p: metrics_summary(rows, p) for p in profiles}
    save(output / "results.json", dict(metrics=METRICS, rows=rows, errors=errors,
                                      distinct_training_workloads=5, independent_validation=False))
    save(output / "verification.json", dict(
        exact_timing_reduction_replays=25, final_model_replay_attempts=42,
        deterministic_repeats=True, finalist_reduction_matches_simulator=True,
        model_implementation_modified=False, default_logic_config_modified=False,
        excluded_case="row_sum: tile.create unsupported", independent_validation=False))
    with (output / "results.csv").open("w", newline="") as handle:
        fields = ["case", "metric", "ssm", "before", "span", "stage"]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            for i, metric in enumerate(METRICS):
                writer.writerow(dict(case=row["case"], metric=metric,
                                     **{p: row[p][i] if row[p] is not None else None
                                        for p in ("ssm", "before", "span", "stage")}))
    print(json.dumps(errors, indent=2), flush=True)


if __name__ == "__main__":
    main()
