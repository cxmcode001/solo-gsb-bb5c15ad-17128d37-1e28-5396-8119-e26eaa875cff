"""全局流量匹配求解器。

将评审分配问题整体规约为二部 b-匹配，用最大流（Edmonds-Karp）一次求解，
而不是逐稿贪心：

    源点 --(所需评审数)--> 稿件 --(1, 仅当可评审)--> 评审人 --(容量)--> 汇点

- 稿件 -> 评审人 的边仅当：主题能力匹配、无利益冲突、非自稿。
- 最大流 == 总需求 时存在满足全部约束的分配；否则判定不可行并给出缺口诊断。
- 所有节点按 ID 排序后建图，BFS 按固定邻接顺序遍历，算法完全确定：
  相同输入（甚至不同的列表顺序）必然产生完全相同的分配结果。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True)
class ManuscriptSpec:
    id: str
    authors: tuple[str, ...]
    topics: tuple[str, ...]
    required_reviewers: int


@dataclass(frozen=True)
class ReviewerSpec:
    id: str
    topics: tuple[str, ...]
    capacity: int
    conflicted_authors: tuple[str, ...]
    conflicted_manuscripts: tuple[str, ...]


@dataclass(frozen=True)
class UnsatisfiedManuscript:
    manuscript_id: str
    required: int
    assigned: int
    missing: int
    eligible_reviewers: int
    eligible_remaining_capacity: int
    candidate_gap: int
    capacity_gap: int


@dataclass(frozen=True)
class SolveResult:
    feasible: bool
    # manuscript_id -> 已排序的 reviewer id 元组；不可行时为空（不输出部分结果）
    assignments: dict[str, tuple[str, ...]]
    total_required: int
    total_assigned: int
    unsatisfied: tuple[UnsatisfiedManuscript, ...]
    shared_pool: dict | None


class _MaxFlow:
    """Edmonds-Karp 最大流。邻接表按插入顺序遍历，建图顺序确定则结果确定。"""

    def __init__(self, n: int) -> None:
        self.n = n
        self.cap = [[0] * n for _ in range(n)]
        self.adj: list[list[int]] = [[] for _ in range(n)]

    def add_edge(self, u: int, v: int, c: int) -> None:
        if c <= 0:
            return
        self.adj[u].append(v)
        self.adj[v].append(u)
        self.cap[u][v] += c

    def max_flow(self, s: int, t: int) -> int:
        flow = 0
        while True:
            parent = [-1] * self.n
            parent[s] = s
            q = deque([s])
            while q and parent[t] == -1:
                u = q.popleft()
                for v in self.adj[u]:
                    if parent[v] == -1 and self.cap[u][v] > 0:
                        parent[v] = u
                        q.append(v)
            if parent[t] == -1:
                return flow
            bottleneck = None
            v = t
            while v != s:
                u = parent[v]
                c = self.cap[u][v]
                bottleneck = c if bottleneck is None else min(bottleneck, c)
                v = u
            v = t
            while v != s:
                u = parent[v]
                self.cap[u][v] -= bottleneck
                self.cap[v][u] += bottleneck
                v = u
            flow += bottleneck


def _eligible_reviewers(m: ManuscriptSpec, reviewers: list[ReviewerSpec]) -> list[int]:
    """返回可评审该稿件的评审人下标列表（按评审人排序后的顺序）。"""
    m_topics = set(m.topics)
    authors = set(m.authors)
    out: list[int] = []
    for j, r in enumerate(reviewers):
        if r.id in authors:
            continue  # 自稿冲突：评审人是稿件作者
        if authors.intersection(r.conflicted_authors):
            continue  # 利益冲突：与稿件作者冲突
        if m.id in r.conflicted_manuscripts:
            continue  # 利益冲突：直接声明回避该稿件
        if m_topics and not m_topics.intersection(r.topics):
            continue  # 主题能力不匹配
        out.append(j)
    return out


def solve_assignment(
    manuscripts: list[ManuscriptSpec],
    reviewers: list[ReviewerSpec],
) -> SolveResult:
    # 按 ID 排序，保证建图顺序与输入列表顺序无关 -> 输出稳定
    ms = sorted(manuscripts, key=lambda m: m.id)
    rs = sorted(reviewers, key=lambda r: r.id)
    m_count, r_count = len(ms), len(rs)

    SRC = 0
    SNK = 1 + m_count + r_count

    def m_node(i: int) -> int:
        return 1 + i

    def r_node(j: int) -> int:
        return 1 + m_count + j

    eligible = [_eligible_reviewers(m, rs) for m in ms]

    mf = _MaxFlow(SNK + 1)
    for i, m in enumerate(ms):
        mf.add_edge(SRC, m_node(i), m.required_reviewers)
    for i in range(m_count):
        for j in eligible[i]:
            mf.add_edge(m_node(i), r_node(j), 1)
    for j, r in enumerate(rs):
        mf.add_edge(r_node(j), SNK, r.capacity)

    total_required = sum(m.required_reviewers for m in ms)
    total_assigned = mf.max_flow(SRC, SNK)

    # 提取分配：m->r 原始容量为 1，反向残余容量即该边流量
    flow_assignment: dict[str, tuple[str, ...]] = {}
    for i, m in enumerate(ms):
        picked = tuple(
            sorted(rs[j].id for j in eligible[i] if mf.cap[r_node(j)][m_node(i)] > 0)
        )
        flow_assignment[m.id] = picked

    if total_assigned == total_required:
        return SolveResult(
            feasible=True,
            assignments=flow_assignment,
            total_required=total_required,
            total_assigned=total_assigned,
            unsatisfied=(),
            shared_pool=None,
        )

    # ---- 不可行：诊断缺口，不输出任何部分分配 ----
    unsatisfied: list[UnsatisfiedManuscript] = []
    unsat_idx: list[int] = []
    for i, m in enumerate(ms):
        assigned = len(flow_assignment[m.id])
        if assigned >= m.required_reviewers:
            continue
        missing = m.required_reviewers - assigned
        elig = eligible[i]
        remaining = sum(mf.cap[r_node(j)][SNK] for j in elig)
        unsatisfied.append(
            UnsatisfiedManuscript(
                manuscript_id=m.id,
                required=m.required_reviewers,
                assigned=assigned,
                missing=missing,
                eligible_reviewers=len(elig),
                eligible_remaining_capacity=remaining,
                # 候选缺口：合格候选人数本身不足（容量再大也不够）
                candidate_gap=max(0, m.required_reviewers - len(elig)),
                # 容量缺口：候选存在但剩余容量不足
                capacity_gap=max(0, missing - remaining),
            )
        )
        unsat_idx.append(i)

    # 所有未满足稿件共享的候选池：解释“缺口到底缺在哪”
    pool = sorted({j for i in unsat_idx for j in eligible[i]})
    shared_pool = {
        "manuscript_ids": [ms[i].id for i in unsat_idx],
        "reviewer_ids": [rs[j].id for j in pool],
        "missing_total": sum(u.missing for u in unsatisfied),
        "pool_remaining_capacity": sum(mf.cap[r_node(j)][SNK] for j in pool),
        "pool_total_capacity": sum(rs[j].capacity for j in pool),
    }

    return SolveResult(
        feasible=False,
        assignments={},
        total_required=total_required,
        total_assigned=total_assigned,
        unsatisfied=tuple(unsatisfied),
        shared_pool=shared_pool,
    )
