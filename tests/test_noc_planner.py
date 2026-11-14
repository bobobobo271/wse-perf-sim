"""Behavioral auto-routing tests using explicit synthetic captures and WSE40 config.

Baseline: 5x8 mesh, 1.4/2 GHz core/NoC, 64 B/flit, 12 B header,
1 NoC cycle/hop, 256 B/core-cycle Shared write port. Vary one named condition.
"""
import copy
import itertools
import json
import math
import tempfile
import unittest
from collections import defaultdict
from pathlib import Path

from api import ModelError, wse_config
from frontend.communication import allgather_group, compile_plan, multicast_routes, route_tree
from simulator import Simulator
from frontend.capture import TraceAdapter
from test_model import fixture, op, simulate, tile

WORK = Path(__file__).resolve().parents[1] / 'workspace/calendar-integration/tests'


def group_capture(path, members, size=256):
    sources, targets, owners, rows = {}, {}, {}, []
    for thread, core in enumerate(members):
        owners[core] = f'produce{core}'
        sources[core] = tile(f'source{core}', owners[core], shape=(1, size // 4), shared=True)
        targets[core] = tile(f'target{core}', None, shape=(1, len(members) * size // 4), shared=True)
        rows.extend([op(owners[core], pe=thread, outputs=[sources[core]]),
                     op(f'consume{core}', pe=thread, inputs=[targets[core]], name='tile.adds',
                        outputs=[tile(f'out{core}', f'consume{core}')])])
    fixture(path, rows)
    manifest = json.loads((path / 'manifest.json').read_text())
    for entry in manifest['instances']:
        entry['mapping'] = dict(core_id=members[entry['thread_id']], pe_id=0)
    (path / 'manifest.json').write_text(json.dumps(manifest))
    return allgather_group(sources, targets, owners, size)


class AutomaticCalendarTests(unittest.TestCase):
    def test_all_subsets_have_unique_landing_nodes(self):
        for size in range(1, 5):
            for destinations in itertools.combinations((0, 1, 8, 9), size):
                landed = []
                for bits in multicast_routes(0, destinations):
                    landed.extend(route_tree(0, bits)[1])
                self.assertEqual(sorted(landed), list(destinations))

    def test_cycle_disconnection_and_illegal_bits_rejected(self):
        from models.noc.routing import route_bitmap
        for bits in (route_bitmap({0,1,8,9}, {1}), route_bitmap({0,2}, {2}), 1):
            with self.assertRaises(ModelError):
                route_tree(0, bits)

    def test_same_opcode_different_layout_requires_new_profile(self):
        from examples.communication.streaming import demo_layouts
        from models.noc.compiler import compile_calendar, make_installation
        from config import platform_spec
        from dataclasses import replace
        platform = platform_spec(wse_config(), access_bytes=32, tx_bytes=64, rx_bytes=64)
        opcode, layouts = demo_layouts('broadcast')
        a = compile_calendar(opcode, layouts, platform)
        changed = dict(layouts)
        changed[0] = (replace(layouts[0][0], src_offset=32), None)
        b = compile_calendar(opcode, changed, platform)
        with self.assertRaisesRegex(ModelError, 'different CalReg'):
            make_installation([a,b], platform)
        self.assertEqual(len(make_installation([a,a], platform)['calreg']), 1)

    def test_every_link_has_nonoverlapping_flit_slots(self):
        from examples.communication.streaming import build_demo
        unit, _ = build_demo('gather')
        occupied = set()
        for profile in unit.calreg.values():
            for segment in profile['segments']:
                for event in segment['links']:
                    slot = (tuple(event['link']), event['noc_cycle'])
                    self.assertNotIn(slot, occupied)
                    occupied.add(slot)
        self.assertTrue(occupied)
