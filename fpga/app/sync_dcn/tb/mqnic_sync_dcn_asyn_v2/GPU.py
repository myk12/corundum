import struct
import math
import enum
import logging
import cocotb
from cocotb.triggers import Timer, Event, with_timeout
from cocotb.log import SimLog
from cocotb.queue import Queue
from scapy.layers.l2 import Ether
from scapy.packet import Raw
from Constants import TIMEOUT_NS, PAYLOAD_BYTES_PER_TARGET, TOTAL_FRAGMENTS, FRAGMENT_PAYLOAD_SIZE
from Constants import GPU_PORT_RATE_Gbps
from Statics import MoeStats
from cocotb.utils import get_sim_time
from cocotb.triggers import Combine

# --- 协议常量 ---
ETHTYPE_CONSENSUS = 0xAE86  # ACK
ETHTYPE_MOE_DATA  = 0xAE89  # MOE 数据包


class MoePhase(enum.IntEnum):
    """MOE 通信的两个阶段"""
    DISPATCH = 0   # 发射 token（All-to-All 第一步）
    COMBINE  = 1   # 回收 token（All-to-All 第二步）


class GpuNode:
    def __init__(self, node_id, node_mac, port_rate_gbps=GPU_PORT_RATE_Gbps):
        self.node_id = node_id
        self.node_mac = node_mac
        self.port_rate_gbps = port_rate_gbps
        self.log = SimLog(f"cocotb.gpu_{node_id}")
        self.log.setLevel(logging.INFO)

        self.network = None
        self.port = None
        self._running = False

        # --- 端口速率相关 ---
        self.ns_per_byte = 8.0 / self.port_rate_gbps  # 每字节的传输时间(ns)

        # --- 出/入方向的异步队列 ---
        self._tx_queue = Queue()
        self._rx_queue = Queue()

        # --- 其余 7 个节点的 ID 列表 ---
        self.peer_ids = [i for i in range(1, 9) if i != node_id]

        # --- 可靠传输状态（按 peer × frag_id 维护） ---
        self.ack_events: dict[int, dict[int, Event]] = {
            pid: {fid: Event() for fid in range(TOTAL_FRAGMENTS)}
            for pid in self.peer_ids
        }

        self.current_sending_tag: dict[int, dict[int, tuple[int, int]]] = {
            pid: {fid: (-1, -1) for fid in range(TOTAL_FRAGMENTS)}
            for pid in self.peer_ids
        }

        # --- 接收侧：分片跟踪表 ---
        self._rx_frag_tracker: dict[tuple[int, int, int], set[int]] = {}
        self.processed_set: set[tuple[int, int, int]] = set()

        # --- 接收 barrier ---
        self._rx_sets: dict[tuple[int, int], set[int]] = {}
        self._rx_barrier_events: dict[tuple[int, int], Event] = {}

        # --- 统计 ---
        self.stats = MoeStats(node_id)

    # ----------------------------------------------------------------
    #  基础设施
    # ----------------------------------------------------------------
    def install_network(self, network, port):
        self.network = network
        self.port = port
        self.start_port_workers()

    def start_port_workers(self):
        cocotb.start_soon(self._port_worker_out())
        cocotb.start_soon(self._port_worker_in())
        self.log.info(
            f"Port workers started | rate={self.port_rate_gbps}Gbps "
            f"({self.ns_per_byte:.2f} ns/byte)"
        )

    @staticmethod
    def _encode_round(layer_id: int, phase: int) -> int:
        return (layer_id << 32) | (phase & 0xFFFFFFFF)

    @staticmethod
    def _decode_round(round_id: int) -> tuple[int, int]:
        layer_id = (round_id >> 32) & 0xFFFFFFFF
        phase = round_id & 0xFFFFFFFF
        return layer_id, phase

    def _get_rx_barrier(self, layer_id: int, phase: int) -> Event:
        key = (layer_id, phase)
        if key not in self._rx_barrier_events:
            self._rx_barrier_events[key] = Event()
            self._rx_sets[key] = set()
        return self._rx_barrier_events[key]

    def _compute_serialization_delay_ns(self, pkt) -> int:
        # FIX4: 返回整数纳秒，避免浮点 Timer 事件堆积拖慢仿真
        pkt_size_bytes = len(bytes(pkt))
        return int(pkt_size_bytes * self.ns_per_byte)

    def create_packet(self, target_node_id, ptype, round_id=0,
                      frag_id=0, total_frags=1):
        eth_header = Ether(src=self.node_mac, dst="ff:ff:ff:ff:ff:ff", type=ptype)
        # 格式: Q(8) B(1) B(1) B(1) H(2) H(2) = 15 字节
        header = struct.pack("!QBBBHH",
                             round_id, self.node_id, 0x01, target_node_id,
                             frag_id, total_frags)

        if ptype == ETHTYPE_MOE_DATA:
            offset = frag_id * FRAGMENT_PAYLOAD_SIZE
            remaining = PAYLOAD_BYTES_PER_TARGET - offset
            this_frag_size = min(FRAGMENT_PAYLOAD_SIZE, remaining)
            payload = bytes(this_frag_size)
        else:
            payload = b''

        return eth_header / Raw(header + payload)

    # ----------------------------------------------------------------
    #  端口 Worker：出方向（TX）
    # ----------------------------------------------------------------
    async def _port_worker_out(self):
        while True:
            pkt = await self._tx_queue.get()
            #self.log.warning(f"worker_out ")
            
            transfer_delay_ns = self._compute_serialization_delay_ns(pkt)
            await Timer(transfer_delay_ns, 'ns')
            self.network.receive_packet(pkt, self.port)

    async def _enqueue_tx(self, pkt):
        
        await self._tx_queue.put(pkt)

    # ----------------------------------------------------------------
    #  端口 Worker：入方向（RX）
    # ----------------------------------------------------------------
    async def _port_worker_in(self):
        while True:
            pkt, in_port = await self._rx_queue.get()
            transfer_delay_ns = self._compute_serialization_delay_ns(pkt)
            await Timer(transfer_delay_ns, 'ns')
            await self._process_rx_packet(pkt, in_port)

    # ----------------------------------------------------------------
    #  接收路径（外部入口 -> 入队）
    # ----------------------------------------------------------------
    def recv_packet(self, pkt: Ether, in_port: int):
        assert self.port == in_port, (
            f"Packet received on unexpected port {in_port} "
            f"(expected {self.port}), gpu {self.node_id}"
        )
        self._rx_queue.put_nowait((pkt, in_port))

    # ----------------------------------------------------------------
    #  接收路径（实际处理逻辑）
    # ----------------------------------------------------------------
    async def _process_rx_packet(self, pkt: Ether, in_port: int):
        raw_data = bytes(pkt[Raw])

        # FIX1: 解包格式与 create_packet 保持一致：!QBBBHH = 15字节
        round_id, src_id, ack_vec, target_id, frag_id, total_frags = \
            struct.unpack("!QBBBHH", raw_data[:15])

        layer_id, phase = self._decode_round(round_id)

        # --- 情况 A: 收到数据包 ---
        if pkt.type == ETHTYPE_MOE_DATA:
            phase_name = MoePhase(phase).name
            self.log.debug(
                f"RX DATA from Node {src_id} | Layer {layer_id} "
                f"Phase {phase_name} frag {frag_id}/{total_frags}"
            )

            # 1. 回传 ACK（await 修复 FIX2）
            ack_pkt = self.create_packet(
                src_id, ETHTYPE_CONSENSUS, round_id=round_id,
                frag_id=frag_id, total_frags=total_frags
            )
            await self._enqueue_tx(ack_pkt)

            # 2. 如果该 peer 的该轮次已经整体完成，跳过
            dedup_key = (src_id, layer_id, phase)
            if dedup_key in self.processed_set:
                return

            # 3. 分片级去重 & 记录
            if dedup_key not in self._rx_frag_tracker:
                self._rx_frag_tracker[dedup_key] = set()

            frag_set = self._rx_frag_tracker[dedup_key]
            if frag_id in frag_set:
                return

            frag_set.add(frag_id)

            # 4. 检查是否该 peer 的所有分片都到齐了
            if len(frag_set) < total_frags:
                return

            # --- 所有分片到齐 ---
            self.processed_set.add(dedup_key)
            self.log.info(
                f"COMPLETE from Node {src_id} | Layer {layer_id} "
                f"{phase_name} ({total_frags} fragments reassembled)"
            )

            await self.process_data_logic(src_id, layer_id, phase)

            # 5. 更新接收 barrier
            key = (layer_id, phase)
            if key not in self._rx_sets:
                self._rx_sets[key] = set()
            if key not in self._rx_barrier_events:
                self._rx_barrier_events[key] = Event()

            self._rx_sets[key].add(src_id)
            if len(self._rx_sets[key]) >= len(self.peer_ids):
                self.log.info(
                    f"RX BARRIER met: Layer {layer_id} {phase_name} — "
                    f"{len(self.peer_ids)} peers"
                )
                self._rx_barrier_events[key].set()

        # --- 情况 B: 收到 ACK ---
        elif pkt.type == ETHTYPE_CONSENSUS:
            if src_id in self.ack_events:
                expected_tag = self.current_sending_tag.get(src_id, {}).get(frag_id, (-1, -1))
                if (layer_id, phase) == expected_tag:
                    self.ack_events[src_id][frag_id].set()

    async def process_data_logic(self, src_id, layer_id, phase):
        pass

    # ----------------------------------------------------------------
    #  发送路径：单目标所有分片可靠发送 + 重传
    # ----------------------------------------------------------------
    async def _sender_worker(self, target_id: int, layer_id: int, phase: int):
        round_id = self._encode_round(layer_id, phase)
        pending = set(range(TOTAL_FRAGMENTS))
        frag_attempts = {fid: 0 for fid in range(TOTAL_FRAGMENTS)}
        frag_start_time = {}

        for fid in pending:
            self.current_sending_tag[target_id][fid] = (layer_id, phase)
            self.ack_events[target_id][fid].clear()

        while pending:
            # --- 1) 把所有 pending 分片各发一次 ---
            for fid in sorted(pending):
                self.ack_events[target_id][fid].clear()
                pkt = self.create_packet(
                    target_id, ETHTYPE_MOE_DATA, round_id=round_id,
                    frag_id=fid, total_frags=TOTAL_FRAGMENTS
                )
                # FIX2: 必须 await，否则协程不执行，数据包不入队
                await self._enqueue_tx(pkt)
                frag_attempts[fid] += 1
                if fid not in frag_start_time:
                    frag_start_time[fid] = get_sim_time(units='ns')

            # --- 2) 并发等待所有 pending 分片的 ACK，超时则进入重传 ---
            try:
                # self.log.warning(
                #     f"WAITING ACKs from Node {target_id} | Layer {layer_id} "
                #     f"Phase {MoePhase(phase).name}"
                # )
                await with_timeout(
                    self._wait_all_acks(target_id, pending),
                    int(TIMEOUT_NS), 'ns'
                )
            except cocotb.result.SimTimeoutError:
                pass

            # --- 3) 移除已确认的分片 ---
            confirmed = {fid for fid in pending if self.ack_events[target_id][fid].is_set()}
            pending -= confirmed

            if pending:
                self.log.warning(
                    f"RETRY -> Node {target_id} | Layer {layer_id} "
                    f"Phase {MoePhase(phase).name} | "
                    f"pending frags: {sorted(pending)}"
                )

        # --- 统计 ---
        now = get_sim_time(units='ns')
        frag_elapsed = {fid: (now - frag_start_time[fid]) / 1e6 for fid in range(TOTAL_FRAGMENTS)}
        total_attempts = sum(frag_attempts.values())
        retransmits = total_attempts - TOTAL_FRAGMENTS
        avg_delay = sum(frag_elapsed.values()) / TOTAL_FRAGMENTS
        max_delay = max(frag_elapsed.values())

        phase_name = MoePhase(phase).name
        self.log.info(
            f"TX ALL {TOTAL_FRAGMENTS} frags OK -> Node {target_id} "
            f"| Layer {layer_id} {phase_name} "
            f"({total_attempts} sends, {retransmits} retransmits)"
            f" | Max delay: {max_delay:.2f} ms"
        )
        self.stats.record_task_done(layer_id, phase, target_id, frag_attempts)

    async def _wait_all_acks(self, target_id: int, pending: set[int]):
        triggers = [self.ack_events[target_id][fid].wait() for fid in pending]
        self.log.warning(
            f"monitoring ACKs from Node {target_id} | " )

        if triggers:
            # Combine 是并行的，不会卡在第一个 fid 上
            # 它会在底层一次性监控所有事件
            await Combine(*triggers)

    # ----------------------------------------------------------------
    #  All-to-All 的单个阶段
    # ----------------------------------------------------------------
    async def _run_all_to_all_phase(self, layer_id: int, phase: MoePhase):
        phase_name = phase.name
        self.log.info(
            f"  All-to-All {phase_name} START (Layer {layer_id}) "
            f"[{TOTAL_FRAGMENTS} frags/target, "
            f"{FRAGMENT_PAYLOAD_SIZE}B/frag]"
        )

        rx_event = self._get_rx_barrier(layer_id, int(phase))

        send_tasks = []
        for target_id in self.peer_ids:
            t = cocotb.start_soon(
                self._sender_worker(target_id, layer_id, int(phase))
            )
            send_tasks.append(t)

        for t in send_tasks:
            await t
        self.log.info(f"  All-to-All {phase_name} TX DONE (Layer {layer_id})")

        await rx_event.wait()
        self.log.info(f"  All-to-All {phase_name} RX DONE (Layer {layer_id})")
        self.log.info(f"  All-to-All {phase_name} DONE  (Layer {layer_id})")

    # ----------------------------------------------------------------
    #  单层 MOE 通信（两阶段）
    # ----------------------------------------------------------------
    async def run_moe_layer(self, layer_id: int):
        self.log.info(f"=== MOE Layer {layer_id} BEGIN ===")
        await self._run_all_to_all_phase(layer_id, MoePhase.DISPATCH)
        await Timer(1, 'ms')
        await self._run_all_to_all_phase(layer_id, MoePhase.COMBINE)
        self.log.info(f"=== MOE Layer {layer_id} END ===")

    # ----------------------------------------------------------------
    #  多层推理主循环
    # ----------------------------------------------------------------
    async def run_multi_layer_inference(self, total_layers=32):
        self._running = True
        self.log.info(
            f"====== INFERENCE START ({total_layers} layers) ======\n"
            f"  Fragment config: {PAYLOAD_BYTES_PER_TARGET}B total -> "
            f"  {TOTAL_FRAGMENTS} frags x {FRAGMENT_PAYLOAD_SIZE}B\n"
            f"  Port rate: {self.port_rate_gbps} Gbps "
            f"({self.ns_per_byte:.2f} ns/byte)"
        )

        for layer_id in range(total_layers):
            await self.run_moe_layer(layer_id)

        self.log.info("====== INFERENCE FINISHED ======")
        self.stats.print_summary()
        self._running = False
