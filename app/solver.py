"""全局流量匹配求解器。

将评审分配问题整体规约为二部 b-匹配：

    源点 --(所需评审数)--> 稿件 --(1, 仅当可评审)--> 评审人 --(容量)--> 汇点

- 普通模式（solve_assignment）：Edmonds-Karp 最大流，最大流 == 总需求即可行。
- 增量重排（solve_incremental）：锁定关系预占后，在残量网络上做
  最小费用最大流——previous_assignments 中的边费用为其 ID 全局序号，
  其余为 BASE+序号（BASE 大于全部序号之和），故费用字典序地先最小化
  非复用边数（=最大化复用，即新增+移除关系最少），再最小化边序号和；
  等距增广路按节点序列字典序选路，ID 顺序破除同成本解。

所有节点按 ID 排序后建图，算法完全确定：相同输入（甚至不同的列表顺序）
必然产生完全相同的分配结果。
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
class LockConflict:
    """锁定关系冲突：code 与响应模型一一对应。"""

    code: str
    manuscript_id: str | None = None
    reviewer_id: str | None = None
    locked_count: int | None = None
    limit: int | None = None
    related_ids: tuple[str, ...] = ()
    detail: str = ""


@dataclass(frozen=True)
class ReassignmentStat:
    reused: int
    added: int
    removed: int
    locked: int
    total_changes: int


@dataclass(frozen=True)
class SolveResult:
    feasible: bool
    # manuscript_id -> 已排序的 reviewer id 元组；不可行时为空（不输出部分结果）
    assignments: dict[str, tuple[str, ...]]
    total_required: int
    total_assigned: int
    unsatisfied: tuple[UnsatisfiedManuscript, ...]
    shared_pool: dict | None
    # 锁定关系冲突（出现则整体不可行）；普通/其他不可行情形为空元组
    lock_conflicts: tuple[LockConflict, ...] = ()
    # 增量重排变更统计；普通请求为 None
    reassignment: ReassignmentStat | None = None


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


def _ineligible_reason(m: ManuscriptSpec, r: ReviewerSpec) -> str | None:
    """返回该评审人不可评审该稿件的原因 code；合格返回 None。"""
    if r.id in set(m.authors):
        return "ineligible_self_author"  # 自稿冲突：评审人是稿件作者
    if set(m.authors).intersection(r.conflicted_authors):
        return "ineligible_author_conflict"  # 利益冲突：与稿件作者冲突
    if m.id in r.conflicted_manuscripts:
        return "ineligible_manuscript_confirmed"  # 利益冲突：直接声明回避该稿件
    if m.topics and not set(m.topics).intersection(r.topics):
        return "ineligible_topics"  # 主题能力不匹配
    return None


def _eligible_reviewers(m: ManuscriptSpec, reviewers: list[ReviewerSpec]) -> list[int]:
    """返回可评审该稿件的评审人下标列表（按评审人排序后的顺序）。"""
    return [
        j
        for j, r in enumerate(reviewers)
        if _ineligible_reason(m, r) is None
    ]


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


# ---------------------------------------------------------------------------
# 增量重排
#
# 锁定关系先无条件预占（冲突直接判不可行）；剩余名额在残量网络上求最小费用最大流：
#
#   源 --(残量需求)--> 稿件 --(1, 仅合格)--> 评审人 --(残量容量)--> 汇
#
# 稿件 -> 评审人 边的费用为两级字典序：
#
#   BASE + rank ：不在 previous_assignments 中
#   rank        ：在 previous_assignments 中
#
# BASE > 所有 rank 之和，因此费用首先最小化「非复用边数」
# （等价于最大化复用 previous_assignments，即新增+移除关系总数最少）；
# 在复用数相同的解之间，再以各 (稿件, 评审人) 边的 ID 全局序号之和
# （rank，按 manuscript_id 再 reviewer_id 排序）破除同成本解。
# 最短增广路在距离相同时按路径节点元组（ID 顺序）字典序选路，
# 保证相同输入必然得到唯一、确定的结果。
# ---------------------------------------------------------------------------


class _MinCostFlow:
    """带势函数的 Dijkstra 最小费用最大流（初始边费用均非负）。

    等距增广路按路径节点序列字典序决胜（节点按 ID 排序编号），
    选路与邻接表插入顺序无关，结果完全确定。
    """

    def __init__(self, n: int) -> None:
        self.n = n
        self._to: list[int] = []
        self._cap: list[int] = []
        self._cost: list[int] = []
        self._rev: list[int] = []
        self.adj: list[list[int]] = [[] for _ in range(n)]

    def add_edge(self, u: int, v: int, cap: int, cost: int) -> int:
        """新增边 u->v，返回正向边的下标（用于事后读取流量）。"""
        i = len(self._to)
        self._to.extend((v, u))
        self._cap.extend((cap, 0))
        self._cost.extend((cost, -cost))
        self._rev.extend((i + 1, i))
        self.adj[u].append(i)
        self.adj[v].append(i + 1)
        return i

    def flow_on(self, edge_idx: int) -> int:
        """正向边当前流量（等于其反向边残余容量）。"""
        return self._cap[self._rev[edge_idx]]

    def min_cost_flow(self, s: int, t: int, required: int) -> tuple[int, int]:
        """尝试送满 required 单位流，返回 (实际总流量, 总费用)；送不满时提前结束。

        等距增广路按节点序列字典序决胜：

        节点按 ID 排序编号。到节点 v 的路径次序键递归定义为
        ``(首跳节点, 父路径键, v)``——首跳不同立即按首跳定序，首跳相同
        则委托给前缀键，整体与完整节点序列的字典序一致。键以结构共享
        方式构造（直接引用父键，不复制路径），构造为 O(1)，
        比较通常在首跳即分出大小；堆按 (距离, 键) 弹出即得唯一确定路径。
        """
        import heapq

        n = self.n
        potential = [0] * n
        total_flow = 0
        total_cost = 0
        cap = self._cap
        cost = self._cost
        to = self._to
        adj = self.adj
        root_key: tuple = ()

        while total_flow < required:
            INF = 10 ** 30
            dist = [INF] * n
            dist[s] = 0
            prev_v = [-1] * n
            prev_e = [-1] * n
            settled = [False] * n
            key_of: dict[int, tuple] = {s: root_key}
            # 堆元素：(约化距离, 次序键, 节点)
            pq = [(0, root_key, s)]

            while pq:
                d, key, u = heapq.heappop(pq)
                if settled[u] or d != dist[u] or key_of.get(u) != key:
                    continue  # 已 settle 或已被更短 / 字典序更小的等距候选取代
                settled[u] = True
                # 注意：不能在 t 出堆时提前停止——必须让本次 Dijkstra
                # 完整跑完，所有可达节点都更新势函数；否则未 settle 节点的
                # 旧势函数会使残余边出现负约化费用，破坏后续 Dijkstra。
                pu = potential[u]
                for ei in adj[u]:
                    if cap[ei] <= 0:
                        continue
                    v = to[ei]
                    if settled[v]:
                        continue
                    nd = d + cost[ei] + pu - potential[v]
                    # 源点直接后继：首跳即该后继；否则沿用本路径首跳
                    v_hop = v if u == s else key[0]
                    new_key = (v_hop, key, v)
                    old_key = key_of.get(v)
                    if (
                        old_key is None
                        or nd < dist[v]
                        or (nd == dist[v] and new_key < old_key)
                    ):
                        dist[v] = nd
                        key_of[v] = new_key
                        prev_v[v] = u
                        prev_e[v] = ei
                        heapq.heappush(pq, (nd, new_key, v))

            if not settled[t]:
                return total_flow, total_cost

            for v in range(n):
                if settled[v]:
                    potential[v] += dist[v]

            bottleneck = required - total_flow
            v = t
            while v != s:
                bottleneck = min(bottleneck, cap[prev_e[v]])
                v = prev_v[v]
            v = t
            while v != s:
                ei = prev_e[v]
                cap[ei] -= bottleneck
                cap[self._rev[ei]] += bottleneck
                v = prev_v[v]

            total_flow += bottleneck
            total_cost += bottleneck * potential[t]

        return total_flow, total_cost


def solve_incremental(
    manuscripts: list[ManuscriptSpec],
    reviewers: list[ReviewerSpec],
    previous: list[tuple[str, str]],
    locked: list[tuple[str, str]],
) -> SolveResult:
    """增量重排求解。

    previous 中不满足资格的关系视为「自然失效」，直接计入移除、不参与复用；
    locked 中任一关系违反资格 / 需求 / 容量，即整体不可行并返回全部冲突。
    """
    ms = sorted(manuscripts, key=lambda m: m.id)
    rs = sorted(reviewers, key=lambda r: r.id)
    m_count, r_count = len(ms), len(rs)
    m_by_id = {m.id: i for i, m in enumerate(ms)}
    r_by_id = {r.id: j for j, r in enumerate(rs)}
    total_required = sum(m.required_reviewers for m in ms)

    locked_pairs = sorted(
        ((m_by_id[mid], r_by_id[rid]) for mid, rid in locked),
        key=lambda p: (p[0], p[1]),
    )
    # 历史关系：保留合格部分；不合格的稍后计入 removed（自然失效）
    previous_eligible: set[tuple[int, int]] = set()
    for mid, rid in previous:
        i, j = m_by_id[mid], r_by_id[rid]
        if _ineligible_reason(ms[i], rs[j]) is None:
            previous_eligible.add((i, j))

    # ---- 1) 逐条校验锁定关系的评审资格 ----
    conflicts: list[LockConflict] = []
    locked_valid: set[tuple[int, int]] = set()
    for i, j in locked_pairs:
        code = _ineligible_reason(ms[i], rs[j])
        if code is not None:
            m, r = ms[i], rs[j]
            reasons = {
                "ineligible_self_author": "评审人是该稿件作者（自稿冲突）",
                "ineligible_author_conflict": "评审人与稿件作者存在利益冲突",
                "ineligible_manuscript_confirmed": "评审人已声明回避该稿件",
                "ineligible_topics": "评审人主题能力与稿件不匹配",
            }
            conflicts.append(
                LockConflict(
                    code=code,
                    manuscript_id=m.id,
                    reviewer_id=r.id,
                    detail=reasons[code],
                )
            )
        else:
            locked_valid.add((i, j))

    # ---- 2) 稿件需求：每篇锁定数不得超过其所需评审数 ----
    locked_per_m: dict[int, list[int]] = {}
    for i, j in locked_valid:
        locked_per_m.setdefault(i, []).append(j)
    over_required: list[LockConflict] = []
    for i in sorted(locked_per_m):
        rids = sorted(locked_per_m[i])
        if len(rids) > ms[i].required_reviewers:
            over_required.append(
                LockConflict(
                    code="over_required",
                    manuscript_id=ms[i].id,
                    locked_count=len(rids),
                    limit=ms[i].required_reviewers,
                    related_ids=tuple(rs[j].id for j in rids),
                    detail=(
                        f"锁定评审 {len(rids)} 人，超过该稿件所需 "
                        f"{ms[i].required_reviewers} 人"
                    ),
                )
            )

    # ---- 3) 评审容量：每名评审锁定数不得超过其容量 ----
    locked_per_r: dict[int, list[int]] = {}
    for i, j in locked_valid:
        locked_per_r.setdefault(j, []).append(i)
    over_capacity: list[LockConflict] = []
    for j in sorted(locked_per_r):
        mids = sorted(locked_per_r[j])
        if len(mids) > rs[j].capacity:
            over_capacity.append(
                LockConflict(
                    code="over_capacity",
                    reviewer_id=rs[j].id,
                    locked_count=len(mids),
                    limit=rs[j].capacity,
                    related_ids=tuple(ms[i].id for i in mids),
                    detail=(
                        f"被 {len(mids)} 篇稿件锁定，超过该评审容量 "
                        f"{rs[j].capacity} 篇"
                    ),
                )
            )

    # 结构化不可行原因一次给全：资格 -> 需求 -> 容量；组内按 ID 排序确定
    if over_required:
        conflicts.extend(over_required)
    if over_capacity:
        conflicts.extend(over_capacity)
    if conflicts:
        return SolveResult(
            feasible=False,
            assignments={},
            total_required=total_required,
            total_assigned=0,
            unsatisfied=(),
            shared_pool=None,
            lock_conflicts=tuple(conflicts),
        )

    # ---- 4) 残量网络：锁定预占后，剩余名额做最小费用最大流 ----
    src = 0
    snk = 1 + m_count + r_count

    def m_node(i: int) -> int:
        return 1 + i

    def r_node(j: int) -> int:
        return 1 + m_count + j

    eligible = [_eligible_reviewers(m, rs) for m in ms]

    # rank：边在 (稿件 ID, 评审 ID) 全局序列中的序号，作为第二级确定性目标
    rank: dict[tuple[int, int], int] = {}
    seq = 0
    for i in range(m_count):
        for j in eligible[i]:
            rank[(i, j)] = seq
            seq += 1
    base = max(1, seq) * (total_required + 1)

    mcf = _MinCostFlow(snk + 1)
    for i, m in enumerate(ms):
        residual_required = m.required_reviewers - len(locked_per_m.get(i, ()))
        if residual_required > 0:
            mcf.add_edge(src, m_node(i), residual_required, 0)
    edge_of: dict[tuple[int, int], int] = {}
    for i in range(m_count):
        for j in eligible[i]:
            if (i, j) in locked_valid:
                continue  # 锁定关系已预占
            cost = rank[(i, j)] if (i, j) in previous_eligible else base + rank[(i, j)]
            edge_of[(i, j)] = mcf.add_edge(m_node(i), r_node(j), 1, cost)
    residual_cap: list[int] = []
    for j, r in enumerate(rs):
        cap = r.capacity - len(locked_per_r.get(j, ()))
        residual_cap.append(cap)
        if cap > 0:
            mcf.add_edge(r_node(j), snk, cap, 0)

    locked_total = len(locked_valid)
    flow_amount, _ = mcf.min_cost_flow(src, snk, total_required - locked_total)

    # 最终分配 = 锁定 + 流上的边
    final: dict[int, set[int]] = {i: set(locked_per_m.get(i, ())) for i in range(m_count)}
    for (i, j), ei in edge_of.items():
        if mcf.flow_on(ei) > 0:
            final[i].add(j)

    if locked_total + flow_amount < total_required:
        # ---- 残量问题不可行：缺口诊断（锁定视为已满足），不输出部分分配 ----
        r_remaining: dict[int, int] = {j: residual_cap[j] for j in range(r_count)}
        for (i, j), ei in edge_of.items():
            if mcf.flow_on(ei) > 0:
                r_remaining[j] -= 1

        unsatisfied: list[UnsatisfiedManuscript] = []
        unsat_idx: list[int] = []
        for i, m in enumerate(ms):
            assigned = len(final[i])
            if assigned >= m.required_reviewers:
                continue
            missing = m.required_reviewers - assigned
            elig = eligible[i]
            remaining = sum(r_remaining[j] for j in elig)
            unsatisfied.append(
                UnsatisfiedManuscript(
                    manuscript_id=m.id,
                    required=m.required_reviewers,
                    assigned=assigned,
                    missing=missing,
                    eligible_reviewers=len(elig),
                    eligible_remaining_capacity=remaining,
                    candidate_gap=max(0, m.required_reviewers - len(elig)),
                    capacity_gap=max(0, missing - remaining),
                )
            )
            unsat_idx.append(i)

        pool = sorted({j for i in unsat_idx for j in eligible[i]})
        shared_pool = {
            "manuscript_ids": [ms[i].id for i in unsat_idx],
            "reviewer_ids": [rs[j].id for j in pool],
            "missing_total": sum(u.missing for u in unsatisfied),
            "pool_remaining_capacity": sum(r_remaining[j] for j in pool),
            "pool_total_capacity": sum(rs[j].capacity for j in pool),
        }

        return SolveResult(
            feasible=False,
            assignments={},
            total_required=total_required,
            total_assigned=locked_total + flow_amount,
            unsatisfied=tuple(unsatisfied),
            shared_pool=shared_pool,
        )

    # ---- 5) 可行：组装确定顺序的分配与变更统计 ----
    assignments: dict[str, tuple[str, ...]] = {}
    final_pairs: set[tuple[int, int]] = set()
    for i in range(m_count):
        ids = tuple(rs[j].id for j in sorted(final[i]))
        assignments[ms[i].id] = ids
        final_pairs.update((i, j) for j in final[i])

    # previous 全部关系（含已自然失效的不合格关系）参与变更统计
    previous_all: set[tuple[int, int]] = {
        (m_by_id[mid], r_by_id[rid]) for mid, rid in previous
    }
    reused = len(final_pairs & previous_all)
    added = len(final_pairs - previous_all)
    removed = len(previous_all - final_pairs)

    return SolveResult(
        feasible=True,
        assignments=assignments,
        total_required=total_required,
        total_assigned=total_required,
        unsatisfied=(),
        shared_pool=None,
        reassignment=ReassignmentStat(
            reused=reused,
            added=added,
            removed=removed,
            locked=locked_total,
            total_changes=added + removed,
        ),
    )
