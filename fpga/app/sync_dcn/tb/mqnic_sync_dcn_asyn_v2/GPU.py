import struct
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

        # --- 可靠传输状态（按 peer 分别维护） ---
        # ACK 唤醒事件：每个 peer 一个
        self.ack_events = {i: Event() for i in self.peer_ids}

        # 接收去重：记录已处理过的 (src, layer, phase) 三元组
        self.processed_set: set[tuple[int, int, int]] = set()

        # 当前正在发送的 (layer_id, phase) —— 按目标节点索引
        # 用于校验回来的 ACK 是否匹配当前发送任务
        self.current_sending_tag: dict[int, tuple[int, int]] = {i: (-1, -1) for i in self.peer_ids}

        # --- 接收 barrier ---
        # key = (layer_id, phase)，value = set of src_ids already received
        self._rx_sets: dict[tuple[int, int], set[int]] = {}
        # 当某个 (layer_id, phase) 收齐 7 个 peer 时触发
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
        """
        将 (layer_id, phase) 编码为 64-bit round_id。
        高 32 位 = layer_id，低 32 位 = phase。
        这样 round_id 同时携带层号和阶段信息，ACK 可以精确匹配。
        """
        return (layer_id << 32) | (phase & 0xFFFFFFFF)

    @staticmethod
    def _decode_round(round_id: int) -> tuple[int, int]:
        """round_id -> (layer_id, phase)"""
        layer_id = (round_id >> 32) & 0xFFFFFFFF
        phase = round_id & 0xFFFFFFFF
        return layer_id, phase

    def _get_rx_barrier(self, layer_id: int, phase: int) -> Event:
        """获取或创建某个 (layer, phase) 的接收 barrier event"""
        key = (layer_id, phase)
        if key not in self._rx_barrier_events:
            self._rx_barrier_events[key] = Event()
            self._rx_sets[key] = set()
        return self._rx_barrier_events[key]

    def create_packet(self, target_node_id, ptype, round_id=0):
        eth_header = Ether(src=self.node_mac, dst="ff:ff:ff:ff:ff:ff", type=ptype)
        header = struct.pack("!QBBB", round_id, self.node_id, 0x01, target_node_id)

        if ptype == ETHTYPE_MOE_DATA:
            payload = bytes(PAYLOAD_BYTES_PER_TARGET)
        else:
            payload = b''

        return eth_header / Raw(header + payload)

    # ----------------------------------------------------------------
    #  接收路径
    # ----------------------------------------------------------------
    async def recv_packet(self, pkt: Ether, in_port: int):
        """处理接收到的包：区分数据包和 ACK"""
        assert self.port == in_port, f"Packet received on unexpected port {in_port} (expected {self.port})"
        raw_data = bytes(pkt[Raw])
        round_id, src_id, ack_vec, target_id = struct.unpack("!QBBB", raw_data[:11])
        layer_id, phase = self._decode_round(round_id)

        # 模拟硬件处理延迟
        await Timer(HOP_DELAY_NS, 'ns')

        # --- 情况 A: 收到数据包 ---
        if pkt.type == ETHTYPE_MOE_DATA:
            phase_name = MoePhase(phase).name
            self.log.debug(
                f"RX DATA from Node {src_id} | Layer {layer_id} Phase {phase_name}"
            )

            # 1. 无论是否重复，都立即回传 ACK（发送方可能没收到上一次的 ACK）
            ack_pkt = self.create_packet(src_id, ETHTYPE_CONSENSUS, round_id=round_id)
            cocotb.start_soon(self.network.receive_packet(ack_pkt, self.port))

            # 2. 幂等性去重
            dedup_key = (src_id, layer_id, phase)
            if dedup_key in self.processed_set:
                self.log.debug(
                    f"Duplicate from Node {src_id} Layer {layer_id} {phase_name} — ACK resent, data ignored"
                )
                return

            # 3. 标记已处理 & 执行业务逻辑
            self.processed_set.add(dedup_key)
            await self.process_data_logic(src_id, layer_id, phase)

            # 4. 更新接收 barrier
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
                expected_tag = self.current_sending_tag.get(src_id, (-1, -1))
                if (layer_id, phase) == expected_tag:
                    phase_name = MoePhase(phase).name
                    self.log.info(
                        f"Valid ACK from Node {src_id} | Layer {layer_id} Phase {phase_name}"
                    )
                    self.ack_events[src_id].set()
                else:
                    self.log.debug(
                        f"Stale ACK from Node {src_id} round={round_id}, "
                        f"expected tag={expected_tag}"
                    )

    async def process_data_logic(self, src_id, layer_id, phase):
        """收到新数据后的业务逻辑占位"""
        pass

    # ----------------------------------------------------------------
    #  发送路径：可靠单播 + 重传
    # ----------------------------------------------------------------
    async def _sender_worker(self, target_id: int, layer_id: int, phase: int):
        """针对单个目标的可靠发送（带超时重传）"""
        round_id = self._encode_round(layer_id, phase)
        self.current_sending_tag[target_id] = (layer_id, phase)
        attempts = 0

        while True:
            self.ack_events[target_id].clear()

            pkt = self.create_packet(target_id, ETHTYPE_MOE_DATA, round_id=round_id)
            await self.network.receive_packet(pkt, self.port)
            attempts += 1
            self.stats.record_send()

            try:
                await with_timeout(self.ack_events[target_id].wait(), TIMEOUT_NS, 'ns')
                phase_name = MoePhase(phase).name
                self.log.info(
                    f"TX OK -> Node {target_id} | Layer {layer_id} {phase_name} "
                    f"({attempts} attempt(s))"
                )
                self.stats.record_task_done(layer_id, phase, target_id, attempts)
                break
            except cocotb.result.SimTimeoutError:
                self.log.warning(
                    f"TIMEOUT [Layer {layer_id} Phase {MoePhase(phase).name} -> Node {target_id}] "
                    f"Retry #{attempts}"
                )

    # ----------------------------------------------------------------
    #  All-to-All 的单个阶段
    # ----------------------------------------------------------------
    async def _run_all_to_all_phase(self, layer_id: int, phase: MoePhase):
        """
        对其余 7 个节点并发执行可靠发送，并等待：
          1. 发送侧：7 路全部收到 ACK
          2. 接收侧：收齐其余 7 个节点发来的本 phase 数据
        两个条件都满足后才返回（双向 barrier）。
        """
        phase_name = phase.name
        self.log.info(f"  All-to-All {phase_name} START (Layer {layer_id})")

        # 提前创建接收 barrier event（避免在数据先于发送到达时丢失信号）
        rx_event = self._get_rx_barrier(layer_id, int(phase))

        # --- 发送侧：并发向 7 个 peer 可靠发送 ---
        send_tasks = []
        for target_id in self.peer_ids:
            t = cocotb.start_soon(
                self._sender_worker(target_id, layer_id, int(phase))
            )
            send_tasks.append(t)

        # 等所有发送完成（7 路 ACK 全部收到）
        for t in send_tasks:
            await t
        self.log.info(f"  All-to-All {phase_name} TX DONE (Layer {layer_id})")
        # --- 接收侧：等收齐 7 个 peer 的数据 ---
        # 如果已经收齐了，rx_event 已经 set，这里会立刻返回
        await rx_event.wait()
        self.log.info(f"  All-to-All {phase_name} RX DONE (Layer {layer_id})")

        self.log.info(f"  All-to-All {phase_name} DONE  (Layer {layer_id})")

    # ----------------------------------------------------------------
    #  单层 MOE 通信（两阶段）
    # ----------------------------------------------------------------
    async def run_moe_layer(self, layer_id: int):
        """
        执行一层 MOE 通信，严格按顺序：
          Phase 1 — DISPATCH: 向所有节点发射 token
          Phase 2 — COMBINE:  从所有节点回收 token
        COMBINE 必须在 DISPATCH 完成后才开始。
        每个 phase 的"完成"意味着：
          - 自己发出的 7 路数据全部被 ACK（发送完成）
          - 自己也收到了其余 7 个节点发来的本 phase 数据（接收完成）
        """
        self.log.info(f"=== MOE Layer {layer_id} BEGIN ===")
        # Phase 1: Dispatch (发射 token)
        await self._run_all_to_all_phase(layer_id, MoePhase.DISPATCH)
        await Timer(1, 'ms')#moe层间隔
        # Phase 2: Combine (回收 token)
        await self._run_all_to_all_phase(layer_id, MoePhase.COMBINE)

        self.log.info(f"=== MOE Layer {layer_id} END ===")

    # ----------------------------------------------------------------
    #  多层推理主循环
    # ----------------------------------------------------------------
    async def run_multi_layer_inference(self, total_layers=32):
        """
        模拟大模型推理全过程。

        关键约束：
          第 N 层的 DISPATCH 必须在第 N-1 层的 COMBINE 完成之后才能开始。
          由于 run_moe_layer 内部已是顺序的（DISPATCH -> COMBINE），
          而外层 for 循环逐层 await，天然满足此依赖关系：

          Layer 0: DISPATCH -> COMBINE
          Layer 1: DISPATCH -> COMBINE   （必须等 Layer 0 COMBINE 结束）
          Layer 2: DISPATCH -> COMBINE   （必须等 Layer 1 COMBINE 结束）
          ...

        每个 phase 的"完成"同时包含发送完成和接收完成（双向 barrier），
        确保本节点在进入下一阶段前已拥有所有必要数据。
        """
        self._running = True
        self.log.info(f"====== INFERENCE START ({total_layers} layers) ======")

        for layer_id in range(total_layers):

            # MOE 两阶段通信
            await self.run_moe_layer(layer_id)


        self.log.info("====== INFERENCE FINISHED ======")
        self.stats.print_summary()
        self._running = False
