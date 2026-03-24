
import heapq
import logging
import os
from random import random
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
from cocotb.triggers import Timer, Event, with_timeout
from cocotb.triggers import RisingEdge, FallingEdge, Timer
from cocotb.queue import Queue

from cocotbext.axi import AxiStreamBus
from cocotbext.axi import AxiSlave, AxiBus, SparseMemoryRegion
from cocotbext.eth import EthMac
from cocotbext.pcie.core import RootComplex
from cocotbext.pcie.xilinx.us import UltraScalePlusPcieDevice
from cocotb.utils import get_sim_time

from Constants import *


def parse_packet(pkt):
    raw_data = bytes(pkt[Raw])
    # 扩展到 15 字节: Q (8) + B (1) + B (1) + B (1) + H (2) + H (2) = 15
    header_size = 15 
    header_data = raw_data[:header_size]

    # 将最后两个 B 改为 H (Unsigned Short)
    slot_id, node_id, ack_vec, target_node_id, frag_id, total_frags = \
        struct.unpack("!QBBBHH", header_data)
        
    return target_node_id


class Tofino:
    def __init__(self, port_set, spine, router_table, leaf_id):
        self.port_set = port_set
        self.spine = spine
        self.router_table = router_table
        self.leaf_id = leaf_id

        # 状态记录
        self.current_buffer_usage = 0  # 当前总缓冲区占用量 (Bytes)

        # 使用 cocotb 内置 Queue（无界），内部已正确处理协程同步
        self.port_queues_out = {port: Queue() for port in port_set}
        self.port_queues_in = {port: Queue() for port in port_set}
        self.queues_transfer = Queue()

        # 为每个端口启动一个独立的处理协程 (Consumer)
        cocotb.start_soon(self._transfer_worker())
        for port in port_set:
            cocotb.start_soon(self._port_worker_in(port))
            cocotb.start_soon(self._port_worker_out(port))
        self._delay_heap = []                # 小顶堆: (到期时间ns, 序号, pkt)
        self._delay_seq = 0                  # 打破时间相同时的排序
        self._delay_notify = Event()         # 通知 worker 有新包入堆
        cocotb.start_soon(self._delayed_transfer_worker())

    def add_queue_in(self, pkt: Ether, in_port):
        """
        生产者：包到达交换机，进入端口进行转发
        """
        self.port_queues_in[in_port].put_nowait(pkt)
        return True

    def add_queue_out(self, pkt: Ether, out_port):
        """
        生产者：包到达交换机，尝试进入对应出去端口的缓冲区
        """
        self.port_queues_out[out_port].put_nowait(pkt)
        return True

    async def _transfer_worker(self):
        ns_per_byte = 8.0 / (PROT_RATE_Gbps * PORT_NUM)
        while True:
            pkt = await self.queues_transfer.get()
            pkt_size_bytes = len(bytes(pkt))
            transfer_delay_ns = pkt_size_bytes * ns_per_byte
            await Timer(int(transfer_delay_ns), 'ns')
            # 减少当前缓冲区占用
            self.current_buffer_usage -= pkt_size_bytes
            out_port = self.select_out_port(pkt)
            if out_port in self.port_set:
                self.add_queue_out(pkt, out_port)
            else:
                assert True, (
                    f"Packet with target node ID {parse_packet(pkt)} "
                    f"at Spine {self.spine.Spine_id} Leaf {self.leaf_id} "
                    f"needs to be transferred to another leaf for output port {out_port}"
                )

    def add_queue_transfer(self, pkt: Ether):
        pkt_size_bytes = len(bytes(pkt))
        if self.current_buffer_usage + pkt_size_bytes > QUEUE_SIZE_BYTES:
            self.spine.log.warning(
                f"Packet with target node ID {parse_packet(pkt)} at Spine {self.spine.Spine_id} Leaf {self.leaf_id} dropped due to buffer overflow "
                f"(current usage {self.current_buffer_usage} bytes, packet size {pkt_size_bytes} bytes, limit {QUEUE_SIZE_BYTES} bytes)"
            )
            return False

        self.current_buffer_usage += pkt_size_bytes

        # 计算到期时间：当前时间 + 随机延迟
        if self.current_buffer_usage == pkt_size_bytes:
            self.queues_transfer.put_nowait(pkt)
            return True
        else:
            delay_ns = 2_000_000  # 2ms, 也可以用 random.uniform(...)

        now = get_sim_time(units='ns')
        due_time = now + delay_ns

        self._delay_seq += 1
        heapq.heappush(self._delay_heap, (due_time, self._delay_seq, pkt))
        self._delay_notify.set()  # 唤醒 worker

        return True

    async def _delayed_transfer_worker(self):
        """
        单协程：不断检查堆顶，sleep 到最早的到期时间，然后出队。
        无论多少包在等待，只有这一个协程在运行。
        """
        while True:
            # 堆空时等待
            while not self._delay_heap:
                self._delay_notify.clear()
                await self._delay_notify.wait()

            due_time, _, _ = self._delay_heap[0]
            now = get_sim_time(units='ns')
            wait_ns = due_time - now

            if wait_ns > 0:
                self._delay_notify.clear()
                timer_trigger = Timer(max(1, round(wait_ns)), 'ns')
                notify_trigger = self._delay_notify.wait()
                await cocotb.triggers.First(timer_trigger, notify_trigger)
                continue

            # 一次性把所有已到期的包全部弹出
            now = get_sim_time(units='ns')
            while self._delay_heap and self._delay_heap[0][0] <= now:
                _, _, pkt = heapq.heappop(self._delay_heap)
                self.queues_transfer.put_nowait(pkt)

    async def _port_worker_in(self, port):
        """
        消费者：每个端口独立的硬件处理单元
        模拟串行输入：一个包收完，才收下一个
        """
        ns_per_byte = 8.0 / PROT_RATE_Gbps

        while True:
            pkt = await self.port_queues_in[port].get()
            pkt_size_bytes = len(bytes(pkt))

            # 模拟物理层传输延迟 (Serialization Delay)
            transfer_delay_ns = pkt_size_bytes * ns_per_byte
            await Timer(int(transfer_delay_ns), 'ns')
            self.add_queue_transfer(pkt)

    async def _port_worker_out(self, port):
        """
        消费者：每个端口独立的硬件处理单元
        模拟串行输出：一个包传完，才传下一个
        """
        ns_per_byte = 8.0 / PROT_RATE_Gbps

        while True:
            pkt = await self.port_queues_out[port].get()
            pkt_size_bytes = len(bytes(pkt))

            # 模拟物理层传输延迟 (Serialization Delay)
            transfer_delay_ns = pkt_size_bytes * ns_per_byte
            await Timer(int(transfer_delay_ns), 'ns')

            # 调用下游接收函数
            await self.spine.send_packet(pkt, port, self.leaf_id)
    

    def select_out_port(self, pkt):
        target_node_id = parse_packet(pkt)
        out_ports = self.router_table.get(target_node_id)
        assert out_ports is not None, (
            f"Target node ID {target_node_id} not found in routing table "
            f"of Spine {self.spine.Spine_id}"
        )
        if isinstance(out_ports, list):
            return out_ports[self.leaf_id - 1]
        return out_ports
