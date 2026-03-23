import struct
import math
import enum
import logging
import cocotb
from cocotb.triggers import Timer, Event, with_timeout
from cocotb.log import SimLog
from scapy.layers.l2 import Ether
from scapy.packet import Raw
from Constants import HOP_DELAY_NS, TIMEOUT_NS, PAYLOAD_BYTES_PER_TARGET
from Statics import MoeStats

# --- 协议常量 ---
ETHTYPE_CONSENSUS = 0xAE86  # ACK
ETHTYPE_MOE_DATA  = 0xAE89  # MOE 数据包

# --- 分片常量 ---
TOTAL_FRAGMENTS = 128  # 固定分片数，可修改

def compute_fragment_size() -> int:
    """根据总 payload 和分片数计算每个分片的大小"""
    return math.ceil(PAYLOAD_BYTES_PER_TARGET / TOTAL_FRAGMENTS)

FRAGMENT_PAYLOAD_SIZE = compute_fragment_size()


class MoePhase(enum.IntEnum):
    """MOE 通信的两个阶段"""
    DISPATCH = 0   # 发射 token（All-to-All 第一步）
    COMBINE  = 1   # 回收 token（All-to-All 第二步）


class GpuNode:
    def __init__(self, node_id, node_mac):
        self.node_id = node_id
        self.node_mac = node_mac
        self.log = SimLog(f"cocotb.gpu_{node_id}")
        self.log.setLevel(logging.INFO)

        self.network = None
        self.port = None
        self._running = False

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

    def create_packet(self, target_node_id, ptype, round_id=0,
                      frag_id=0, total_frags=1):
        eth_header = Ether(src=self.node_mac, dst="ff:ff:ff:ff:ff:ff", type=ptype)
        header = struct.pack("!QBBBBB",
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
    #  接收路径
    # ----------------------------------------------------------------
    async def recv_packet(self, pkt: Ether, in_port: int):
        assert self.port == in_port, (
            f"Packet received on unexpected port {in_port} "
            f"(expected {self.port}), gpu {self.node_id}"
        )
        raw_data = bytes(pkt[Raw])
        round_id, src_id, ack_vec, target_id, frag_id, total_frags = \
            struct.unpack("!QBBBBB", raw_data[:13])
        layer_id, phase = self._decode_round(round_id)

        await Timer(HOP_DELAY_NS, 'ns')

        # --- 情况 A: 收到数据包 ---
        if pkt.type == ETHTYPE_MOE_DATA:
            phase_name = MoePhase(phase).name
            self.log.debug(
                f"RX DATA from Node {src_id} | Layer {layer_id} "
                f"Phase {phase_name} frag {frag_id}/{total_frags}"
            )

            # 1. 无论是否重复，都回传该分片的 ACK
            ack_pkt = self.create_packet(
                src_id, ETHTYPE_CONSENSUS, round_id=round_id,
                frag_id=frag_id, total_frags=total_frags
            )
            cocotb.start_soon(self.network.receive_packet(ack_pkt, self.port))

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
                    f"received from all {len(self.peer_ids)} peers"
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
    #  发送路径：每个分片独立可靠发送 + 重传
    # ----------------------------------------------------------------
    async def _frag_sender_worker(self, target_id: int, layer_id: int,
                                  phase: int, frag_id: int) -> int:
        """
        针对单个目标的单个分片的可靠发送（带超时重传）。
        返回值：该分片实际发送次数（含首次，≥1）。
        不再内部调用 stats.record_send()，由上层汇总统计。
        """
        round_id = self._encode_round(layer_id, phase)
        self.current_sending_tag[target_id][frag_id] = (layer_id, phase)
        attempts = 0

        while True:
            self.ack_events[target_id][frag_id].clear()

            pkt = self.create_packet(
                target_id, ETHTYPE_MOE_DATA, round_id=round_id,
                frag_id=frag_id, total_frags=TOTAL_FRAGMENTS
            )
            await self.network.receive_packet(pkt, self.port)
            attempts += 1

            try:
                await with_timeout(
                    self.ack_events[target_id][frag_id].wait(),
                    TIMEOUT_NS, 'ns'
                )
                self.log.debug(
                    f"TX frag {frag_id}/{TOTAL_FRAGMENTS} OK -> Node {target_id} "
                    f"| Layer {layer_id} Phase {MoePhase(phase).name} "
                    f"({attempts} attempt(s))"
                )
                return attempts
            except cocotb.result.SimTimeoutError:
                self.log.warning(
                    f"TIMEOUT [Layer {layer_id} Phase {MoePhase(phase).name} "
                    f"-> Node {target_id} frag {frag_id}] Retry #{attempts}"
                )

    async def _sender_worker(self, target_id: int, layer_id: int, phase: int):
        """
        针对单个目标的可靠发送：并发发送所有分片。
        所有分片完成后，汇总每个分片的尝试次数，一次性上报统计。
        """
        frag_tasks = []
        for frag_id in range(TOTAL_FRAGMENTS):
            t = cocotb.start_soon(
                self._frag_sender_worker(target_id, layer_id, phase, frag_id)
            )
            frag_tasks.append((frag_id, t))

        # 收集每个分片的实际尝试次数
        frag_attempts: dict[int, int] = {}
        for frag_id, t in frag_tasks:
            attempts = await t
            frag_attempts[frag_id] = attempts

        total_attempts = sum(frag_attempts.values())
        retransmits = total_attempts - TOTAL_FRAGMENTS
        phase_name = MoePhase(phase).name
        self.log.info(
            f"TX ALL {TOTAL_FRAGMENTS} frags OK -> Node {target_id} "
            f"| Layer {layer_id} {phase_name} "
            f"({total_attempts} sends, {retransmits} retransmits)"
        )

        # 一次性上报：传入 frag_id -> attempts 字典
        self.stats.record_task_done(layer_id, phase, target_id, frag_attempts)

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
            f"{TOTAL_FRAGMENTS} frags x {FRAGMENT_PAYLOAD_SIZE}B"
        )

        for layer_id in range(total_layers):
            await self.run_moe_layer(layer_id)

        self.log.info("====== INFERENCE FINISHED ======")
        self.stats.print_summary()
        self._running = False
