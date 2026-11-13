"""Joint route/Calendar compiler, independent of core instructions and bindings."""
import copy
import math
from wse_perf_sim.models.noc.protocol import digest
from wse_perf_sim.models.noc.routing import node_id, integer, multicast_routes, route_tree, transmissions
from wse_perf_sim.errors import ModelError


def compile_phase(phase, platform):
    """Plan copy traffic in conservative, nonoverlapping whole-packet windows.

    Uses existing NoC and Shared-write service parameters. A packet drains its
    tree and destination writes before the following tree starts. No new hardware
    register format or payload storage is required by this behavioral model.
    """
    if phase.get('routing') != 'auto':
        if 'routing' in phase:
            raise ModelError('Unknown routing mode')
        return
    parameters = platform['network']
    payload = parameters['link_bytes_per_cycle'] - parameters['header_bytes']
    participants = {node_id(entry['core']) for entry in phase['nodes']}
    write = platform['write_service']
    cursor = 0
    for entry in sorted(phase['nodes'], key=lambda item: item['core']):
        if any(field in entry for field in ('routes', 'route_bits', 'window')):
            raise ModelError('Automatic routing takes destinations, not precompiled routes/windows')
        destinations = entry.get('destinations', sorted(participants))
        if not set(destinations) <= participants:
            raise ModelError('Route lands outside registered participants')
        size = integer(entry['nbytes'], 'nbytes', 1)
        flits = math.ceil(size / payload)
        # Endpoint delivery, edge rounding and one visibility-boundary margin.
        service = (math.ceil(size / write['throughput']) + write['latency']
                   + platform['endpoint_command_cycles'] + 2)
        write_cycles = math.ceil(service * parameters['noc_clock_hz'] / parameters['core_clock_hz'])
        entry['routes'] = []
        for bits in multicast_routes(entry['core'], destinations):
            _, _, distance = route_tree(entry['core'], bits)
            duration = (parameters['tx_cdc_cycles'] + flits + max(distance.values()) * parameters['hop_cycles']
                        + parameters['rx_cdc_cycles'] + write_cycles + parameters['guard_cycles'])
            entry['routes'].append(dict(route_bits=bits, start_cycle=cursor, end_cycle=cursor + duration))
            cursor += duration


def make_installation(phases, platform):
    """WSE-side configuration contains no instruction ids or Tile descriptors."""
    calreg = {}
    for phase in phases:
        entries = {}
        for entry in phase['nodes']:
            routes = transmissions(entry, platform['network'])
            entries[entry['core']] = dict(
                windows=[{k: route[k] for k in ('start_cycle', 'end_cycle')} for route in routes],
                route_digest=digest([route['route_bits'] for route in routes]),
                nbytes=entry['nbytes'], source_offset=entry.get('source_offset', 0),
                destination_offset=entry['destination_offset'], expected=copy.deepcopy(entry['expected']))
        calreg[phase['name']] = dict(opcode=phase['opcode'], epoch=phase['epoch'], entries=entries)
    body = dict(schema='wse-communit/1', platform_hash=digest(platform), calreg=calreg)
    return dict(body, version=digest(body))
