"""交集分析服务：计算两个人群的交集、并集或差集。

数量与成员一律从圈选的完整名单计算；交集产物会记录本次计算实际读取的
两个名单版本（roster_id），复核时按这两个版本重新取名单，而不是取最新名单。
"""
from __future__ import annotations

from typing import Any, Callable

from app.store import store
from app.services.crowd_select import PREVIEW_LIMIT, get_roster_for_analysis

# 交集成员在结果里展示的条数上限：与圈选预览一致，只为页面展示。
MEMBER_PREVIEW_LIMIT = PREVIEW_LIMIT


def _apply_mode(users_a: set[str], users_b: set[str], mode: str) -> set[str]:
    if mode == "union":
        return users_a | users_b
    if mode in ("diff", "a_only"):
        return users_a - users_b
    if mode == "b_only":
        return users_b - users_a
    return users_a & users_b


def _member_sort_key(user_id: str) -> tuple[int, str]:
    """成员按统一顺序排列，预览截断不会漏掉“排序靠后但仍在交集里”的人。"""
    digits = "".join(ch for ch in user_id if ch.isdigit())
    return (int(digits) if digits else 0, user_id)


def analyze_overlap(
    task_id: str, between_reads: Callable[[str], None] | None = None
) -> dict[str, Any]:
    """执行交集分析，消费圈选的完整名单。

    between_reads 是两次名单读取之间的可选回调，回调收到已读取的一侧编号。
    生产调用不传；核对并发场景时可用它把一次圈选重算插进两次读取之间。
    """
    task = store.find_by_field("overlap_analysis", "task_id", task_id)
    if task is None:
        raise ValueError(f"Overlap task not found: {task_id}")

    crowd_a_id = task["crowd_a"]
    crowd_b_id = task["crowd_b"]
    intersect_mode = task["intersect_mode"]
    analysis_dims = task.get("analysis_dims", [])

    # 读取两侧完整名单（只有预览的旧名单不能参与计算）
    first_condition_id = crowd_a_id
    roster_a = get_roster_for_analysis(crowd_a_id)
    if between_reads is not None:
        between_reads(first_condition_id)
    roster_b = get_roster_for_analysis(crowd_b_id)

    users_a = set(roster_a["user_ids"])
    users_b = set(roster_b["user_ids"])

    result_users = _apply_mode(users_a, users_b, intersect_mode)

    # 维度分析
    dim_stats: dict[str, dict[str, int]] = {}
    for dim in analysis_dims:
        dim_stats[dim] = {}
        # 模拟维度分布
        for user in result_users:
            val = f"{dim}_val_{hash(user) % 5}"
            dim_stats[dim][val] = dim_stats[dim].get(val, 0) + 1

    sorted_members = sorted(result_users, key=_member_sort_key)
    versions = {
        "a": {"condition_id": crowd_a_id, "roster_id": roster_a["roster_id"],
              "roster_version": roster_a.get("version", "")},
        "b": {"condition_id": crowd_b_id, "roster_id": roster_b["roster_id"],
              "roster_version": roster_b.get("version", "")},
    }

    result = {
        "task_id": task_id,
        "crowd_a": crowd_a_id,
        "crowd_b": crowd_b_id,
        "intersect_mode": intersect_mode,
        "users_a_count": len(users_a),
        "users_b_count": len(users_b),
        # 数量来自完整名单交集，不受成员预览截断影响
        "result_count": len(result_users),
        "intersection_rate": len(users_a & users_b) / len(users_a) if users_a else 0.0,
        # 成员预览只展示前若干名
        "result_users": sorted_members[:MEMBER_PREVIEW_LIMIT],
        "member_preview_limit": MEMBER_PREVIEW_LIMIT,
        "result_users_truncated": len(sorted_members) > MEMBER_PREVIEW_LIMIT,
        # 本次计算实际读取的两个名单版本，复核以此为准
        "roster_a": roster_a["roster_id"],
        "roster_b": roster_b["roster_id"],
        "roster_versions": versions,
        "dim_stats": dim_stats,
        "result_table": task.get("result_table", ""),
    }

    # 存储产物
    store.set_artifact("overlap_analysis", task_id, result)

    # 更新状态，并在任务记录上留下两个名单版本供复核
    store.update("overlap_analysis", task["id"], {
        "status": "已完成",
        "pending": False,
        "analysis_roster_a": roster_a["roster_id"],
        "analysis_roster_b": roster_b["roster_id"],
        "analysis_roster_a_version": roster_a.get("version", ""),
        "analysis_roster_b_version": roster_b.get("version", ""),
        "result_count": len(result_users),
    })

    return result


def get_overlap_result(task_id: str) -> dict[str, Any] | None:
    """获取交集分析结果；已发布的数量只来自完整名单。"""
    artifact = store.get_artifact("overlap_analysis", task_id)
    if artifact:
        return artifact
    try:
        return analyze_overlap(task_id)
    except ValueError:
        return None


def review_overlap(task_id: str) -> dict[str, Any]:
    """按当次分析绑定的两个名单版本复核人数与成员。

    复核取的是记录上的版本；如果该版本在圈选重算后已不再完整，直接报错，
    而不是拿最新名单冒充当次结果。
    """
    task = store.find_by_field("overlap_analysis", "task_id", task_id)
    if task is None:
        raise ValueError(f"Overlap task not found: {task_id}")

    artifact = store.get_artifact("overlap_analysis", task_id)
    roster_a_id = (artifact or {}).get("roster_a") or task.get("analysis_roster_a")
    roster_b_id = (artifact or {}).get("roster_b") or task.get("analysis_roster_b")
    if not roster_a_id or not roster_b_id:
        raise ValueError(f"Overlap task has no bound rosters: {task_id}")

    roster_a = store.get_roster(task["crowd_a"], roster_a_id)
    roster_b = store.get_roster(task["crowd_b"], roster_b_id)
    if roster_a is None or roster_b is None:
        raise ValueError("Bound roster version not found")

    users_a = set(roster_a["user_ids"])
    users_b = set(roster_b["user_ids"])
    result_users = _apply_mode(users_a, users_b, task["intersect_mode"])
    sorted_members = sorted(result_users, key=_member_sort_key)

    return {
        "task_id": task_id,
        "roster_a": roster_a_id,
        "roster_b": roster_b_id,
        "roster_versions": {
            "a": {"condition_id": task["crowd_a"], "roster_id": roster_a_id,
                  "roster_version": roster_a.get("version", "")},
            "b": {"condition_id": task["crowd_b"], "roster_id": roster_b_id,
                  "roster_version": roster_b.get("version", "")},
        },
        "users_a_count": len(users_a),
        "users_b_count": len(users_b),
        "result_count": len(result_users),
        "result_users": sorted_members[:MEMBER_PREVIEW_LIMIT],
        "result_users_truncated": len(sorted_members) > MEMBER_PREVIEW_LIMIT,
        "matches_recorded": (
            len(result_users) == (artifact or {}).get("result_count", len(result_users))
        ),
    }
