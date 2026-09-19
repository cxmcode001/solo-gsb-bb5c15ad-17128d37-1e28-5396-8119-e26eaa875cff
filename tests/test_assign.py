"""POST /assign 与 /health 的端到端测试。"""

from __future__ import annotations

import random

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def _verify_constraints(body: dict, response: dict) -> None:
    """校验分配结果满足全部约束：篇均评审数、容量、主题匹配、冲突回避。"""
    assert response["status"] == "feasible"
    assignments = {a["manuscript_id"]: a["reviewer_ids"] for a in response["assignments"]}
    reviewers = {r["id"]: r for r in body["reviewers"]}
    default_req = body.get("required_reviewers", 1)

    used = {rid: 0 for rid in reviewers}
    for m in body["manuscripts"]:
        rid_list = assignments[m["id"]]
        req = m.get("required_reviewers") or default_req
        assert len(rid_list) == req, f"{m['id']} 评审数不足"
        assert len(set(rid_list)) == len(rid_list), "同一稿件重复分配评审人"
        for rid in rid_list:
            r = reviewers[rid]
            used[rid] += 1
            assert rid not in m["authors"], "自稿冲突"
            assert not set(m["authors"]) & set(r.get("conflicted_authors", [])), "利益冲突"
            assert m["id"] not in r.get("conflicted_manuscripts", [])
            if m["topics"]:
                assert set(m["topics"]) & set(r["topics"]), "主题不匹配"
    for rid, n in used.items():
        assert n <= reviewers[rid]["capacity"], f"{rid} 超出容量"


# ---------- 健康检查 ----------

def test_health():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


# ---------- 可行场景 ----------

def test_feasible_basic():
    body = {
        "required_reviewers": 2,
        "manuscripts": [
            {"id": "m1", "authors": ["alice"], "topics": ["ml"]},
            {"id": "m2", "authors": ["bob"], "topics": ["ml", "db"]},
            {"id": "m3", "authors": ["carol"], "topics": ["db"]},
        ],
        "reviewers": [
            {"id": "r1", "topics": ["ml"], "capacity": 2},
            {"id": "r2", "topics": ["ml", "db"], "capacity": 2},
            {"id": "r3", "topics": ["db"], "capacity": 2},
            {"id": "r4", "topics": ["ml", "db"], "capacity": 1,
             "conflicted_authors": ["alice"]},
        ],
    }
    resp = client.post("/assign", json=body)
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_required"] == 6
    assert data["total_assigned"] == 6
    assert data["unsatisfied"] is None
    _verify_constraints(body, data)


def test_global_matching_not_greedy():
    """逐稿贪心会失败、全局匹配才能成功的经典情形。

    m1 可由 r1/r2 评审，m2 只能由 r1 评审，容量均为 1。
    贪心先把 r1 给 m1 后 m2 无人可评；全局解必须是 m1->r2, m2->r1。
    """
    body = {
        "manuscripts": [
            {"id": "m1", "authors": [], "topics": ["t1"], "required_reviewers": 1},
            {"id": "m2", "authors": [], "topics": ["t2"], "required_reviewers": 1},
        ],
        "reviewers": [
            {"id": "r1", "topics": ["t1", "t2"], "capacity": 1},
            {"id": "r2", "topics": ["t1"], "capacity": 1},
        ],
    }
    resp = client.post("/assign", json=body)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "feasible"
    assignments = {a["manuscript_id"]: a["reviewer_ids"] for a in data["assignments"]}
    assert assignments == {"m1": ["r2"], "m2": ["r1"]}


def test_per_manuscript_required_overrides_default():
    body = {
        "required_reviewers": 1,
        "manuscripts": [
            {"id": "m1", "authors": [], "topics": ["t"], "required_reviewers": 2},
            {"id": "m2", "authors": [], "topics": ["t"]},
        ],
        "reviewers": [
            {"id": "r1", "topics": ["t"], "capacity": 2},
            {"id": "r2", "topics": ["t"], "capacity": 2},
        ],
    }
    data = client.post("/assign", json=body).json()
    assert data["status"] == "feasible"
    assert data["total_required"] == 3
    _verify_constraints(body, data)


