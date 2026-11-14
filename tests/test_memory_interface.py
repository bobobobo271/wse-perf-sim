"""#9 contract tests. Synthetic core-cycle services, no silicon calibration.

Standalone: 128 B SRAM, 16 B/cycle, read/write latency 3, queue 2;
network 16 B/cycle, latency 2, control 8 B, header 4 B, two credits.
Each variation changes only the named field. Integrated tests use wse_config.
"""
import copy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

from errors import ModelError
from examples.communication.memory import MemoryClient
from models.memory.sram import SRAM
from models.noc.memory import MemoryNetwork
from runtime.memory import MemoryRequest, MemorySubmit, MemoryRetired, MemoryDone, MemoryRejected
from runtime.runner import Runner
from config import wse_config
from test_model import fixture, op, tile, ROOT
from api import simulate, simulate_core


class MemoryContractTests(unittest.TestCase):
    def make(self, requests, *, inflight=2, depth=2, retire_delay=0, sequential=False, latency=2):
        sram = SRAM(1,capacity=128,throughput=16,read_latency=3,write_latency=3,
                    queue_depth=depth,storage=bytearray(range(128)))
        network = MemoryNetwork((0,1),throughput=16,latency=latency,control_bytes=8,header_bytes=4,inflight=inflight)
        client = MemoryClient('client',requests,retire_delay=retire_delay,sequential=sequential)
        runner = Runner({c.name:c for c in (sram,network,client)})
        return sram,network,client,runner

    def test_local_bypasses_network_remote_has_three_stages(self):
        for src in (0,1):
            request=MemoryRequest((src,0),src,1,'read',16,32)
            s,n,c,r=self.make([(0,request,'memory-network' if src==0 else 'core:1/sram')])
            r.run()
            done=c.done[request.request_id]
            self.assertEqual(done.data,bytes(range(16,48)))
            self.assertEqual([e['engine'] for e in done.stages],
                             ['memory_request','sram','memory_response'] if src==0 else ['sram'])
            self.assertEqual(len(n.events),2 if src==0 else 0)
            if src==0:
                self.assertEqual([e['work'] for e in n.events],[12,36])
                self.assertLess(done.stages[0]['complete'],done.stages[1]['start'])
                self.assertLess(done.stages[1]['complete'],done.stages[2]['start'])

    def test_write_confirmation_then_read_returns_new_bytes(self):
        payload=bytes([99]*32)
        write=MemoryRequest((0,0),0,1,'write',16,32,payload)
        read=MemoryRequest((0,1),0,1,'read',16,32)
        s,n,c,r=self.make([(0,write,'memory-network'),(0,read,'memory-network')],sequential=True)
        r.run()
        self.assertEqual(c.done[(0,1)].data,payload)
        self.assertEqual(bytes(s.storage[16:48]),payload)
        self.assertEqual([e['work'] for e in n.events],[36,12,12,36])

    def test_credit_held_until_completion_consumed(self):
        requests=[(0,MemoryRequest((0,i),0,1,'read',i*16,16),'memory-network') for i in range(2)]
        s,n,c,r=self.make(requests,inflight=1,retire_delay=20)
        r.run()
        self.assertTrue(any(x.retryable for x in c.rejected))
        first=next(cycle for cycle,msg in c.history if isinstance(msg,MemoryDone) and msg.request_id==(0,0))
        self.assertGreater(c.accepted[(0,1)],first+20)
        self.assertEqual(len(s.events),2)
        self.assertFalse(n.active)

    def test_sram_queue_backpressure_and_exactly_once_service(self):
        requests=[(0,MemoryRequest((1,i),1,1,'read',i*16,16),'core:1/sram') for i in range(3)]
        s,n,c,r=self.make(requests,depth=1,retire_delay=4)
        r.run()
        self.assertEqual(len(c.done),3)
        self.assertEqual(len(s.events),3)
        self.assertTrue(c.rejected)

    def test_shared_sram_local_remote_contend_same_port(self):
        # 64 B takes four service cycles; remote request reaches the same port later.
        local=MemoryRequest((1,0),1,1,'read',0,64)
        remote=MemoryRequest((0,0),0,1,'read',32,64)
        s,n,c,r=self.make([(0,remote,'memory-network'),(5,local,'core:1/sram')])
        r.run()
        spans=sorted((e['start'],e['service_end']) for e in s.events)
        self.assertLessEqual(spans[0][1],spans[1][0])
        self.assertEqual(s.service.bytes['core:1/sram'],128)

    def test_different_srams_run_in_parallel_and_responses_match_out_of_order(self):
        s,n,c,r=self.make([(0,MemoryRequest((1,0),1,1,'read',0,16),'core:1/sram')])
        slow=SRAM(0,capacity=128,throughput=16,read_latency=20,write_latency=3,storage=bytearray([55]*128))
        c.requests.insert(0,(0,MemoryRequest((0,0),0,0,'read',0,16),'core:0/sram'))
        r.components[slow.name]=slow
        r.tick_order.append(slow.name)
        r.run()
        self.assertEqual(s.events[0]['start'],slow.events[0]['start'])
        order=[m.request_id for _,m in c.history if isinstance(m,MemoryDone)]
        self.assertEqual(order,[(1,0),(0,0)])
        self.assertEqual(c.done[(0,0)].data,bytes([55]*16))

    def test_bad_range_target_payload_and_duplicate_identity(self):
        base=MemoryRequest((0,0),0,1,'read',0,16)
        for request in (replace(base,dst_core=99),replace(base,offset_bytes=120),replace(base,nbytes=0),
                        replace(base,op='write',data=b'bad')):
            s,n,c,r=self.make([(0,request,'memory-network')])
            r.run()
            self.assertIsInstance(c.done[(0,0)],(MemoryRejected,MemoryDone))
            self.assertNotEqual(getattr(c.done[(0,0)],'status','rejected'),'success')
            self.assertEqual(len(s.events),0)
        s,n,c,r=self.make([(0,base,'memory-network')]);r.run()
        n.receive('commands',MemorySubmit('client',base));n.tick(r.cycle+1)
        (_,_,reply),=n.drain_outputs()
        self.assertIsInstance(reply,MemoryRejected)
        self.assertFalse(reply.retryable)

    def test_network_latency_changes_elapsed_not_sram_service(self):
        runs=[]
        for latency in (2,12):
            s,n,c,r=self.make([(0,MemoryRequest((0,0),0,1,'read',0,32),'memory-network')],latency=latency)
            r.run();runs.append((r.cycle,s.service.busy.copy()))
        self.assertEqual(runs[1][0]-runs[0][0],20)
        self.assertEqual(runs[0][1],runs[1][1])


class MemoryIntegrationTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory(dir=ROOT/'workspace');self.addCleanup(tmp.cleanup)
        self.path=Path(tmp.name)

    def capture(self,target=1):
        memory=dict(object_id='memory',kind='tensor',dtype='float32',shape=[1,32],memory_space='SRAM')
        x=tile('x','load',shape=(1,32),shared=True)
        load=op('load',inputs=[memory],outputs=[x],name='tile.load')
        load['memory_accesses']=[dict(object_id='memory',shape=[1,32],offset_bytes=16,target_core=target)]
        fixture(self.path,[load,op('consume',inputs=[x],name='tile.adds')])

    def test_remote_load_idle_target_then_tile_writeback_and_consumer(self):
        self.capture()
        report=simulate(self.path,wse_config())
        rows={row['op_id']:row for row in report['timeline']}
        load=rows['load']
        self.assertEqual([s['engine'] for s in load['stages'] if s['engine']!='shared_write'],
                         ['memory_request','sram','memory_response'])
        result=next(s for s in load['stages'] if s['engine']=='memory_response')
        write=next(s for s in load['stages'] if s['engine']=='shared_write')
        self.assertGreater(write['start'],result['complete'])
        self.assertGreater(rows['consume']['issue'],load['complete'])
        self.assertIn('core:1/sram',report['resources'])
        self.assertNotIn('core:1/tlsu',report['components'])

    def test_local_load_and_remote_sram_capacity_error(self):
        self.capture(target=0)
        report=simulate(self.path,wse_config())
        self.assertFalse(report['memory_network_events'])
        self.capture(target=40)
        with self.assertRaisesRegex(ModelError,'Target SRAM core'):
            simulate(self.path,wse_config())

    def test_core_only_memory_backend_injection(self):
        self.capture(target=0)
        config=wse_config();config.pop('hbm')
        with self.assertRaisesRegex(ModelError,'memory_backends'):
            simulate_core(self.path,config)
        sram=SRAM(0,capacity=1024,throughput=16,read_latency=2,write_latency=2)
        report=simulate_core(self.path,config,memory_backends={sram.name:sram})
        self.assertEqual(len(sram.events),1)
        self.assertNotIn('noc',report)

    def test_remote_store_completes_after_write_confirmation_before_dependent_load(self):
        memory=dict(object_id='memory',kind='tensor',dtype='float32',shape=[1,32],memory_space='SRAM')
        x=tile('x','produce',shape=(1,32))
        store=op('store',inputs=[x],outputs=[memory],name='tile.store')
        store['memory_accesses']=[dict(object_id='memory',shape=[1,32],offset_bytes=16,target_core=1)]
        load=op('load',inputs=[memory],outputs=[tile('y','load',shape=(1,32))],name='tile.load',deps=['store'])
        load['memory_accesses']=copy.deepcopy(store['memory_accesses'])
        fixture(self.path,[op('produce',outputs=[x]),store,load])
        report=simulate(self.path,wse_config())
        rows={row['op_id']:row for row in report['timeline']}
        write=next(e for e in report['sram_events'] if e['op']=='write')
        ack=next(e for e in rows['store']['stages'] if e['engine']=='memory_response')
        self.assertGreater(ack['start'],write['complete'])
        self.assertGreater(rows['store']['complete'],ack['complete'])
        self.assertGreater(rows['load']['issue'],rows['store']['complete'])

    def test_hbm_local_and_remote_share_target_sram_resource(self):
        # All accesses target core 1; only SRAM bandwidth is slowed to 16 B/cycle.
        memory=dict(object_id='memory',kind='tensor',dtype='float32',shape=[1,32],memory_space='SRAM')
        remote=op('remote',inputs=[memory],name='tile.load')
        remote['memory_accesses']=[dict(object_id='memory',shape=[1,32],target_core=1)]
        local=op('local',pe=1,inputs=[memory],name='tile.load')
        local['memory_accesses']=[dict(object_id='memory',shape=[1,32])]
        hbm=dict(memory,object_id='hbm',memory_space='HBM')
        transfer=op('copy',pe=1,inputs=[hbm],outputs=[memory],name='memory.copy')
        transfer['memory_accesses']=[dict(object_id=o['object_id'],shape=[1,32]) for o in (hbm,memory)]
        fixture(self.path,[remote,local,transfer])
        manifest=json.loads((self.path/'manifest.json').read_text())
        for instance in manifest['instances']:
            instance['mapping']=dict(core_id=instance['thread_id'],pe_id=0)
        (self.path/'manifest.json').write_text(json.dumps(manifest))
        config=wse_config()
        config['wse']['sram_bandwidth']['value']=16*config['wse']['core_clock_hz']['value']
        report=simulate(self.path,config)
        events=sorted(report['sram_events'],key=lambda e:e['start'])
        self.assertEqual(len(events),3)
        self.assertEqual({e['resource'] for e in events},{'core:1/sram'})
        for a,b in zip(events,events[1:]):
            self.assertLessEqual(a['service_end'],b['start'])
        self.assertEqual(report['resources']['core:1/sram']['bytes'],384)
        self.assertEqual(report['resources']['core:1/sram']['service_cycles'],24)
