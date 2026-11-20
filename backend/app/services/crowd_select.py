"""人群圈选服务：基于标签组合条件圈选用户。

圈选重算会产出一份完整名单（roster，kind=full），完整名单是交集分析计算
人数与成员的唯一依据。接口上的成员预览只截取前若干名，不代表完整名单。
"""
from __future__ import annotations

from typing import Any

from app.store import store

# 预览接口返回的成员上限：只用于页面展示，不参与任何数量计算。
PREVIEW_LIMIT = 500


def _matched_user_ids(condition: dict[str, Any]) -> list[str]:
    """按标签组合计算完整圈选名单（不截断）。"""
    tag_combination = condition.get("tag_combination", [])

    # 获取标签计算结果
    tag_results = store.list_artifacts("tag_compute")

    # 模拟圈选结果
    matched_users: list[str] = []
    for combo in tag_combination:
        tag_id = combo.get("tag_id", "")
        # 查找对应标签计算结果
        for result in tag_results:
            if result.get("tag_id") == tag_id:
                users = result.get("user_tag_values", [])
                for u in users:
                    user_id = u.get("user_id")
                    if u.get("tag_value") == combo.get("value") or u.get("tag_value") == True:
                        if user_id not in matched_users:
                            matched_users.append(user_id)

    # 如果无匹配，用默认值
    if not matched_users:
        matched_users = [f"U{i:05d}" for i in range(1, condition.get("size_estimate", 1000) + 1)]

    return matched_users


def select_crowd(condition_id: str) -> dict[str, Any]:
    """执行人群圈选，产出一份完整名单并更新生效状态。"""
    condition = store.find_by_field("crowd_select", "condition_id", condition_id)
    if condition is None:
        raise ValueError(f"Crowd condition not found: {condition_id}")

    matched_users = _matched_user_ids(condition)

    # 重算产出完整名单：版本号随每次重算递增
    previous = store.get_roster(condition_id)
    previous_version = int(previous["version"]) if previous else 0
    roster = store.save_roster(
        condition_id, matched_users, kind="full", version=str(previous_version + 1)
    )

    # 数量来自完整名单，不受预览截断影响
    result = _complete_result(condition, roster)

    # 存储产物
    store.set_artifact("crowd_select", condition_id, result)

    # 更新状态
    store.update("crowd_select", condition["id"], {
        "status": "已生效",
        "pending": False,
        "latest_roster_id": roster["roster_id"],
        "latest_roster_version": roster["version"],
    })

    return result


def _complete_result(condition: dict[str, Any], roster: dict[str, Any]) -> dict[str, Any]:
    """用一份完整的圈选名单构造结果：数量取整批，成员只给预览。"""
    return {
        "condition_id": condition["condition_id"],
        "condition_name": condition["condition_name"],
        "logic_expr": condition["logic_expr"],
        "matched_users": roster["user_ids"][:PREVIEW_LIMIT],
        "preview_limit": PREVIEW_LIMIT,
        "matched_count": len(roster["user_ids"]),
        "complete": True,
        "confirmed": True,
        "roster_id": roster["roster_id"],
        "roster_version": roster.get("version", ""),
        "output_set": condition["output_set"],
        "tag_combination": condition.get("tag_combination", []),
    }


def _preview_result(condition: dict[str, Any], roster: dict[str, Any]) -> dict[str, Any]:
    """把一份旧名单包装成预览结果：只有成员预览，没有整批依据。"""
    return {
        "condition_id": condition["condition_id"],
        "condition_name": condition["condition_name"],
        "logic_expr": condition["logic_expr"],
        "matched_users": roster["user_ids"][:PREVIEW_LIMIT],
        "preview_limit": PREVIEW_LIMIT,
        # 预览名单不代表完整名单，不提供可确认的数量
        "matched_count": None,
        "complete": False,
        "confirmed": False,
        "roster_id": roster["roster_id"],
        "roster_version": roster.get("version", ""),
        "output_set": condition["output_set"],
        "tag_combination": condition.get("tag_combination", []),
    }


def get_crowd_result(condition_id: str) -> dict[str, Any] | None:
    """获取圈选结果。

    优先返回完整名单构建的结果；没有完整名单时，只允许回查仅预览的旧结果，
    且该结果会被明确标记为不完整、无数量依据。
    """
    condition = store.find_by_field("crowd_select", "condition_id", condition_id)
    if condition is None:
        return None

    artifact = store.get_artifact("crowd_select", condition_id)
    if artifact is not None and artifact.get("complete"):
        return artifact

    roster = store.get_roster(condition_id)
    if roster is not None:
        return _complete_result(condition, roster)

    preview = store.get_roster_for_preview(condition_id)
    if preview is not None:
        return _preview_result(condition, preview)

    # 从没跑过的条件：实时圈选一次，产出完整名单
    return select_crowd(condition_id)


def rebuild_crowd(condition_id: str) -> dict[str, Any]:
    """圈选重算：重新产出一份并存版本的完整名单。"""
    condition = store.find_by_field("crowd_select", "condition_id", condition_id)
    if condition is None:
        raise ValueError(f"Crowd condition not found: {condition_id}")

    matched_users = _matched_user_ids(condition)

    previous = store.get_roster(condition_id)
    previous_version = int(previous["version"]) if previous else 0
    roster = store.save_roster(
        condition_id,
        matched_users,
        kind="full",
        version=str(previous_version + 1),
        extra={"recomputed_from": previous["roster_id"] if previous else None},
    )

    result = _complete_result(condition, roster)
    store.set_artifact("crowd_select", condition_id, result)
    store.update("crowd_select", condition["id"], {
        "status": "已生效",
        "pending": False,
        "latest_roster_id": roster["roster_id"],
        "latest_roster_version": roster["version"],
    })
    return result


def get_roster_for_analysis(condition_id: str) -> dict[str, Any]:
    """交集分析读取完整名单：只有预览的旧结果不能当作完整名单或空名单。"""
    roster = store.get_roster(condition_id)
    if roster is None:
        # 尚未产出完整名单：先完整圈选一次，避免拿预览名单参与计算
        select_crowd(condition_id)
        roster = store.get_roster(condition_id)
    if roster is None:
        raise ValueError(f"Complete crowd roster not found: {condition_id}")
    return roster
