import logging
from cocotb.log import SimLog

class MoeStats:
    """单个节点的发送统计"""

    def __init__(self, node_id: int):
        self.node_id = node_id
        self.log = SimLog(f"cocotb.stats_{node_id}")
        self.log.setLevel(logging.INFO)

        # --- 全局计数 ---
        self.total_tx: int = 0          # 总发送次数（含首次 + 重传）
        self.total_retransmits: int = 0  # 总重传次数
        self.total_tasks: int = 0        # 完成的发送任务数（每个 target/layer/phase 算一个）

        # --- 细分记录 ---
        # key = (layer_id, phase, target_id), value = attempts（该任务尝试了几次）
        self.task_attempts: dict[tuple[int, int, int], int] = {}

    def record_send(self):
        """每次发出一个数据包时调用"""
        self.total_tx += 1

    def record_task_done(self, layer_id: int, phase: int, target_id: int, attempts: int):
        """
        一个发送任务完成时调用。
        attempts: 该任务总共发了几次（含首次发送）
        重传次数 = attempts - 1
        """
        self.task_attempts[(layer_id, phase, target_id)] = attempts
        self.total_retransmits += max(0, attempts - 1)
        self.total_tasks += 1

    def summary(self) -> str:
        """返回本节点的统计摘要字符串"""
        lines = [
            f"===== Node {self.node_id} Stats =====",
            f"  Total TX packets : {self.total_tx}",
            f"  Total tasks      : {self.total_tasks}",
            f"  Total retransmits: {self.total_retransmits}",
            f"  First-try success: {self.total_tasks - self._retransmit_task_count()}/{self.total_tasks}",
        ]

        # 列出有重传的任务
        retransmit_tasks = {k: v for k, v in self.task_attempts.items() if v > 1}
        if retransmit_tasks:
            lines.append(f"  Tasks with retransmit ({len(retransmit_tasks)}):")
            for (layer, phase, target), att in sorted(retransmit_tasks.items()):
                lines.append(
                    f"    Layer {layer} Phase {phase} -> Node {target}: "
                    f"{att} attempts ({att - 1} retransmit(s))"
                )
        else:
            lines.append("  No retransmits — all tasks succeeded on first try")

        return "\n".join(lines)

    def print_summary(self):
        """打印本节点统计到 cocotb log"""
        self.log.info("\n" + self.summary())

    def _retransmit_task_count(self) -> int:
        """有过重传的任务数"""
        return sum(1 for v in self.task_attempts.values() if v > 1)


def print_global_summary(stats_list: list["MoeStats"]):
    """
    汇总所有节点的统计并打印。
    stats_list: 所有 GpuNode 的 MoeStats 实例列表。
    """
    log = SimLog("cocotb.stats_global")
    log.setLevel(logging.INFO)

    total_tx = sum(s.total_tx for s in stats_list)
    total_retransmits = sum(s.total_retransmits for s in stats_list)
    total_tasks = sum(s.total_tasks for s in stats_list)
    first_try_tasks = sum(s.total_tasks - s._retransmit_task_count() for s in stats_list)

    lines = [
        "",
        "=" * 50,
        "         GLOBAL MOE COMMUNICATION STATS",
        "=" * 50,
        f"  Nodes              : {len(stats_list)}",
        f"  Total TX packets   : {total_tx}",
        f"  Total tasks        : {total_tasks}",
        f"  Total retransmits  : {total_retransmits}",
        f"  First-try success  : {first_try_tasks}/{total_tasks}"
        + (f" ({first_try_tasks/total_tasks*100:.1f}%)" if total_tasks > 0 else ""),
        f"  Retransmit rate    : "
        + (f"{total_retransmits/total_tx*100:.2f}%" if total_tx > 0 else "N/A"),
        "=" * 50,
    ]

    # 每个节点一行摘要
    lines.append("  Per-node breakdown:")
    for s in sorted(stats_list, key=lambda x: x.node_id):
        lines.append(
            f"    Node {s.node_id}: "
            f"tx={s.total_tx}, retransmits={s.total_retransmits}, "
            f"tasks={s.total_tasks}"
        )
    lines.append("=" * 50)

    log.info("\n".join(lines))