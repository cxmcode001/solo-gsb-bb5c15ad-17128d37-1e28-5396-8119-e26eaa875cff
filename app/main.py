"""FastAPI 应用入口。"""

from __future__ import annotations

from fastapi import FastAPI

from .models import (
    AssignmentOut,
    AssignRequest,
    AssignResponse,
    LockConflictOut,
    ReassignmentOut,
    SharedPoolOut,
    UnsatisfiedOut,
)
from .solver import ManuscriptSpec, ReviewerSpec, solve_assignment, solve_incremental

app = FastAPI(
    title="Anonymous Review Assignment API",
    version="1.1.0",
    description=(
        "匿名评审分配服务：基于全局最大流（非逐稿贪心）求满足主题能力、"
        "利益冲突与容量约束的完整分配；不可行时返回缺口诊断且不输出部分结果。"
        "支持携带 previous_assignments / locked_assignments 的增量重排模式："
        "锁定关系冲突时返回结构化原因，其余名额最小化新增/移除关系数。"
        "服务无状态，不持久化任何稿件/评审信息。"
    ),
)


@app.get("/health", tags=["meta"])
def health() -> dict:
    return {"status": "ok"}


@app.get("/", tags=["meta"])
def root() -> dict:
    return {
        "service": "anonymous-review-assign",
        "version": app.version,
        "docs": "/docs",
        "health": "/health",
    }


def _build_specs(req: AssignRequest):
    default_required = req.required_reviewers
    manuscripts = [
        ManuscriptSpec(
            id=m.id,
            authors=tuple(m.authors),
            topics=tuple(m.topics),
            required_reviewers=(
                m.required_reviewers
                if m.required_reviewers is not None
                else default_required
            ),
        )
        for m in req.manuscripts
    ]
    reviewers = [
        ReviewerSpec(
            id=r.id,
            topics=tuple(r.topics),
            capacity=r.capacity,
            conflicted_authors=tuple(r.conflicted_authors),
            conflicted_manuscripts=tuple(r.conflicted_manuscripts),
        )
        for r in req.reviewers
    ]
    return manuscripts, reviewers


@app.post("/assign", response_model=AssignResponse, tags=["assign"])
def assign(req: AssignRequest) -> AssignResponse:
    manuscripts, reviewers = _build_specs(req)

    incremental = bool(req.previous_assignments or req.locked_assignments)
    if incremental:
        result = solve_incremental(
            manuscripts,
            reviewers,
            previous=[
                (p.manuscript_id, p.reviewer_id) for p in req.previous_assignments
            ],
            locked=[
                (p.manuscript_id, p.reviewer_id) for p in req.locked_assignments
            ],
        )
    else:
        # 普通请求：保持原 Edmonds-Karp 路径与响应完全不变
        result = solve_assignment(manuscripts, reviewers)

    if result.feasible:
        return AssignResponse(
            status="feasible",
            total_required=result.total_required,
            total_assigned=result.total_assigned,
            assignments=[
                AssignmentOut(manuscript_id=mid, reviewer_ids=list(rids))
                for mid, rids in sorted(result.assignments.items())
            ],
            unsatisfied=None,
            shared_pool=None,
            lock_conflicts=None,
            reassignment=(
                ReassignmentOut(**vars(result.reassignment))
                if result.reassignment is not None
                else None
            ),
        )

    return AssignResponse(
        status="infeasible",
        total_required=result.total_required,
        total_assigned=result.total_assigned,
        assignments=None,
        unsatisfied=[UnsatisfiedOut(**vars(u)) for u in result.unsatisfied],
        shared_pool=SharedPoolOut(**result.shared_pool) if result.shared_pool else None,
        lock_conflicts=[
            LockConflictOut(**vars(c)) for c in result.lock_conflicts
        ]
        or None,
        reassignment=None,
    )
