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
from cocotb.log import SimLog
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, FallingEdge, Timer

from cocotbext.axi import AxiStreamBus
from cocotbext.axi import AxiSlave, AxiBus, SparseMemoryRegion
from cocotbext.eth import EthMac
from cocotbext.pcie.core import RootComplex
from cocotbext.pcie.xilinx.us import UltraScalePlusPcieDevice
PROT_RATE = 10 #Gbps
FLOW_SIZE_BYTES = 1500#
HOP_DELAY_NS = 600  # 

class Switch:
    def __init__(self,zone1,zone2,switch_id):
        self.sw_nodes = []
        self.switchs = []
        self.zone1 = zone1
        self.zone2 = zone2
        self.switch_id = switch_id
        self.tasks = []


    def add_sw_node(self, sw_node, port=None):
        self.sw_nodes.append({"node": sw_node, "port": port})
        return sw_node
    def add_hw_dut(self, hw_dut, port=None):
        self.hw_dut = hw_dut
    def add_switch(self, switch, port=None):
        self.switchs.append({"switch": switch, "port": port})
        return switch
    async def receive_packet_leaf(self, pkt: Ether, sender_id: int, hw_node: bool,receiver_id:int,switch_id:int):
        await Timer(HOP_DELAY_NS, 'ns')  # Simulate hop delay
        if switch_id ==1:
            target_switch_id = 2
        else:
            target_switch_id = 1
        target_switch = next((switch for switch in self.switchs if switch["switch"].switch_id == target_switch_id), None)
        assert target_switch is not None, f"Target switch with ID {target_switch_id} not found in switch {self.switch_id}"
        await target_switch["switch"].receive_packet_root(pkt, sender_id, receiver_id)

    async def receive_packet_root(self, pkt: Ether, sender_id : int, receiver_id: int):#node尝试把消息广播到
        #检查receiver_id是否在software节点中
        await Timer(HOP_DELAY_NS, 'ns')  # Check every hop delay
        node = next((node for node in self.sw_nodes if node["node"].node_id == receiver_id), None)
       
        assert node["node"].node_id != sender_id and node is not None, f"Receiver node with ID {receiver_id} not found in switch {self.switch_id} or sender and receiver are the same"
        #node["node"].log.info(f"Software Node {node['node'].node_id} received packet from Node {sender_id}: {pkt.summary()}")
        await node["node"].recv_packet(pkt)


    async def receive_packet_node(self, pkt: Ether, sender_id : int, receiver_id: int):#node尝试把消息广播到
        await Timer(HOP_DELAY_NS, 'ns')  # Check every hop delay
        #检查receiver_id是否在software节点中
        node = next((node for node in self.sw_nodes if node["node"].node_id == receiver_id), None)
        if node is None:
            for switch in self.switchs:
                switch_obj = switch["switch"]
                await switch_obj.receive_packet_leaf(pkt, sender_id, hw_node=False, receiver_id=receiver_id,switch_id=self.switch_id)

        else:
            assert node["node"].node_id != sender_id
            #node["node"].log.info(f"Software Node {node['node'].node_id} received packet from Node {sender_id}: {pkt.summary()}")
            await node["node"].recv_packet(pkt)
        

    def start(self):
        for node in self.sw_nodes:
            task = cocotb.start_soon(node['node']._run_consensus_app())  # Start software node runner
            self.tasks.append(task)

        #cocotb.start_soon(self.hw_dut._run_consensus_app())  # Start hardware DUT runner

    async def wait_done(self):
        for task in self.tasks:
            await task

        #self.hw_dut._running = False
        #cocotb.start_soon(self.hw_dut._stop_consensus_app())  # Stop hardware DUT runner
