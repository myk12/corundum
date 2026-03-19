from asyncio import Queue
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

from Constants import *
from collections import deque
from cocotb.triggers import Event

class CocotbQueue:
    """兼容 cocotb 协程调度器的异步队列"""
    def __init__(self):
        self._queue = deque()
        self._event = Event()

    def put_nowait(self, item):
        self._queue.append(item)
        self._event.set()  # 通知消费者

    async def get(self):
        while not self._queue:
            self._event.clear()
            await self._event.wait()
        return self._queue.popleft()

    def empty(self):
        return len(self._queue) == 0

    def qsize(self):
        return len(self._queue)
def parse_packet(pkt):
        # 提取 Raw 层数据
        raw_data = bytes(pkt[Raw])
        
        # 根据封装时的格式 "!QBBB" (11字节) 进行解包
        header_size = 11
        header_data = raw_data[:header_size]
        
        slot_id, node_id, ack_vec, target_node_id = struct.unpack("!QBBB", header_data)
        return target_node_id
class Tofino:
    def __init__(self, port_set, spine, router_table,leaf_id):
        self.port_set = port_set
        self.spine = spine
        self.router_table = router_table
        self.leaf_id = leaf_id

        # 状态记录
        self.current_buffer_usage = 0  # 当前总缓冲区占用量 (Bytes)
        
        self.port_queues_out = {port: CocotbQueue() for port in port_set}
        self.port_queues_in = {port: CocotbQueue() for port in port_set}
        
        # 为每个端口启动一个独立的处理协程 (Consumer)
        for port in port_set:
            cocotb.start_soon(self._port_worker_in(port))
            cocotb.start_soon(self._port_worker_out(port))
    def add_queue_in(self, pkt: Ether, in_port):
        """
        生产者：包到达交换机，进入端口进行转发
        """
        # 1. 将包放入对应端口的队列
        # 使用 put_nowait 因为我们已经在上面手动检查了 buffer 限制
        self.port_queues_in[in_port].put_nowait(pkt)
        return True
    def add_queue_out(self, pkt: Ether, out_port):
        """
        生产者：包到达交换机，尝试进入对应出去端口的缓冲区
        """
        pkt_size_bytes = len(bytes(pkt))
        
        # 1. 检查全局缓冲区溢出
        if self.current_buffer_usage + pkt_size_bytes > QUEUE_SIZE_BYTES:
            self.spine.log.warning(f"Buffer overflow at Spine {self.spine.Spine_id} Leaf {self.leaf_id} for port {out_port}: "
                                   f"Current usage: {self.current_buffer_usage}, Packet size: {pkt_size_bytes}")
            return False
        
        # 2. 占用缓冲区空间
        self.current_buffer_usage += pkt_size_bytes
        
        # 3. 将包放入对应端口的队列
        # 使用 put_nowait 因为我们已经在上面手动检查了 buffer 限制
        self.port_queues_out[out_port].put_nowait(pkt)
        return True

    async def _port_worker_in(self, port):
        """
        消费者：每个端口独立的硬件处理单元
        模拟串行输出：一个包传完，才传下一个
        """
        # 计算 400Gbps 下 1 字节需要的纳秒数
        # 400 Gbps = 50 GB/s = 50 Bytes/ns
        # 所以 1 Byte 需要 1/50 ns = 0.02 ns
        ns_per_byte = 8.0 / PROT_RATE_Gbps 
        
        while True:
            # 1. 等待队列中有包 (由事件驱动，不耗 CPU)
            pkt = await self.port_queues_in[port].get()
            pkt_size_bytes = len(bytes(pkt))
            
            # 2. 模拟物理层传输延迟 (Serialization Delay)
            # 传输延迟 = 包大小(Bytes) * 每字节传输耗时
            transfer_delay_ns = pkt_size_bytes * ns_per_byte
            await Timer(transfer_delay_ns, 'ns')
            out_port = self.select_out_port(pkt)#选择输出端口
            # 4. 调用下游接收函数 (假设 leaf 对象已绑定)
            if out_port in self.port_set:
                self.add_queue_out(pkt, out_port)
            else:
                # 需要通过另一个 leaf 转发,用start_soon异步调用transfer_inside_spine
                cocotb.start_soon(self.spine.transfer_inside_spine(pkt, self.leaf_id, out_port))
    async def _port_worker_out(self, port):
        """
        消费者：每个端口独立的硬件处理单元
        模拟串行输出：一个包传完，才传下一个
        """
        while True:
            # 1. 等待队列中有包 (由事件驱动，不耗 CPU)
            pkt = await self.port_queues_out[port].get()
            
            # 2. 模拟物理层传输延迟 (Serialization Delay)
            pkt_size_bytes = len(bytes(pkt))
            ns_per_byte = 8.0 / PROT_RATE_Gbps 
            transfer_delay_ns = pkt_size_bytes * ns_per_byte
            await Timer(transfer_delay_ns, 'ns')
            #减少当前缓冲区占用
            self.current_buffer_usage -= pkt_size_bytes
            
            # 3. 调用下游接收函数 (假设 leaf 对象已绑定)
            await self.spine.send_packet(pkt, port)


    def select_out_port(self, pkt):
        #获取目标节点ID
        target_node_id = parse_packet(pkt)
        #根据路由表选择输出端口 
        out_ports = self.router_table.get(target_node_id)
        assert out_ports is not None, f"Target node ID {target_node_id} not found in routing table of Spine {self.Spine_id}"
        if isinstance(out_ports, list):
            # -- 负载均衡策略选择 --
            return out_ports[self.leaf_id-1]  # 这里简单地选择第一个端口，实际可以根据负载情况动态选择
        # 4. 如果读取到的直接是一个整数（单条路径），直接返回
        #如果out_ports不不在port_set中，说明需要通过另一个leaf转发
        
        return out_ports

       