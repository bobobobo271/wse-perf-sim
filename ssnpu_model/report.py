"""Build serializable timelines and statistics from completed simulation state."""
from __future__ import annotations


def build_report(*, cycle, nodes, timeline, block_members, source_hashes,
                 resources, waits, stage_waits, config):
    core_end, pe_end, pe_tile_end = {}, {}, {}
    for key, row in timeline.items():
        core = str(row['core'])
        core_end[core] = max(core_end.get(core, 0), row['complete'])
        if not nodes[key].control:
            pe = f'{core}:{row["pe"]}'
            pe_tile_end[pe] = max(pe_tile_end.get(pe, 0), row['complete'])
            pe_end[pe] = max(pe_end.get(pe, 0), row['complete'])
        elif nodes[key].raw['ir_op'] == 'barrier':
            for c, p in block_members[nodes[key].task, nodes[key].block]:
                pe = f'{c}:{p}'
                pe_end[pe] = max(pe_end.get(pe, 0), row['complete'])
    return {'total_cycles': cycle, 'instruction_count': sum(not i.control for i in nodes.values()),
            'core_completion': core_end, 'pe_completion': pe_end, 'pe_tile_completion': pe_tile_end,
            'source_sha256': source_hashes,
            'timeline': list(timeline.values()),
            'resources': {k: {'service_cycles': v, 'utilization': v / cycle if cycle else 0,
                              'bytes': resources.bytes[k]} for k, v in sorted(resources.busy.items())},
            'storage_peak_bytes': dict(resources.peak),
            'storage_live_bytes': dict(resources.usage),
            'storage_events': resources.storage_events,
            'wait_reason_sets': {k: dict(v) for k, v in waits.items()},
            'internal_stage_resource_wait_cycles': dict(stage_waits),
            'config': config,
            'assumptions': [
                'Synthetic rates; hardware cycle accuracy has not been calibrated.',
                'Unlimited window/ROB; order_dependencies establish age, not completion.',
                'Sequential internal stages; private TileReg access included in compute cost.',
                'Shared reads charged separately per consumer; no broadcast combining.',
                'Output allocated at issue; fixed slot overwrites await prior readers.',
                'Views are zero-cost aliases; reuse-only shared versions remain live-out.',
                'Memory paths use configured costs, without cache residency simulation.',
                'Wait reason sets count instruction-cycles; overlapping categories are not additive.']}