def test_empty_manuscripts_is_feasible():
    body = {"manuscripts": [], "reviewers": [{"id": "r1", "topics": [], "capacity": 1}]}
    data = client.post("/assign", json=body).json()
    assert data["status"] == "feasible"
    assert data["assignments"] == []


# ---------- 确定性 / 稳定性 ----------

def _random_instance(seed: int) -> dict:
    rng = random.Random(seed)
    topics = ["ml", "nlp", "cv", "db", "se"]
    authors = [f"author{i}" for i in range(20)]
    manuscripts = [
        {
            "id": f"m{i}",
            "authors": rng.sample(authors, 2),
            "topics": rng.sample(topics, rng.randint(1, 2)),
            "required_reviewers": rng.randint(1, 3),
        }
        for i in range(12)
    ]
    reviewers = [
        {
            "id": f"r{j}",
            "topics": rng.sample(topics, rng.randint(1, 3)),
            "capacity": rng.randint(1, 4),
            "conflicted_authors": rng.sample(authors, rng.randint(0, 3)),
        }
        for j in range(15)
    ]
    return {"manuscripts": manuscripts, "reviewers": reviewers}


def test_same_input_same_output():
    body = _random_instance(seed=42)
    r1 = client.post("/assign", json=body).json()
    r2 = client.post("/assign", json=body).json()
    assert r1 == r2


def test_input_order_does_not_change_result():
    body = _random_instance(seed=7)
    shuffled = {
        "manuscripts": list(reversed(body["manuscripts"])),
        "reviewers": list(reversed(body["reviewers"])),
    }
    r1 = client.post("/assign", json=body).json()
    r2 = client.post("/assign", json=shuffled).json()
    assert r1 == r2


# ---------- 不可行场景 ----------

def test_infeasible_capacity_gap():
    """总容量不足：2 稿各需 2 人，只有 2 名评审各容量 1。"""
    body = {
        "required_reviewers": 2,
        "manuscripts": [
            {"id": "m1", "authors": [], "topics": ["t"]},
            {"id": "m2", "authors": [], "topics": ["t"]},
        ],
        "reviewers": [
            {"id": "r1", "topics": ["t"], "capacity": 1},
            {"id": "r2", "topics": ["t"], "capacity": 1},
        ],
    }
    data = client.post("/assign", json=body).json()
    assert data["status"] == "infeasible"
    # 不输出任何部分分配
    assert data["assignments"] is None
    assert data["total_required"] == 4
    assert data["total_assigned"] == 2

    unsat = {u["manuscript_id"]: u for u in data["unsatisfied"]}
    assert set(unsat) == {"m2"}  # 确定性：m1 先被满足，m2 缺口 2
    assert unsat["m2"]["missing"] == 2
    assert unsat["m2"]["eligible_reviewers"] == 2
    assert unsat["m2"]["eligible_remaining_capacity"] == 0
    assert unsat["m2"]["candidate_gap"] == 0
    assert unsat["m2"]["capacity_gap"] == 2

    pool = data["shared_pool"]
    assert pool["missing_total"] == 2
    assert pool["pool_remaining_capacity"] == 0
    assert pool["pool_total_capacity"] == 2
    assert sorted(pool["reviewer_ids"]) == ["r1", "r2"]


def test_infeasible_candidate_gap():
    """候选人数不足：m1 需 2 人，但 r2 与作者存在利益冲突。"""
    body = {
        "manuscripts": [
            {"id": "m1", "authors": ["alice"], "topics": ["ml"],
             "required_reviewers": 2},
        ],
        "reviewers": [
            {"id": "r1", "topics": ["ml"], "capacity": 5},
            {"id": "r2", "topics": ["ml"], "capacity": 5,
             "conflicted_authors": ["alice"]},
        ],
    }
    data = client.post("/assign", json=body).json()
    assert data["status"] == "infeasible"
    assert data["assignments"] is None
    u = data["unsatisfied"][0]
    assert u["manuscript_id"] == "m1"
    assert u["eligible_reviewers"] == 1
    assert u["candidate_gap"] == 1
    assert u["capacity_gap"] == 0  # r1 剩余容量充足，缺的是候选人数


