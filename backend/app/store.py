"""内存数据仓库：支持跨模块数据消费、版本快照和关联查询。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。
"""
from __future__ import annotations

from typing import Any

from app.seed import SEED_ROWS


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        # 数据产物存储：跨模块共享的数据结果
        self._artifacts: dict[str, dict[str, Any]] = {}
        # 版本快照
        self._snapshots: dict[str, list[dict[str, Any]]] = {}
        # 计算结果缓存
        self._compute_cache: dict[str, dict[str, Any]] = {}
        # 圈选名单版本：condition_id -> {roster_id -> roster}
        self._rosters: dict[str, dict[str, dict[str, Any]]] = {}
        self._next_roster_seq = 1
        # 下一个自增ID
        self._next_ids: dict[str, int] = {
            name: max((int(row.get("id", 0)) for row in self._tables[name]), default=0) + 1
            for name in self._tables
        }

    def module_names(self) -> list[str]:
        return sorted(self._tables)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def find_by_field(self, module: str, field: str, value: Any) -> dict[str, Any] | None:
        for row in self.rows(module):
            if row.get(field) == value:
                return row
        return None

    def find_all_by_field(self, module: str, field: str, value: Any) -> list[dict[str, Any]]:
        return [row for row in self.rows(module) if row.get(field) == value]

    def insert(self, module: str, row: dict[str, Any]) -> dict[str, Any]:
        """插入一条记录并分配自增ID。"""
        if "id" not in row:
            row["id"] = self._next_ids.get(module, 1)
            self._next_ids[module] = row["id"] + 1
        self._tables.setdefault(module, []).append(dict(row))
        return row

    def update(self, module: str, entry_id: int, updates: dict[str, Any]) -> dict[str, Any] | None:
        """更新一条记录。"""
        row = self.find(module, entry_id)
        if row is None:
            return None
        row.update(updates)
        return row

    def delete(self, module: str, entry_id: int) -> bool:
        """删除一条记录。"""
        rows = self.rows(module)
        for i, row in enumerate(rows):
            if int(row.get("id", 0)) == entry_id:
                rows.pop(i)
                return True
        return False

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}

    # ========== 数据产物管理 ==========
    def set_artifact(self, artifact_type: str, artifact_id: str, data: dict[str, Any]) -> None:
        """存储跨模块数据产物。"""
        if artifact_type not in self._artifacts:
            self._artifacts[artifact_type] = {}
        self._artifacts[artifact_type][artifact_id] = data

    def get_artifact(self, artifact_type: str, artifact_id: str) -> dict[str, Any] | None:
        """获取跨模块数据产物。"""
        return self._artifacts.get(artifact_type, {}).get(artifact_id)

    def list_artifacts(self, artifact_type: str) -> list[dict[str, Any]]:
        """列出某类型的所有产物。"""
        return list(self._artifacts.get(artifact_type, {}).values())

    def get_artifact_keys(self, artifact_type: str) -> list[str]:
        """获取某类型产物的所有键。"""
        return list(self._artifacts.get(artifact_type, {}).keys())

    # ========== 圈选名单版本管理 ==========
    def _roster_bucket(self, condition_id: str) -> dict[str, dict[str, Any]]:
        return self._rosters.setdefault(condition_id, {})

    def save_roster(
        self,
        condition_id: str,
        user_ids: list[str],
        *,
        kind: str,
        version: str = "",
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """保存一份圈选名单并返回带版本标识的名单记录。

        kind=full 是圈选重算产出的完整名单；kind=preview 只用于页面预览，
        不能代表完整名单，也不能当作“已确认的空名单”。
        """
        bucket = self._roster_bucket(condition_id)
        roster_id = f"{condition_id}@{self._next_roster_seq}"
        self._next_roster_seq += 1
        roster = {
            "roster_id": roster_id,
            "condition_id": condition_id,
            "kind": kind,
            "version": version,
            "complete": kind == "full",
            "confirmed": kind == "full",
            "user_ids": list(user_ids),
            "user_count": len(user_ids),
        }
        if extra:
            roster.update(extra)
        bucket[roster_id] = roster
        return roster

    def get_roster(self, condition_id: str, roster_id: str | None = None) -> dict[str, Any] | None:
        """按名单标识取名单；不传标识时返回该条件最新的完整名单。"""
        bucket = self._roster_bucket(condition_id)
        if roster_id is not None:
            return bucket.get(roster_id)
        latest: dict[str, Any] | None = None
        for roster in bucket.values():
            if roster.get("complete"):
                latest = roster
        return latest

    def get_roster_for_preview(self, condition_id: str, limit: int = 500) -> dict[str, Any] | None:
        """取用于预览的名单：有完整名单用完整名单，否则回退到只有预览的旧结果。"""
        bucket = self._roster_bucket(condition_id)
        roster = self.get_roster(condition_id)
        if roster is not None:
            return roster
        previews = [r for r in bucket.values() if r.get("kind") == "preview"]
        return previews[-1] if previews else None

    # ========== 快照管理 ==========
    def add_snapshot(self, snapshot_type: str, data: dict[str, Any]) -> None:
        """添加快照。"""
        self._snapshots.setdefault(snapshot_type, []).append(data)

    def get_latest_snapshot(self, snapshot_type: str) -> dict[str, Any] | None:
        """获取最新快照。"""
        snapshots = self._snapshots.get(snapshot_type, [])
        return snapshots[-1] if snapshots else None

    def list_snapshots(self, snapshot_type: str) -> list[dict[str, Any]]:
        """列出所有快照。"""
        return list(self._snapshots.get(snapshot_type, []))

    # ========== 计算缓存 ==========
    def set_compute_cache(self, cache_key: str, data: dict[str, Any]) -> None:
        """存储计算缓存。"""
        self._compute_cache[cache_key] = data

    def get_compute_cache(self, cache_key: str) -> dict[str, Any] | None:
        """获取计算缓存。"""
        return self._compute_cache.get(cache_key)

    def invalidate_compute_cache(self, prefix: str) -> None:
        """按前缀失效缓存。"""
        keys_to_remove = [k for k in self._compute_cache if k.startswith(prefix)]
        for k in keys_to_remove:
            del self._compute_cache[k]

    # ========== 跨模块关联查询 ==========
    def join(self, left_module: str, right_module: str, left_field: str, right_field: str) -> list[dict[str, Any]]:
        """执行简单的内存join。"""
        results: list[dict[str, Any]] = []
        left_rows = self.rows(left_module)
        right_rows = self.rows(right_module)
        right_index: dict[Any, list[dict[str, Any]]] = {}
        for r in right_rows:
            key = r.get(right_field)
            right_index.setdefault(key, []).append(r)
        for l in left_rows:
            key = l.get(left_field)
            for r in right_index.get(key, []):
                merged = dict(l)
                for rk, rv in r.items():
                    if rk not in merged:
                        merged[rk] = rv
                    else:
                        merged[f"{right_module}.{rk}"] = rv
                results.append(merged)
        return results


def query_filters(request: Any) -> dict[str, Any]:
    """从请求查询参数中提取过滤条件（排除分页字段）。"""
    params = dict(request.query_params)
    return {k: v for k, v in params.items() if k not in ("page", "size")}


store = Store()
