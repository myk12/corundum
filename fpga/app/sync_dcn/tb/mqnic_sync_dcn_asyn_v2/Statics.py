import logging
from cocotb.log import SimLog


class MoeStats:
    """单个节点的发送统计（支持分片）"""

    def __init__(self, node_id: int):
        self.node_id = node_id
        self.log = SimLog(f"cocotb.stats_{node_id}")
        self.log.setLevel(logging.INFO)

        # --- 全局计数 ---
        self.total_tx: int = 0              # 总发送的网络包数（每个分片每次发送/重传各算一个）
        self.total_frag_retransmits: int = 0 # 分片级重传次数（某分片发了 3 次 → 重传 2 次）
        self.total_tasks: int = 0            # 完成的发送任务数（每个 target/layer/phase 算一个）

        # --- 细分记录 ---
        # key = (layer_id, phase, target_id)
        # value = dict with:
        #   'total_attempts': 所有分片的发送次数之和
        #   'total_frags': 分片数
        #   'retransmit_frags': 有过重传的分片数量
        #   'frag_details': dict[frag_id -> attempts] （可选，用于细粒度分析）
        self.task_records: dict[tuple[int, int, int], dict] = {}

    def record_task_done(self, layer_id: int, phase: int, target_id: int,
                         frag_attempts: dict[int, int]):
        """
        一个发送任务（某 target 的某 layer/phase）的所有分片全部完成时调用。

        frag_attempts: dict[frag_id -> attempts]
            每个分片实际发了几次（含首次发送）。
            例如 {0: 1, 1: 3, 2: 1, ...} 表示分片 1 重传了 2 次。
        """
        total_attempts = sum(frag_attempts.values())
        total_frags = len(frag_attempts)
        retransmit_frags = sum(1 for v in frag_attempts.values() if v > 1)
        frag_retransmits = sum(max(0, v - 1) for v in frag_attempts.values())

        self.task_records[(layer_id, phase, target_id)] = {
            'total_attempts': total_attempts,
            'total_frags': total_frags,
            'retransmit_frags': retransmit_frags,
            'frag_retransmits': frag_retransmits,
            'frag_details': dict(frag_attempts),
        }

        self.total_tx += total_attempts
        self.total_frag_retransmits += frag_retransmits
        self.total_tasks += 1

    def summary(self) -> str:
        """返回本节点的统计摘要字符串"""
        perfect_tasks = self._perfect_task_count()
        lines = [
            f"===== Node {self.node_id} Stats =====",
            f"  Total TX packets      : {self.total_tx}",
            f"  Total tasks           : {self.total_tasks}",
            f"  Total frag retransmits: {self.total_frag_retransmits}",
            f"  Perfect tasks (0 retx): {perfect_tasks}/{self.total_tasks}",
        ]

        # 列出有重传的任务
        retransmit_tasks = {
            k: v for k, v in self.task_records.items()
            if v['frag_retransmits'] > 0
        }
        if retransmit_tasks:
            lines.append(f"  Tasks with retransmit ({len(retransmit_tasks)}):")
            for (layer, phase, target), rec in sorted(retransmit_tasks.items()):
                lines.append(
                    f"    Layer {layer} Phase {phase} -> Node {target}: "
                    f"{rec['total_attempts']} sends across {rec['total_frags']} frags, "
                    f"{rec['retransmit_frags']} frags retransmitted, "
                    f"{rec['frag_retransmits']} total retransmit(s)"
                )
        else:
            lines.append("  No retransmits — all fragments succeeded on first try")

        return "\n".join(lines)

    def print_summary(self):
        """打印本节点统计到 cocotb log"""
        self.log.info("\n" + self.summary())

    def _perfect_task_count(self) -> int:
        """所有分片都一次成功的任务数"""
        return sum(
            1 for v in self.task_records.values()
            if v['frag_retransmits'] == 0
        )


def print_global_summary(stats_list: list["MoeStats"]):
    """
    汇总所有节点的统计并打印。
    stats_list: 所有 GpuNode 的 MoeStats 实例列表。
    """
    log = SimLog("cocotb.stats_global")
    log.setLevel(logging.INFO)

    total_tx = sum(s.total_tx for s in stats_list)
    total_frag_retransmits = sum(s.total_frag_retransmits for s in stats_list)
    total_tasks = sum(s.total_tasks for s in stats_list)
    perfect_tasks = sum(s._perfect_task_count() for s in stats_list)

    # 计算预期的最少发送数（无重传情况）
    # 每个任务有 total_frags 个分片，每个分片至少发一次
    expected_min_tx = sum(
        rec['total_frags']
        for s in stats_list
        for rec in s.task_records.values()
    )

    lines = [
        "",
        "=" * 60,
        "           GLOBAL MOE COMMUNICATION STATS",
        "=" * 60,
        f"  Nodes                  : {len(stats_list)}",
        f"  Total TX packets       : {total_tx}",
        f"  Expected min TX        : {expected_min_tx}  (if 0 retransmits)",
        f"  Total tasks            : {total_tasks}",
        f"  Total frag retransmits : {total_frag_retransmits}",
        f"  Perfect tasks (0 retx) : {perfect_tasks}/{total_tasks}"
        + (f" ({perfect_tasks/total_tasks*100:.1f}%)" if total_tasks > 0 else ""),
        f"  Retransmit overhead    : "
        + (f"{total_frag_retransmits/expected_min_tx*100:.2f}%"
           if expected_min_tx > 0 else "N/A"),
        "=" * 60,
    ]

    # 每个节点一行摘要
    lines.append("  Per-node breakdown:")
    for s in sorted(stats_list, key=lambda x: x.node_id):
        lines.append(
            f"    Node {s.node_id}: "
            f"tx={s.total_tx}, frag_retx={s.total_frag_retransmits}, "
            f"tasks={s.total_tasks}, perfect={s._perfect_task_count()}"
        )
    lines.append("=" * 60)

    log.info("\n".join(lines))
