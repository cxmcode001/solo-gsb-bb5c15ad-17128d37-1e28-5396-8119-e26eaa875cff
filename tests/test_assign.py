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
