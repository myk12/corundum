from email import header
import enum
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
ETHTYPE_CONSENSUS = 0xAE86
ETHTYPE_BEFORE_ATTENTION = 0xAE87
ETHTYPE_AFTER_ATTENTION = 0xAE88
ETHTYPE_BEFORE_MOE = 0xAE89
ETHTYPE_AFTER_MOE = 0xAE8A
MOE_MS = 1.4 #A100
ATTEN_NS = 2000
LAYER_NUM = 2
SLOT_LEN_US = 10
class PacketType(enum.IntEnum):
    BEFORE_ATTENTION = 1
    AFTER_ATTENTION = 2
    BEFORE_MOE = 3
    AFTER_MOE = 4
    CONSENSUS_ATTENTION = 5
    CONSENSUS_MOE = 6    

class GpuNode:
    def __init__(self, node_id, node_mac, ptp_clock_tod):
        self.node_id = node_id
        self.node_mac = node_mac
        self.log = SimLog(f"cocotb.sw_node_{node_id}")
        self.network_connected = False
        self.ptp_clock_tod = ptp_clock_tod
        self.slot_len = SLOT_LEN_US # Slot length in microseconds, should be configured to match the hardware DUT's slot length
        self.current_slot = 0
        # Time to send packet in microseconds, staggered based on node ID to avoid collisions
        #self.send_time = 2 * self.node_id#实际没有使用send_time,send_time只是为了说明不同节点发送时间错开,避免碰撞.实际发送时间由PTP时钟和slot_len决定
        self._running = False
        self.layer=0
        self.state=PacketType.BEFORE_MOE
        self.counter_before_atten = -1
        self.counter_after_atten = -1
        self.counter_before_moe = 7
        self.counter_after_moe = -1
        self.send_counter = 7
        
    def install_network(self, network, port=None):
        self.network = network
        self.network_connected = True
    def display_packet(self,pkt):
        ether_header = pkt[Ether]
        consensus_header = pkt[Ether].payload.load[:10]  # Assuming consensus header is 10 bytes
        
        src_mac = ether_header.src
        dst_mac = ether_header.dst
        eth_type = ether_header.type
        slot_id, node_id, ack_vec = struct.unpack("!QBB", consensus_header)
        if eth_type == ETHTYPE_BEFORE_ATTENTION:
            self.counter_before_atten -= 1
        elif eth_type == ETHTYPE_AFTER_ATTENTION:
            self.counter_after_atten -= 1
        elif eth_type == ETHTYPE_BEFORE_MOE:
            self.counter_before_moe -= 1
        elif eth_type == ETHTYPE_AFTER_MOE:
            self.counter_after_moe -= 1
        self.log.info(f"Packet details - Src MAC: {src_mac}, Dst MAC: {dst_mac}, Ethertype: {eth_type:#06x}, Slot ID: {slot_id}, Node ID: {node_id}, Ack Vec: {ack_vec:#04x}")

    def create_packet(self, slot_id: int,ptype: int):
        # Create a packet with the necessary information for consensus
        # Packet Format:
        # [Ethernet Header][Consensus Header][Payload]
        # - Ethernet Header: destination MAC (6 bytes), source MAC (6 bytes), ethertype (2 bytes)
        # - Consensus Header: slot_id 8 bytes, node_id 1 byte, ack_vec 1 byte
        # - Payload: padding node_id to 40 bytes total
        eth_header = Ether(src=self.node_mac, dst="ff:ff:ff:ff:ff:ff", type=ptype)
        header = struct.pack("!QBB", slot_id, self.node_id, 0x07)  # Example consensus header
        payload = bytes([self.node_id]) * 40  # Padding node_id to 40 bytes total

        pkt = eth_header / Raw(header + payload)

        return pkt

    
    async def recv_packet(self, pkt: Ether):
        # Process received packet and update internal state as needed
        #self.log.info(f"Node {self.node_id} received packet: {pkt.summary()}")
        self.display_packet(pkt)
    async def check_status(self):
        if self.counter_before_atten == 0 and self.send_counter == 0:
                self.log.info(f"Received All Tokens Before Attention - Node ID: {self.node_id}")
                self.counter_before_atten = -1
                self.counter_after_atten = 7
                #await Timer(ATTEN_NS, 'ns')  # Simulate attention delay
                #await Timer(20, 'us')
                self.log.info(f"Attention processing complete at Node {self.node_id}")
                assert self.state == PacketType.BEFORE_ATTENTION, f"Expected state to be BEFORE_ATTENTION but got {self.state}"
                self.state = PacketType.AFTER_ATTENTION
        if self.counter_after_atten == 0 and self.send_counter == 0:
                self.log.info(f"Received All Tokens After Attention - Node ID: {self.node_id}")
                self.counter_after_atten = -1
                assert self.state == PacketType.AFTER_ATTENTION, f"Expected state to be AFTER_ATTENTION but got {self.state}"
                self.layer += 1
                self.log.info(f"Node {self.node_id} completed inference layer {self.layer}")
                if self.layer == LAYER_NUM:
                    self._running=False
                    self.log.info(f"Node {self.node_id} completed inference")
                self.counter_before_moe = 7
                self.state = PacketType.BEFORE_MOE

        if self.counter_before_moe == 0 and self.send_counter == 0:
                self.log.info(f"Received All Tokens Before MOE - Node ID: {self.node_id}")
                self.counter_before_moe = -1
                self.counter_after_moe = 7
                #await Timer(MOE_MS, 'ms')  # Simulate MOE processing time
                #await Timer(20, 'us')
                self.log.info(f"MOE processing complete at Node {self.node_id}")
                assert self.state == PacketType.BEFORE_MOE, f"Expected state to be BEFORE_MOE but got {self.state}"
                self.state = PacketType.AFTER_MOE
        if self.counter_after_moe == 0 and self.send_counter == 0:
                self.log.info(f"Received All Tokens After MOE - Node ID: {self.node_id}")
                self.counter_after_moe = -1
                self.counter_before_atten = 7
                assert self.state == PacketType.AFTER_MOE, f"Expected state to be AFTER_MOE but got {self.state}"
                self.state = PacketType.BEFORE_ATTENTION
        

    
    async def _run_consensus_app(self):
        assert self.network_connected, "Network must be connected before starting consensus app runner"
        self.log.info("Starting consensus app runner")
        self._running = True
        last_slot_id = -1
        receiver_count = self.node_id
        self.send_counter=7
        while self._running:
            # wait clock cycle or event to trigger consensus app logic
            await Timer(2, 'us')  # Check every microsecond
            old_state = self.state
            await self.check_status()  
            if self._running == False:
                break
            new_state = self.state
            if old_state != new_state:
                 assert self.send_counter == 0, f"State changed from {old_state} to {new_state} but send_counter is not 0 at Node {self.node_id}"
                 self.log.info(f"Node {self.node_id} state changed from {old_state} to {new_state}")
                 self.send_counter = 7
                 receiver_count = self.node_id
            ptp_val = int(self.ptp_clock_tod.value)
            current_ts_ns = (ptp_val >> 16) & 0xFFFFFFFF  # Extract current PTP time in nanoseconds (assuming TOD format with 16 fractional bits)
            current_slot_id = (current_ts_ns // (self.slot_len * 1000))  # Calculate current slot ID based on PTP time

            if current_slot_id != last_slot_id and self.send_counter != 0:  # New slot and ready to send
                self.send_counter -= 1
                #self.log.info(f"Node {self.node_id} entering slot {current_slot_id}")
                # new slot, send packtet
                last_slot_id = current_slot_id
                if self.state is PacketType.BEFORE_ATTENTION:
                        pkt = self.create_packet(current_slot_id, ETHTYPE_BEFORE_ATTENTION)
                elif self.state is PacketType.AFTER_ATTENTION:
                        pkt = self.create_packet(current_slot_id, ETHTYPE_AFTER_ATTENTION)
                elif self.state is PacketType.BEFORE_MOE:
                        pkt = self.create_packet(current_slot_id, ETHTYPE_BEFORE_MOE)
                elif self.state is PacketType.AFTER_MOE:
                        pkt = self.create_packet(current_slot_id, ETHTYPE_AFTER_MOE)
                else:
                    assert False, f"Unexpected state {self.state} when creating packet at Node {self.node_id}"
                receiver_id = receiver_count%8+1
                if receiver_id == self.node_id:
                    receiver_count += 1
                    receiver_id = receiver_count%8+1
                receiver_count += 1

                self.log.info(f"Node {self.node_id} sent packet for slot {current_slot_id} to {receiver_id}")
                await self.network.receive_packet_node(pkt, sender_id=self.node_id, receiver_id=receiver_id)

                

    async def _stop_consensus_app(self):
        self.log.info("Stopping consensus app runner")
        self._running = False