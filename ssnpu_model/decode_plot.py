"""Export measured simulator service intervals, never a schematic schedule."""
from collections import defaultdict
import json
from pathlib import Path


COLORS = {'HBM': '#d97706', 'SRAM': '#0284c7', 'Cube': '#16a34a',
          'Vector': '#7c3aed', 'Shared': '#94a3b8', 'Calendar': '#dc2626'}


def service_intervals(report):
    cycle_us = 1e6/report['config']['wse']['core_clock_hz']['value']
    intervals = []
    for row in report['timeline']:
        for stage in row['stages']:
            resource = stage['resource']
            if resource == 'chip/hbm':
                kind = 'HBM'
            elif resource.endswith('/memory'):
                kind = 'SRAM'
            elif '/cube' in resource:
                kind = 'Cube'
            elif '/vector' in resource:
                kind = 'Vector'
            else:
                kind = 'Shared'
            intervals.append(dict(lane=resource, kind=kind, op_id=row['op_id'],
                start_us=stage['start']*cycle_us, end_us=stage['service_end']*cycle_us))
    for event in report.get('noc', {}).get('events', []):
        if event['kind'] == 'link':
            intervals.append(dict(lane='NoC/'+event['resource'], kind='Calendar',
                op_id=event['phase'], start_us=event['time_ns']/1000, end_us=event['end_ns']/1000))
    return intervals


def export_resource_trace(report, output):
    """Perfetto-compatible JSON with actual HBM/SRAM/Cube/Vector/NoC services."""
    intervals = service_intervals(report)
    lanes = {name: index+1 for index, name in enumerate(sorted({event['lane'] for event in intervals}))}
    events = [dict(ph='M', name='process_name', pid=1, args={'name': 'WSE resource service'})]
    events.extend(dict(ph='M', name='thread_name', pid=1, tid=tid, args={'name': name}) for name, tid in lanes.items())
    events.extend(dict(ph='X', name=event['op_id'], cat=event['kind'], pid=1, tid=lanes[event['lane']],
                       ts=event['start_us'], dur=event['end_us']-event['start_us'], args=event)
                  for event in intervals)
    Path(output, 'resources.trace.json').write_text(json.dumps(dict(traceEvents=events, displayTimeUnit='ns'))+'\n')


def export_pipeline(report, output):
    # Imported only by the example runner; timing API remains standard-library only.
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib.patches import Patch

    output = Path(output)
    intervals = service_intervals(report)
    (output/'pipeline_intervals.json').write_text(json.dumps(intervals, indent=2)+'\n')
    groups = defaultdict(list)
    for event in intervals:
        lane = event['lane']
        if event['kind'] in ('Cube', 'Vector'):
            # PE lanes remain separate; same-core compute can overlap.
            label = lane
        elif event['kind'] == 'Shared':
            continue  # Available in the detailed JSON / Perfetto export.
        else:
            label = lane
        groups[label].append(event)
    lanes = sorted(groups, key=lambda name: (0 if name == 'chip/hbm' else 2 if name.startswith('NoC/') else 1, name))
    fig, ax = plt.subplots(figsize=(17, max(5, len(lanes)*0.25+1.8)), layout='constrained')
    for index, lane in enumerate(lanes):
        events = groups[lane]
        ax.broken_barh([(e['start_us'], e['end_us']-e['start_us']) for e in events],
                       (index-0.35, 0.7), facecolors=COLORS[events[0]['kind']], linewidth=0)
    ax.set_yticks(range(len(lanes)), lanes, fontsize=7)
    ax.invert_yaxis()
    ax.set_xlabel('Simulated time (µs)')
    ax.set_xlim(0, report['total_time_ns']/1000)
    ax.grid(axis='x', alpha=0.2)
    ax.set_title('Decode FlashAttention — compute, HBM/SRAM service and NoC link occupancy\n'
                 'Configured model timing; equal per-PE compute partitions; not calibrated hardware latency', fontsize=11)
    ax.legend(handles=[Patch(color=COLORS[k], label=k) for k in ('HBM', 'SRAM', 'Cube', 'Vector', 'Calendar')],
              loc='upper center', bbox_to_anchor=(0.5, 1.01), ncol=5, fontsize=8)
    fig.savefig(output/'pipeline.png', dpi=150)
    fig.savefig(output/'pipeline.svg')
    plt.close(fig)

    # A compact overview gives one row per core/domain, unioning PE service
    # intervals for readability. The detailed figure above preserves each PE.
    overview = defaultdict(list)
    for event in intervals:
        if event['kind'] == 'Shared':
            continue
        core = event['lane'].split('/')[0]
        label = 'NoC links (union)' if event['kind'] == 'Calendar' else core+' '+event['kind']
        overview[label].append(event)
    names = sorted(overview, key=lambda name: (0 if name.startswith('chip/') else 2 if name.startswith('NoC') else 1, name))
    fig, ax = plt.subplots(figsize=(15, max(5, len(names)*0.28+2)), layout='constrained')
    for index, name in enumerate(names):
        events = sorted(overview[name], key=lambda e: e['start_us'])
        merged = []
        for event in events:
            a, z = event['start_us'], event['end_us']
            if merged and a <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], z)
            else:
                merged.append([a, z])
        ax.broken_barh([(a,z-a) for a,z in merged], (index-.35,.7), facecolors=COLORS[events[0]['kind']], linewidth=0)
    ax.set_yticks(range(len(names)), names, fontsize=8)
    ax.invert_yaxis()
    ax.set_xlim(0, report['total_time_ns']/1000)
    ax.set_xlabel('Simulated time (µs)')
    ax.grid(axis='x', alpha=.2)
    ax.set_title('Decode pipeline overview — union of active service intervals\n'
                 'Gaps include data dependencies, resource waits and communication control', fontsize=11)
    fig.savefig(output/'pipeline_overview.png', dpi=150)
    plt.close(fig)
