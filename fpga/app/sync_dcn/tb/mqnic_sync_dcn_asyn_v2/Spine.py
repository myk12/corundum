import logging
import os
import struct
import sys 

import scapy.utils
from scapy.layers.l2 import Ether
from scapy.packet import Raw
from scapy.layers.inet import IP, TCP
import cocotb_test.simulator
import cocotb
import logging
from cocotb.log import SimLog
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, FallingEdge, Timer

from cocotbext.axi import AxiStreamBus
from cocotbext.axi import AxiSlave, AxiBus, SparseMemoryRegion
from cocotbext.eth import EthMac
from cocotbext.pcie.core import RootComplex
from cocotbext.pcie.xilinx.us import UltraScalePlusPcieDevice
from Tofino import Tofino, parse_packet
from Constants import port_mapping


class Spine:#tofino
    def __init__(self,port_set_1,port_set_2,router_table1,router_table2,spine_id):
        self.sw_nodes = []
        self.spines = []
        self.port_set= port_set_1+port_set_2
        self.leaf1 = Tofino(port_set_1, self, router_table1,1)
        self.leaf2 = Tofino(port_set_2, self, router_table2,2)
        self.Spine_id = spine_id
        self.tasks = []
        self.log = SimLog(f"cocotb.spine_{spine_id}")
        self.log.setLevel(logging.INFO)
    
    def add_sw_node(self, sw_node, port=None, leaf_id=None):
        self.sw_nodes.append({"node": sw_node, "port": port, "leaf": leaf_id})
    def add_hw_dut(self, hw_dut, port=None):
        self.hw_dut = hw_dut
    def add_spine(self, spine):
        self.spines.append({"spine": spine})
    def receive_packet(self,pkt: Ether, in_port):
        
        #self.log.info(f"Packet with target node ID {parse_packet(pkt)} received at Spine {self.Spine_id} on port {in_port}")
        self.receive_packet_for_queue(pkt, in_port)
    def receive_packet_for_queue(self, pkt: Ether, in_port):
        #1,检查in_port在哪一个leaf中
        if in_port in self.leaf1.port_set:
            #2,把包放入leaf1的输入队列
            return self.leaf1.add_queue_in(pkt, in_port)
        elif in_port in self.leaf2.port_set:
            return self.leaf2.add_queue_in(pkt, in_port)
        else:
            assert False, f"Input port {in_port} not found in any leaf switch of Spine {self.Spine_id}"
    async def send_packet(self, pkt: Ether, out_port,leaf_id):
        receiver_id = parse_packet(pkt)
        #检查receiver_id是否在本spine的sw_node中,且leaf_id是否正确
        node = next((node for node in self.sw_nodes if node["node"].node_id == receiver_id and node["leaf"] == leaf_id), None)
        if node is not None:
            assert node is not None, f"Target node ID {receiver_id} not found in sw_nodes of Spine {self.Spine_id} in port {out_port}"
            #打印日志
            #self.log.info(f"Packet with target node ID {receiver_id} sent to port {out_port} of Spine {self.Spine_id}")
            node["node"].recv_packet(pkt,out_port)
            return
        
        #读取mapping,找到out_port对应的in_port和spine
        ##self.log.info(f"Packet with target node ID {receiver_id} sent to spine port {out_port} of Spine {self.Spine_id} in port {out_port}")
        in_port = port_mapping.get(out_port)
        target_spine = next((spine for spine in self.spines if in_port in spine["spine"].port_set), None)
        assert target_spine is not None, f"Target spine with input port {in_port} not found in Spine {self.Spine_id},out_port {out_port},leaf_id {leaf_id},receiver_id {receiver_id}"
        #启动协程
        #self.log.info(f"Packet with target node ID {receiver_id} sent to Spine {self.Spine_id} for internal transfer to port {out_port} (in_port {in_port})")
        target_spine["spine"].receive_packet(pkt, in_port)
        
    def start(self):
        for node in self.sw_nodes:
            task = cocotb.start_soon(node['node'].run_multi_layer_inference(32))  # Start software node runner
            self.tasks.append(task)

        #cocotb.start_soon(self.hw_dut._run_consensus_app())  # Start hardware DUT runner

    async def wait_done(self):
        for task in self.tasks:
            await task

        #self.hw_dut._running = False
        #cocotb.start_soon(self.hw_dut._stop_consensus_app())  # Stop hardware DUT runner