def test_infeasible_no_eligible_reviewer_at_all():
    body = {
        "manuscripts": [
            {"id": "m1", "authors": ["alice"], "topics": ["quantum"],
             "required_reviewers": 1},
        ],
        "reviewers": [{"id": "r1", "topics": ["ml"], "capacity": 3}],
    }
    data = client.post("/assign", json=body).json()
    assert data["status"] == "infeasible"
    u = data["unsatisfied"][0]
    assert u["eligible_reviewers"] == 0
    assert u["candidate_gap"] == 1
    assert data["shared_pool"]["reviewer_ids"] == []


def test_self_conflict_excluded():
    """评审人是稿件作者时自动回避。"""
    body = {
        "manuscripts": [
            {"id": "m1", "authors": ["r1"], "topics": ["t"],
             "required_reviewers": 1},
        ],
        "reviewers": [
            {"id": "r1", "topics": ["t"], "capacity": 1},
            {"id": "r2", "topics": ["t"], "capacity": 1},
        ],
    }
    data = client.post("/assign", json=body).json()
    assert data["status"] == "feasible"
    assert data["assignments"][0]["reviewer_ids"] == ["r2"]


# ---------- 请求校验 ----------

def test_duplicate_manuscript_id_rejected():
    body = {
        "manuscripts": [{"id": "m1"}, {"id": "m1"}],
        "reviewers": [],
    }
    resp = client.post("/assign", json=body)
    assert resp.status_code == 422


def test_duplicate_reviewer_id_rejected():
    body = {
        "manuscripts": [],
        "reviewers": [
            {"id": "r1", "capacity": 1},
            {"id": "r1", "capacity": 2},
        ],
    }
    resp = client.post("/assign", json=body)
    assert resp.status_code == 422


def test_negative_capacity_rejected():
    body = {
        "manuscripts": [],
        "reviewers": [{"id": "r1", "capacity": -1}],
    }
    resp = client.post("/assign", json=body)
    assert resp.status_code == 422


# ---------- 增量重排 ----------

_INC_BASE = {
    "manuscripts": [
        {"id": "m1", "authors": [], "topics": ["t"], "required_reviewers": 1},
        {"id": "m2", "authors": [], "topics": ["t"], "required_reviewers": 1},
    ],
    "reviewers": [
        {"id": "r1", "topics": ["t"], "capacity": 1},
        {"id": "r2", "topics": ["t"], "capacity": 2},
    ],
}


def test_incremental_reuses_previous_when_globally_feasible():
    """历史关系本会占用关键容量时，全局重排复用它，而不是逐稿贪心推翻。"""
    body = {
        **_INC_BASE,
        # m2 只有 r1 能满足（r2 容量可给 m1）；历史却是 m1->r1
        "previous_assignments": [{"manuscript_id": "m1", "reviewer_id": "r1"}],
    }
    # r1 容量 1 时历史不可整体复用：m2 无其他可选（本例 r2 也可评 m2，故仍可行，
    # 但复用 m1->r1 会使 m2->r2，复用数=1，是最优）
    data = client.post("/assign", json=body).json()
    assert data["status"] == "feasible"
    assignments = {a["manuscript_id"]: a["reviewer_ids"] for a in data["assignments"]}
    assert assignments == {"m1": ["r1"], "m2": ["r2"]}
    st = data["reassignment"]
    assert st == {"reused": 1, "added": 1, "removed": 0, "locked": 0, "total_changes": 1}


def test_incremental_minimizes_added_and_removed():
    """锁定关系强制让出容量时：m1 被改为 r2，历史 m1->r1 记为移除、m1->r2 记新增。"""
    body = {
        **_INC_BASE,
        "previous_assignments": [{"manuscript_id": "m1", "reviewer_id": "r1"}],
        "locked_assignments": [{"manuscript_id": "m2", "reviewer_id": "r1"}],
    }
    data = client.post("/assign", json=body).json()
    assert data["status"] == "feasible"
    assignments = {a["manuscript_id"]: a["reviewer_ids"] for a in data["assignments"]}
    assert assignments == {"m1": ["r2"], "m2": ["r1"]}
    assert data["reassignment"] == {
        "reused": 0, "added": 2, "removed": 1, "locked": 1, "total_changes": 3
    }


