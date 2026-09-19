# 匿名评审分配 API（Anonymous Review Assignment）

基于 Python + FastAPI 的评审分配服务。`POST /assign` 将「稿件—评审人」分配问题整体规约为
**全局最大流**（Edmonds-Karp，非逐稿贪心），一次求出同时满足以下全部约束的完整分配：

- 每篇稿件获得指定数量的评审人；
- 评审人主题能力与稿件主题匹配；
- 回避利益冲突（含自稿冲突）；
- 评审人分配总量不超过其容量。

无可行解时返回未满足稿件及候选/容量缺口诊断，**不输出任何部分分配结果**；
相同输入（甚至不同的列表顺序）必然产生完全相同的输出。服务无状态，不持久化任何数据。

请求还可携带 `previous_assignments` / `locked_assignments` 进入**增量重排模式**：
锁定关系强制保留，其余名额在全局可行前提下优先复用历史关系，使新增与移除关系总数最少。

## 快速启动（单条命令）

```bash
docker compose up --build
```

依赖安装与初始化全部在镜像构建与容器启动时完成，无需任何额外步骤。

启动后访问：

| 地址 | 说明 |
| --- | --- |
| http://localhost:8000/health | 健康检查 |
| http://localhost:8000/assign | 分配接口（POST） |
| http://localhost:8000/docs | Swagger 交互式文档 |
| http://localhost:8000/redoc | ReDoc 文档 |

## 配置

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `PORT` | `8000` | 容器内服务监听端口 |

宿主机端口映射在 `docker-compose.yml` 的 `ports` 中调整（默认 `8000:8000`）。

不使用 Compose 时也可直接构建运行：

```bash
docker build -t review-assign .
docker run -p 8000:8000 review-assign
```

## API 说明

### `GET /health`

```json
{"status": "ok"}
```

### `POST /assign`

请求体：

```json
{
  "required_reviewers": 2,
  "manuscripts": [
    {"id": "m1", "authors": ["alice"], "topics": ["ml"]},
    {"id": "m2", "authors": ["bob"], "topics": ["ml", "db"], "required_reviewers": 1}
  ],
  "reviewers": [
    {"id": "r1", "topics": ["ml"], "capacity": 2},
    {"id": "r2", "topics": ["ml", "db"], "capacity": 1, "conflicted_authors": ["alice"]},
    {"id": "r3", "topics": ["db"], "capacity": 2, "conflicted_manuscripts": ["m1"]}
  ]
}
```

字段规则：

- `required_reviewers`（请求级，默认 `1`）：每稿默认所需评审数；稿件级
  `required_reviewers` 可对其覆盖。
- 稿件 `topics` 非空时，评审人须与其有交集才具备能力；稿件 `topics` 为空则不做主题限制。
- 冲突回避（满足任一即不可评审）：
  - 评审人 `id` 出现在稿件 `authors` 中（自稿冲突）；
  - 稿件作者与评审人 `conflicted_authors` 有交集；
  - 稿件 `id` 出现在评审人 `conflicted_manuscripts` 中。
- 稿件 / 评审人 `id` 必须各自唯一，否则返回 `422`。
- `previous_assignments` / `locked_assignments` 中引用不存在的稿件或评审 ID、
  或同一列表内出现重复的 `(manuscript_id, reviewer_id)` 对，返回 `422`；
  两个列表之间允许出现相同的关系对。

可行时响应（`200`；普通请求中 `lock_conflicts`、`reassignment` 恒为 `null`）：

```json
{
  "status": "feasible",
  "total_required": 3,
  "total_assigned": 3,
  "assignments": [
    {"manuscript_id": "m1", "reviewer_ids": ["r1", "r3"]},
    {"manuscript_id": "m2", "reviewer_ids": ["r2"]}
  ],
  "unsatisfied": null,
  "shared_pool": null,
  "lock_conflicts": null,
  "reassignment": null
}
```

不可行时响应（`200`，`assignments` 为 `null`，不输出部分结果）：

```json
{
  "status": "infeasible",
  "total_required": 4,
  "total_assigned": 2,
  "assignments": null,
  "unsatisfied": [
    {
      "manuscript_id": "m2",
      "required": 2,
      "assigned": 0,
      "missing": 2,
      "eligible_reviewers": 2,
      "eligible_remaining_capacity": 0,
      "candidate_gap": 0,
      "capacity_gap": 2
    }
  ],
  "shared_pool": {
    "manuscript_ids": ["m2"],
    "reviewer_ids": ["r1", "r2"],
    "missing_total": 2,
    "pool_remaining_capacity": 0,
    "pool_total_capacity": 2
  },
  "lock_conflicts": null,
  "reassignment": null
}
```

