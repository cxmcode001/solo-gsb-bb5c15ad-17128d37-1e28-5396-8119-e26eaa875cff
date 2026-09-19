"""请求 / 响应模型（Pydantic v2）。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ManuscriptIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=128, description="稿件 ID（唯一）")
    authors: list[str] = Field(default_factory=list, max_length=512, description="作者名单")
    topics: list[str] = Field(default_factory=list, max_length=64, description="稿件主题")
    required_reviewers: int | None = Field(
        default=None, ge=0, le=100,
        description="该稿所需评审数；缺省时使用请求级 required_reviewers",
    )


class ReviewerIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=128, description="评审人 ID（唯一）")
    topics: list[str] = Field(default_factory=list, max_length=64, description="主题能力")
    capacity: int = Field(ge=0, le=1000, description="最多可评审稿件数")
    conflicted_authors: list[str] = Field(
        default_factory=list, max_length=2048, description="存在利益冲突的作者名单"
    )
    conflicted_manuscripts: list[str] = Field(
        default_factory=list, max_length=2048, description="需回避的稿件 ID 列表"
    )


class AssignRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    manuscripts: list[ManuscriptIn] = Field(max_length=10000)
    reviewers: list[ReviewerIn] = Field(max_length=10000)
    required_reviewers: int = Field(
        default=1, ge=0, le=100,
        description="每稿默认所需评审数（稿件级 required_reviewers 可覆盖）",
    )

    @model_validator(mode="after")
    def _check_unique_ids(self) -> "AssignRequest":
        m_ids = [m.id for m in self.manuscripts]
        if len(m_ids) != len(set(m_ids)):
            raise ValueError("manuscripts 中存在重复的 id")
        r_ids = [r.id for r in self.reviewers]
        if len(r_ids) != len(set(r_ids)):
            raise ValueError("reviewers 中存在重复的 id")
        return self


class AssignmentOut(BaseModel):
    manuscript_id: str
    reviewer_ids: list[str]


class UnsatisfiedOut(BaseModel):
    manuscript_id: str
    required: int
    assigned: int
    missing: int
    eligible_reviewers: int
    eligible_remaining_capacity: int
    candidate_gap: int
    capacity_gap: int


class SharedPoolOut(BaseModel):
    manuscript_ids: list[str]
    reviewer_ids: list[str]
    missing_total: int
    pool_remaining_capacity: int
    pool_total_capacity: int


class AssignResponse(BaseModel):
    status: Literal["feasible", "infeasible"]
    total_required: int
    total_assigned: int
    # 可行时给出完整分配；不可行时为 null（不输出部分结果）
    assignments: list[AssignmentOut] | None
    # 不可行时给出缺口诊断；可行时为 null
    unsatisfied: list[UnsatisfiedOut] | None
    shared_pool: SharedPoolOut | None