def test_incremental_invalid_previous_pair_is_naturally_dropped():
    """历史关系已不再满足资格（主题冲突）：自然失效，计为移除，不判不可行。"""
    body = {
        "manuscripts": [
            {"id": "m1", "authors": ["alice"], "topics": ["ml"],
             "required_reviewers": 1},
        ],
        "reviewers": [
            {"id": "r1", "topics": ["db"], "capacity": 1},
            {"id": "r2", "topics": ["ml"], "capacity": 1},
        ],
        "previous_assignments": [{"manuscript_id": "m1", "reviewer_id": "r1"}],
    }
    data = client.post("/assign", json=body).json()
    assert data["status"] == "feasible"
    assert data["assignments"][0]["reviewer_ids"] == ["r2"]
    assert data["reassignment"]["reused"] == 0
    assert data["reassignment"]["removed"] == 1
    assert data["reassignment"]["added"] == 1


def test_incremental_tie_break_is_id_order_and_deterministic():
    """历史关系与新增名额互不竞争时，新增边在同成本解之间按 ID 顺序决胜；
    评审人 / 稿件列表乱序不影响结果。"""
    body = {
        "manuscripts": [
            {"id": "m1", "topics": ["t1"], "required_reviewers": 1},
            {"id": "m2", "topics": ["t2"], "required_reviewers": 1},
        ],
        "reviewers": [
            {"id": "r2", "topics": ["t1", "t2"], "capacity": 2},
            {"id": "r1", "topics": ["t1"], "capacity": 1},
        ],
        # m2 唯一可选 r2（r1 无 t2 能力），复用与 m1 的选择互不竞争；
        # m1 可在 r1/r2 间任选，rank 更小的 r1 胜出
        "previous_assignments": [{"manuscript_id": "m2", "reviewer_id": "r2"}],
    }
    r1 = client.post("/assign", json=body).json()
    shuffled = {
        **body,
        "manuscripts": list(reversed(body["manuscripts"])),
        "reviewers": list(reversed(body["reviewers"])),
    }
    r2 = client.post("/assign", json=shuffled).json()
    assert r1 == r2
    assignments = {a["manuscript_id"]: a["reviewer_ids"] for a in r1["assignments"]}
    assert assignments["m1"] == ["r1"]
    assert assignments["m2"] == ["r2"]
    assert r1["reassignment"] == {
        "reused": 1, "added": 1, "removed": 0, "locked": 0, "total_changes": 1
    }


def test_locked_feasible_is_enforced():
    body = {
        **_INC_BASE,
        "locked_assignments": [{"manuscript_id": "m1", "reviewer_id": "r1"}],
    }
    data = client.post("/assign", json=body).json()
    assert data["status"] == "feasible"
    assignments = {a["manuscript_id"]: a["reviewer_ids"] for a in data["assignments"]}
    assert assignments["m1"] == ["r1"]
    assert data["reassignment"]["locked"] == 1


def test_locked_ineligible_returns_structured_conflict_no_partial():
    body = {
        "manuscripts": [
            {"id": "m1", "authors": ["alice"], "topics": ["ml"],
             "required_reviewers": 1},
        ],
        "reviewers": [
            {"id": "r1", "topics": ["ml"], "capacity": 2,
             "conflicted_authors": ["alice"]},
            {"id": "r2", "topics": ["ml"], "capacity": 2},
        ],
        "locked_assignments": [{"manuscript_id": "m1", "reviewer_id": "r1"}],
    }
    data = client.post("/assign", json=body).json()
    assert data["status"] == "infeasible"
    assert data["assignments"] is None  # 不输出部分分配
    assert data["reassignment"] is None
    conflict = data["lock_conflicts"][0]
    assert conflict["code"] == "ineligible_author_conflict"
    assert conflict["manuscript_id"] == "m1"
    assert conflict["reviewer_id"] == "r1"
    assert conflict["detail"]


