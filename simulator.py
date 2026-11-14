"""Compose independent core and network components with a common runner."""
from math import ceil
from errors import ModelError
from models.ssnpu.system import build_components
from runtime.runner import Runner


class Simulator(Runner):
    def __init__(self, trace, config, tick_order=None, *, core_only=False, comm_backend=None, memory_backends=None):
        self.trace, self.config = trace, config
        self.nodes = trace.instructions
        components = build_components(trace, config)
        self.noc = None
        self.hbm = None
        if core_only:
            if 'hbm' in config:
                raise ModelError('simulate_core requires a core configuration without HBM')
            if any(i.raw['ir_op'].startswith('comm.') for i in self.nodes.values()) and comm_backend is None:
                raise ModelError('Core communication instructions require comm_backend')
        elif 'hbm' in config:
            from models.memory.hbm import HBM
            self.hbm = HBM(config)
            components['hbm'] = self.hbm
        if memory_backends is not None:
            if set(components) & set(memory_backends):
                raise ModelError('Memory backend component name collision')
            components.update(memory_backends)
        if core_only and memory_backends is None and 'wse' in config and any(
                obj.get('memory_space') == 'SRAM' for i in self.nodes.values()
                for obj in i.raw.get('inputs', []) + i.raw.get('outputs', [])):
            raise ModelError('Core SRAM access requires injected memory_backends')
        if 'wse' in config and not core_only and memory_backends is None:
            from models.memory.sram import SRAM
            from models.noc.memory import MemoryNetwork
            p = {k: v['value'] for k, v in config['wse'].items()}
            transport = config['memory_transport']
            for node in range(40):
                sram = SRAM(node, capacity=p['sram_capacity_bytes'],
                    throughput=p['sram_bandwidth']/p['core_clock_hz'],
                    read_latency=ceil(p['sram_read_startup_ns']*p['core_clock_hz']/10**9),
                    write_latency=ceil(p['sram_write_startup_ns']*p['core_clock_hz']/10**9),
                    queue_depth=transport['sram_queue_depth'])
                components[sram.name] = sram
            memory = MemoryNetwork(range(40), **{k: transport[k] for k in
                ('throughput','latency','control_bytes','header_bytes','inflight')})
            components[memory.name] = memory
        if comm_backend is not None:
            components['communit'] = comm_backend
        elif not core_only and trace.calendar_phases:
            from models.noc.unit import CommUnit
            self.noc = CommUnit(trace.comm_installation, trace.comm_platform)
            components['communit'] = self.noc
        backend = components.get('communit')
        super().__init__(components, message_log=config.get('message_log', False),
                         advance=getattr(backend, 'advance', None), tick_order=tick_order)

    def run(self):
        super().run()
        from analysis.report import build_report
        return build_report(self)
