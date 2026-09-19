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


class AssignmentPairIn(BaseModel):
    """一条「稿件—评审人」关系，用于增量重排的历史关系与锁定关系。"""

    model_config = ConfigDict(extra="forbid")

    manuscript_id: str = Field(min_length=1, max_length=128, description="稿件 ID")
    reviewer_id: str = Field(min_length=1, max_length=128, description="评审人 ID")


class AssignRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    manuscripts: list[ManuscriptIn] = Field(max_length=10000)
    reviewers: list[ReviewerIn] = Field(max_length=10000)
    required_reviewers: int = Field(
        default=1, ge=0, le=100,
        description="每稿默认所需评审数（稿件级 required_reviewers 可覆盖）",
    )
    previous_assignments: list[AssignmentPairIn] = Field(
        default_factory=list, max_length=200000,
        description=(
            "增量重排：上一轮已有分配关系。全局可行前提下优先复用，"
            "使新增与移除关系总数最少；任一字段缺省/为空则走普通分配。"
        ),
    )
    locked_assignments: list[AssignmentPairIn] = Field(
        default_factory=list, max_length=200000,
        description=(
            "增量重排：必须保留的锁定关系。每条都须满足稿件需求、评审资格与容量；"
            "任一冲突即整体判定不可行，不输出任何部分分配。"
        ),
    )

    @model_validator(mode="after")
    def _check_request(self) -> "AssignRequest":
        m_ids = [m.id for m in self.manuscripts]
        if len(m_ids) != len(set(m_ids)):
            raise ValueError("manuscripts 中存在重复的 id")
        r_ids = [r.id for r in self.reviewers]
        if len(r_ids) != len(set(r_ids)):
            raise ValueError("reviewers 中存在重复的 id")

        m_id_set = set(m_ids)
        r_id_set = set(r_ids)
        for field_name in ("previous_assignments", "locked_assignments"):
            pairs = [
                (p.manuscript_id, p.reviewer_id)
                for p in getattr(self, field_name)
            ]
            if len(pairs) != len(set(pairs)):
                raise ValueError(f"{field_name} 中存在重复的 (manuscript_id, reviewer_id)")
            for mid, rid in pairs:
                if mid not in m_id_set:
                    raise ValueError(
                        f"{field_name} 引用了不存在的 manuscript_id: {mid}"
                    )
                if rid not in r_id_set:
                    raise ValueError(
                        f"{field_name} 引用了不存在的 reviewer_id: {rid}"
                    )
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


class LockConflictOut(BaseModel):
    """锁定关系冲突诊断（出现即整体不可行）。"""

    # ineligible_*：评审资格不满足；over_required：超出稿件需求；over_capacity：超出评审容量
    code: Literal[
        "ineligible_self_author",
        "ineligible_author_conflict",
        "ineligible_manuscript_confirmed",
        "ineligible_topics",
        "over_required",
        "over_capacity",
    ]
    manuscript_id: str | None = None
    reviewer_id: str | None = None
    # over_required / over_capacity 时实际锁定数量
    locked_count: int | None = None
    # over_required 时为稿件需求；over_capacity 时为评审容量
    limit: int | None = None
    # over_required：该稿锁定的全部评审人；over_capacity：锁定该评审人的全部稿件
    related_ids: list[str] = Field(default_factory=list)
    detail: str = ""


class ReassignmentOut(BaseModel):
    """增量重排的变更统计。"""

    reused: int = Field(description="最终分配中复用 previous_assignments 的关系数")
    added: int = Field(description="相对 previous_assignments 新增的关系数")
    removed: int = Field(description="相对 previous_assignments 移除的关系数")
    locked: int = Field(description="生效的 locked_assignments 关系数")
    total_changes: int = Field(description="新增与移除关系总数（added + removed）")


class AssignResponse(BaseModel):
    status: Literal["feasible", "infeasible"]
    total_required: int
    total_assigned: int
    # 可行时给出完整分配；不可行时为 null（不输出部分结果）
    assignments: list[AssignmentOut] | None
    # 不可行时给出缺口诊断；可行时为 null
    unsatisfied: list[UnsatisfiedOut] | None
    shared_pool: SharedPoolOut | None
    # 仅锁定关系冲突时给出结构化原因；其余情况为 null
    lock_conflicts: list[LockConflictOut] | None = None
    # 仅增量重排可行时给出变更统计；普通请求 / 不可行时为 null
    reassignment: ReassignmentOut | None = None