def test_locked_over_required_returns_structured_conflict():
    body = {
        "manuscripts": [
            {"id": "m1", "topics": ["t"], "required_reviewers": 1},
        ],
        "reviewers": [
            {"id": "r1", "topics": ["t"], "capacity": 5},
            {"id": "r2", "topics": ["t"], "capacity": 5},
        ],
        "locked_assignments": [
            {"manuscript_id": "m1", "reviewer_id": "r1"},
            {"manuscript_id": "m1", "reviewer_id": "r2"},
        ],
    }
    data = client.post("/assign", json=body).json()
    assert data["status"] == "infeasible"
    assert data["assignments"] is None
    c = next(c for c in data["lock_conflicts"] if c["code"] == "over_required")
    assert c["manuscript_id"] == "m1"
    assert c["locked_count"] == 2
    assert c["limit"] == 1
    assert c["related_ids"] == ["r1", "r2"]


def test_locked_over_capacity_returns_structured_conflict():
    body = {
        **_INC_BASE,
        "locked_assignments": [
            {"manuscript_id": "m1", "reviewer_id": "r1"},
            {"manuscript_id": "m2", "reviewer_id": "r1"},
        ],
    }
    data = client.post("/assign", json=body).json()
    assert data["status"] == "infeasible"
    assert data["assignments"] is None
    c = next(c for c in data["lock_conflicts"] if c["code"] == "over_capacity")
    assert c["reviewer_id"] == "r1"
    assert c["locked_count"] == 2
    assert c["limit"] == 1
    assert c["related_ids"] == ["m1", "m2"]


def test_locked_feasible_but_globally_infeasible_gives_gap_diagnosis():
    """锁本身合法，但锁定占满唯一容量后其余需求无解：走缺口诊断，不输出部分结果。"""
    body = {
        "manuscripts": [
            {"id": "m1", "topics": ["t"], "required_reviewers": 2},
            {"id": "m2", "topics": ["t"], "required_reviewers": 2},
        ],
        "reviewers": [
            {"id": "r1", "topics": ["t"], "capacity": 1},
            {"id": "r2", "topics": ["t"], "capacity": 3},
        ],
        "locked_assignments": [{"manuscript_id": "m1", "reviewer_id": "r1"}],
    }
    data = client.post("/assign", json=body).json()
    assert data["status"] == "infeasible"
    assert data["assignments"] is None
    assert data["lock_conflicts"] is None
    assert data["total_required"] == 4
    assert data["total_assigned"] == 3
    unsat = {u["manuscript_id"]: u for u in data["unsatisfied"]}
    assert set(unsat) == {"m2"}


def test_locked_self_author_conflict_structured():
    body = {
        "manuscripts": [
            {"id": "m1", "authors": ["r1"], "topics": ["t"],
             "required_reviewers": 1},
        ],
        "reviewers": [
            {"id": "r1", "topics": ["t"], "capacity": 1},
            {"id": "r2", "topics": ["t"], "capacity": 1},
        ],
        "locked_assignments": [{"manuscript_id": "m1", "reviewer_id": "r1"}],
    }
    data = client.post("/assign", json=body).json()
    assert data["status"] == "infeasible"
    assert data["lock_conflicts"][0]["code"] == "ineligible_self_author"