诊断字段含义：

- `eligible_reviewers`：通过主题与冲突过滤的合格候选人数；
- `eligible_remaining_capacity`：最大流结束后这些候选的剩余容量之和；
- `candidate_gap`：候选人数缺口（`required - eligible_reviewers`，候选本身不足）；
- `capacity_gap`：容量缺口（`missing - eligible_remaining_capacity`，候选够但容量不足）；
- `shared_pool`：所有未满足稿件共享的候选池及其总/剩余容量，用于定位整体瓶颈。

#### 增量重排模式（`previous_assignments` / `locked_assignments`）

请求体可额外携带两个可选字段（均为 `{manuscript_id, reviewer_id}` 对的列表）；
**任一字段非空即进入增量重排模式，两者都缺省或为空时行为与普通请求完全一致**：

- `previous_assignments`：上一轮已有的分配关系。其中已不再满足评审资格的关系
  视为自然失效（计入 `removed`，不判不可行）；其余名额在**全局可行前提下最大化复用**，
  使 `added + removed`（新增与移除关系总数）最少；多个最优解之间以
  `(稿件 ID, 评审 ID)` 全局顺序确定性决胜。
- `locked_assignments`：本轮必须保留的锁定关系。每条锁定关系都必须同时满足
  **稿件需求、评审资格、评审容量**；只要存在任一冲突，整体判定不可行、
  `assignments` 为 `null`，并在 `lock_conflicts` 中给出**全部**结构化原因（非仅第一条）。

> 注意：`locked_assignments` 只保证锁本身合法；锁定占满容量后若其余名额全局无解，
> 仍返回 `infeasible`，此时 `lock_conflicts` 为 `null`，走 `unsatisfied` 缺口诊断。
> 列表中引用不存在的 ID、或出现重复的 `(manuscript_id, reviewer_id)` 对，
> 属于请求格式错误，返回 `422`。

增量重排请求示例：

```json
{
  "required_reviewers": 1,
  "manuscripts": [
    {"id": "m1", "authors": [], "topics": ["ml"]},
    {"id": "m2", "authors": [], "topics": ["db"]}
  ],
  "reviewers": [
    {"id": "r1", "topics": ["ml"], "capacity": 1},
    {"id": "r2", "topics": ["ml", "db"], "capacity": 2}
  ],
  "previous_assignments": [
    {"manuscript_id": "m2", "reviewer_id": "r2"}
  ],
  "locked_assignments": [
    {"manuscript_id": "m1", "reviewer_id": "r1"}
  ]
}
```

可行时响应在普通字段外额外返回 `reassignment`（变更统计；普通请求该字段为 `null`）：

```json
{
  "status": "feasible",
  "total_required": 2,
  "total_assigned": 2,
  "assignments": [
    {"manuscript_id": "m1", "reviewer_ids": ["r1"]},
    {"manuscript_id": "m2", "reviewer_ids": ["r2"]}
  ],
  "unsatisfied": null,
  "shared_pool": null,
  "lock_conflicts": null,
  "reassignment": {
    "reused": 1,
    "added": 1,
    "removed": 0,
    "locked": 1,
    "total_changes": 1
  }
}
```

锁定关系冲突时（下例 m1 只需要 1 人却锁定了 2 人）：

```json
{
  "status": "infeasible",
  "total_required": 1,
  "total_assigned": 0,
  "assignments": null,
  "unsatisfied": [],
  "shared_pool": null,
  "lock_conflicts": [
    {
      "code": "over_required",
      "manuscript_id": "m1",
      "reviewer_id": null,
      "locked_count": 2,
      "limit": 1,
      "related_ids": ["r1", "r2"],
      "detail": "锁定评审 2 人，超过该稿件所需 1 人"
    }
  ],
  "reassignment": null
}
```

`lock_conflicts[].code` 取值：

