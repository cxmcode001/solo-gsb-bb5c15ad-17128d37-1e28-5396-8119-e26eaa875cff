"""FastAPI 应用入口。"""

from __future__ import annotations

from fastapi import FastAPI

from .models import (
    AssignmentOut,
    AssignRequest,
    AssignResponse,
    SharedPoolOut,
    UnsatisfiedOut,
)
from .solver import ManuscriptSpec, ReviewerSpec, solve_assignment

app = FastAPI(
    title="Anonymous Review Assignment API",
    version="1.0.0",
    description=(
        "匿名评审分配服务：基于全局最大流（非逐稿贪心）求满足主题能力、"
        "利益冲突与容量约束的完整分配；不可行时返回缺口诊断且不输出部分结果。"
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


@app.post("/assign", response_model=AssignResponse, tags=["assign"])
def assign(req: AssignRequest) -> AssignResponse:
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
        )

    return AssignResponse(
        status="infeasible",
        total_required=result.total_required,
        total_assigned=result.total_assigned,
        assignments=None,
        unsatisfied=[UnsatisfiedOut(**vars(u)) for u in result.unsatisfied],
        shared_pool=SharedPoolOut(**result.shared_pool) if result.shared_pool else None,
    )