def test_locked_conflicts_reports_all_categories_deterministically():
    """资格冲突 / 超额需求 / 超容量互不关联时，一次返回全部三类原因，顺序确定。"""
    body = {
        "manuscripts": [
            # m1：只需要 1 人却锁定 2 人（r1、r2 均合格）—— over_required
            {"id": "m1", "authors": [], "topics": ["t"], "required_reviewers": 1},
            # m2：锁定 r3（主题不符）—— ineligible_topics
            {"id": "m2", "authors": [], "topics": ["ml"], "required_reviewers": 1},
            # m3 / m4：都合法锁定 r1（r1 容量 1）—— over_capacity
            {"id": "m3", "authors": [], "topics": ["t"], "required_reviewers": 1},
            {"id": "m4", "authors": [], "topics": ["t"], "required_reviewers": 1},
        ],
        "reviewers": [
            {"id": "r1", "topics": ["t", "ml"], "capacity": 1},
            {"id": "r2", "topics": ["t", "ml"], "capacity": 5},
            {"id": "r3", "topics": ["db"], "capacity": 5},
        ],
        "locked_assignments": [
            {"manuscript_id": "m1", "reviewer_id": "r1"},
            {"manuscript_id": "m1", "reviewer_id": "r2"},
            {"manuscript_id": "m2", "reviewer_id": "r3"},
            {"manuscript_id": "m3", "reviewer_id": "r1"},
            {"manuscript_id": "m4", "reviewer_id": "r1"},
        ],
    }
    r_a = client.post("/assign", json=body).json()
    shuffled = {
        "manuscripts": list(reversed(body["manuscripts"])),
        "reviewers": list(reversed(body["reviewers"])),
        "locked_assignments": list(reversed(body["locked_assignments"])),
    }
    r_b = client.post("/assign", json=shuffled).json()
    assert r_a == r_b
    assert r_a["assignments"] is None
    codes = [c["code"] for c in r_a["lock_conflicts"]]
    # 输出顺序：资格类 -> over_required -> over_capacity，各组内按 ID 排序
    assert codes == [
        "ineligible_topics",
        "over_required",
        "over_capacity",
    ]
    cap = next(c for c in r_a["lock_conflicts"] if c["code"] == "over_capacity")
    assert cap["reviewer_id"] == "r1"
    assert cap["locked_count"] == 3
    assert cap["limit"] == 1
    assert cap["related_ids"] == ["m1", "m3", "m4"]


def test_incremental_unknown_manuscript_id_rejected():
    body = {
        "manuscripts": [{"id": "m1", "topics": ["t"]}],
        "reviewers": [{"id": "r1", "topics": ["t"], "capacity": 1}],
        "previous_assignments": [
            {"manuscript_id": "mX", "reviewer_id": "r1"}
        ],
    }
    assert client.post("/assign", json=body).status_code == 422


def test_incremental_unknown_reviewer_id_in_locked_rejected():
    body = {
        "manuscripts": [{"id": "m1", "topics": ["t"]}],
        "reviewers": [{"id": "r1", "topics": ["t"], "capacity": 1}],
        "locked_assignments": [{"manuscript_id": "m1", "reviewer_id": "rX"}],
    }
    assert client.post("/assign", json=body).status_code == 422


def test_duplicate_previous_pair_rejected():
    pair = {"manuscript_id": "m1", "reviewer_id": "r1"}
    body = {
        "manuscripts": [{"id": "m1", "topics": ["t"]}],
        "reviewers": [{"id": "r1", "topics": ["t"], "capacity": 2}],
        "previous_assignments": [pair, dict(pair)],
    }
    assert client.post("/assign", json=body).status_code == 422


def test_empty_incremental_lists_keep_normal_response_shape():
    """previous/locked 均为空（或缺省）时响应与普通请求逐字段兼容。"""
    body = {
        "manuscripts": [{"id": "m1", "topics": ["t"]}],
        "reviewers": [{"id": "r1", "topics": ["t"], "capacity": 1}],
        "previous_assignments": [],
        "locked_assignments": [],
    }
    plain = client.post("/assign", json={
        "manuscripts": body["manuscripts"], "reviewers": body["reviewers"]
    }).json()
    inc = client.post("/assign", json=body).json()
    assert inc == plain
    assert inc["reassignment"] is None
    assert inc["lock_conflicts"] is None


def test_incremental_deterministic_under_input_shuffle():
    body = _random_instance(seed=99)
    seen = set()
    prev = []
    for m in body["manuscripts"]:
        for j in range(m.get("required_reviewers", 2)):
            pair = {"manuscript_id": m["id"], "reviewer_id": f"r{(j + 3) % 15}"}
            key = (pair["manuscript_id"], pair["reviewer_id"])
            if key not in seen:
                seen.add(key)
                prev.append(pair)
    body["previous_assignments"] = prev
    # 仅打乱列表顺序；增量字段两个请求保持一致
    shuffled = {
        "manuscripts": list(reversed(body["manuscripts"])),
        "reviewers": list(reversed(body["reviewers"])),
        "previous_assignments": list(reversed(body["previous_assignments"])),
    }
    r1 = client.post("/assign", json=body).json()
    r2 = client.post("/assign", json=shuffled).json()
    assert r1["status"] == r2["status"] == "feasible"
    assert r1 == r2