| code | 触发条件 | 关键字段 |
| --- | --- | --- |
| `ineligible_self_author` | 评审人是该稿件作者（自稿冲突） | `manuscript_id`、`reviewer_id` |
| `ineligible_author_conflict` | 评审人与稿件作者存在利益冲突 | `manuscript_id`、`reviewer_id` |
| `ineligible_manuscript_confirmed` | 评审人已声明回避该稿件 | `manuscript_id`、`reviewer_id` |
| `ineligible_topics` | 评审人主题能力与稿件不匹配 | `manuscript_id`、`reviewer_id` |
| `over_required` | 该稿锁定人数超过其所需评审数 | `locked_count`、`limit`、`related_ids`（锁定的评审人） |
| `over_capacity` | 该评审被锁定篇数超过其容量 | `reviewer_id`、`locked_count`、`limit`、`related_ids`（锁定它的稿件） |

冲突按「资格 → 需求 → 容量」分组、组内按 ID 排序输出，顺序确定。
`reassignment` 字段含义：`reused` 为最终分配中复用 `previous_assignments` 的关系数；
`added` / `removed` 为相对历史新增 / 移除（含自然失效）的关系数；
`locked` 为生效的锁定关系数；`total_changes = added + removed`。

### 示例调用

```bash
curl -s http://localhost:8000/health

curl -s -X POST http://localhost:8000/assign \
  -H 'Content-Type: application/json' \
  -d '{
        "required_reviewers": 2,
        "manuscripts": [
          {"id": "m1", "authors": ["alice"], "topics": ["ml"]},
          {"id": "m2", "authors": ["bob"],   "topics": ["db"]}
        ],
        "reviewers": [
          {"id": "r1", "topics": ["ml"], "capacity": 2},
          {"id": "r2", "topics": ["ml", "db"], "capacity": 2},
          {"id": "r3", "topics": ["db"], "capacity": 1}
        ]
      }'

# 增量重排：锁定 m1->r1，并在可行前提下尽量复用上一轮的 m2->r3
curl -s -X POST http://localhost:8000/assign \
  -H 'Content-Type: application/json' \
  -d '{
        "required_reviewers": 2,
        "manuscripts": [
          {"id": "m1", "authors": ["alice"], "topics": ["ml"]},
          {"id": "m2", "authors": ["bob"],   "topics": ["db"]}
        ],
        "reviewers": [
          {"id": "r1", "topics": ["ml"], "capacity": 2},
          {"id": "r2", "topics": ["ml", "db"], "capacity": 2},
          {"id": "r3", "topics": ["db"], "capacity": 1}
        ],
        "previous_assignments": [
          {"manuscript_id": "m2", "reviewer_id": "r3"}
        ],
        "locked_assignments": [
          {"manuscript_id": "m1", "reviewer_id": "r1"}
        ]
      }'
```

## 算法与确定性

- 建图：`源 -> 稿件（容量=所需评审数）-> 评审人（容量=1，仅合格）-> 汇（容量=评审容量）`；
  最大流等于总需求即存在满足全部约束的分配，取其流即为解。
- 全局求解保证整体可行（逐稿贪心无法保证，例如 m1 可选 r1/r2、m2 仅可选 r1 且容量均为 1 时，
  贪心会失败而全局解为 m1→r2、m2→r1）。
- 所有节点按 ID 排序后建图，BFS 按固定邻接顺序执行，算法无随机性：
  相同输入（含不同的列表顺序）产生逐字节相同的输出。
- **增量重排**：锁定关系先通过资格 / 需求 / 容量三重校验并预占容量；残量网络上运行
  最小费用最大流（带势函数的 Dijkstra）：历史边费用为其 `(稿件 ID, 评审 ID)` 全局序号、
  非历史边费用为 `BASE + 序号`（`BASE` 大于全部序号之和），因此费用字典序地先最大化
  复用关系数（即最小化新增+移除），再最小化所选边序号和；等距增广路按节点序列
  字典序选路，ID 顺序唯一确定同成本解。任何冲突都在输出分配前整体判定，不产生部分结果。

## 本地开发与测试

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q                 # 运行测试
uvicorn app.main:app --reload --port 8000   # 本地起服务
```

## 项目结构

```
app/
  main.py      # FastAPI 入口：/health、/assign
  models.py    # 请求/响应模型与校验
  solver.py    # 全局最大流分配求解器（Edmonds-Karp）
tests/
  test_assign.py
Dockerfile
docker-compose.yml
requirements.txt
requirements-dev.txt
```
