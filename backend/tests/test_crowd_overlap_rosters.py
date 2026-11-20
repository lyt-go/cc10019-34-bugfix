"""交集分析链路测试：完整名单、预览回退与名单版本复核。"""
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.store import store
from app.services import crowd_select as crowd_service
from app.services import overlap_analysis as overlap_service
from app.services.crowd_select import PREVIEW_LIMIT, get_crowd_result, select_crowd
from app.services.overlap_analysis import analyze_overlap, review_overlap


def _user_ids(start: int, end: int) -> list[str]:
    return [f"U{i:05d}" for i in range(start, end + 1)]


def _size_estimate(condition_id: str) -> int:
    return int(store.find_by_field("crowd_select", "condition_id", condition_id)["size_estimate"])


class TestCrowdOverlapRosterChain(unittest.TestCase):
    def setUp(self) -> None:
        store._artifacts.clear()
        store._compute_cache.clear()
        store._snapshots.clear()
        store._rosters.clear()
        store._next_roster_seq = 1

    def test_preview_never_truncates_counts(self) -> None:
        """超过 500 人的圈选：预览只给部分成员，数量仍按完整名单。"""
        crowd = select_crowd("CS-001")
        self.assertGreater(crowd["matched_count"], PREVIEW_LIMIT)
        self.assertEqual(len(crowd["matched_users"]), PREVIEW_LIMIT)
        self.assertTrue(crowd["complete"])

        roster = store.get_roster("CS-001", crowd["roster_id"])
        self.assertEqual(len(roster["user_ids"]), crowd["matched_count"])

        select_crowd("CS-002")
        result = analyze_overlap("OA-001")
        # 两边名单都超过 500，交集数量必须来自完整名单，而不是预览成员的交集。
        self.assertGreater(result["users_a_count"], PREVIEW_LIMIT)
        self.assertGreater(result["users_b_count"], PREVIEW_LIMIT)
        self.assertGreater(result["result_count"], PREVIEW_LIMIT)
        self.assertLessEqual(len(result["result_users"]), PREVIEW_LIMIT)
        self.assertTrue(result["result_users_truncated"])

    def test_members_beyond_preview_are_intersected(self) -> None:
        """只在第 600 名以后相交的两份名单，交集不能被预览截断成 0。"""
        roster_a = store.save_roster(
            "CS-001", _user_ids(1, 700), kind="full", version="1"
        )
        roster_b = store.save_roster(
            "CS-002", _user_ids(501, 900), kind="full", version="1"
        )
        result = analyze_overlap("OA-001")

        self.assertEqual(result["users_a_count"], 700)
        self.assertEqual(result["users_b_count"], 400)
        # 交集 U00501..U00700，全部落在 A 的 500 名预览之外
        self.assertEqual(result["result_count"], 200)
        self.assertEqual(result["roster_a"], roster_a["roster_id"])
        self.assertEqual(result["roster_b"], roster_b["roster_id"])

    def test_preview_only_legacy_result_is_not_a_complete_roster(self) -> None:
        """只有预览的旧结果可回查，但不能当完整名单，也不能当已确认的空名单。"""
        store.save_roster(
            "CS-001", _user_ids(1, PREVIEW_LIMIT), kind="preview", version="legacy"
        )
        store.save_roster(
            "CS-002", _user_ids(501, 500 + PREVIEW_LIMIT), kind="preview", version="legacy"
        )

        legacy = get_crowd_result("CS-001")
        self.assertIsNotNone(legacy)
        self.assertFalse(legacy["complete"])
        self.assertIsNone(legacy["matched_count"])
        self.assertLessEqual(len(legacy["matched_users"]), PREVIEW_LIMIT)

        # 交集分析不会把预览名单当成完整名单（这会得到 0 人），
        # 而是先补齐完整名单再发布数量。
        estimate_a = _size_estimate("CS-001")
        estimate_b = _size_estimate("CS-002")
        self.assertGreater(estimate_a, PREVIEW_LIMIT)
        self.assertGreater(estimate_b, PREVIEW_LIMIT)
        result = analyze_overlap("OA-001")
        self.assertEqual(result["users_a_count"], estimate_a)
        self.assertEqual(result["users_b_count"], estimate_b)
        self.assertEqual(result["result_count"], min(estimate_a, estimate_b))
        self.assertTrue(store.get_roster("CS-001")["complete"])

    def test_recompute_between_reads_binds_the_read_versions(self) -> None:
        """一侧重算插在两次读取之间：交集数量关联当次实际读取的两个版本。"""
        roster_a = store.save_roster(
            "CS-001", _user_ids(1, 700), kind="full", version="1"
        )
        roster_b_old = store.save_roster(
            "CS-002", _user_ids(501, 900), kind="full", version="1"
        )

        # 重算后的 B 名单移除了 U00501（该成员位于两份名单第 600 名以后）。
        recomputed_b = _user_ids(502, 900)
        original_rebuild = crowd_service.rebuild_crowd
        state: dict[str, str] = {}

        def rebuild_with_removed_member(condition_id: str) -> dict:
            state["recomputed"] = condition_id
            with mock.patch.object(
                crowd_service, "_matched_user_ids", return_value=recomputed_b
            ):
                return original_rebuild(condition_id)

        def between_reads(read_condition_id: str) -> None:
            state["first_read"] = read_condition_id
            # 重算插在读完 A、还没读 B 之间。
            rebuild_with_removed_member("CS-002")

        result = analyze_overlap("OA-001", between_reads=between_reads)

        self.assertEqual(state["first_read"], "CS-001")
        self.assertEqual(state["recomputed"], "CS-002")
        self.assertEqual(result["users_a_count"], 700)
        self.assertEqual(result["users_b_count"], 399)
        self.assertEqual(result["result_count"], 199)

        # 结果绑定的是当次实际读取的两个名单版本：A 仍是旧版本，B 是重算后的版本。
        self.assertEqual(result["roster_a"], roster_a["roster_id"])
        self.assertNotEqual(result["roster_b"], roster_b_old["roster_id"])
        self.assertEqual(result["roster_b"], store.get_roster("CS-002")["roster_id"])

        task = store.find_by_field("overlap_analysis", "task_id", "OA-001")
        self.assertEqual(task["analysis_roster_a"], roster_a["roster_id"])
        self.assertEqual(task["analysis_roster_b"], result["roster_b"])

    def test_review_uses_bound_versions_after_further_recompute(self) -> None:
        """复核取当次分析绑定的名单版本，即使后来圈选又重算过。"""
        roster_a = store.save_roster(
            "CS-001", _user_ids(1, 700), kind="full", version="1"
        )
        roster_b = store.save_roster(
            "CS-002", _user_ids(501, 900), kind="full", version="1"
        )
        result = analyze_overlap("OA-001")
        self.assertEqual(result["result_count"], 200)

        # 分析之后 B 又重算了一次，移除了交集里的成员；复核仍应按绑定的旧版本。
        store.save_roster("CS-002", _user_ids(600, 900), kind="full", version="2")

        reviewed = review_overlap("OA-001")
        self.assertEqual(reviewed["roster_a"], roster_a["roster_id"])
        self.assertEqual(reviewed["roster_b"], roster_b["roster_id"])
        self.assertEqual(reviewed["result_count"], result["result_count"])
        self.assertEqual(reviewed["users_b_count"], 400)
        self.assertTrue(reviewed["matches_recorded"])

    def test_legacy_preview_only_result_never_publishes_a_count(self) -> None:
        """只有预览的旧名单单独跑交集：要么补齐整批再发布，要么明确无整批依据。"""
        store.save_roster(
            "CS-001", _user_ids(1, PREVIEW_LIMIT), kind="preview", version="legacy"
        )
        store.save_roster(
            "CS-002", _user_ids(1, PREVIEW_LIMIT), kind="preview", version="legacy"
        )

        # 只做回查：拿到的是预览结果，明确无数量依据。
        for condition_id in ("CS-001", "CS-002"):
            legacy = get_crowd_result(condition_id)
            self.assertFalse(legacy["complete"])
            self.assertIsNone(legacy["matched_count"])
            self.assertFalse(legacy["confirmed"])

        # 跑交集：不允许 500 人预览被当作整批，必须先补齐完整名单。
        estimate_a = _size_estimate("CS-001")
        estimate_b = _size_estimate("CS-002")
        self.assertGreater(estimate_a, PREVIEW_LIMIT)
        self.assertGreater(estimate_b, PREVIEW_LIMIT)
        result = analyze_overlap("OA-001")
        self.assertEqual(result["users_a_count"], estimate_a)
        self.assertEqual(result["users_b_count"], estimate_b)
        self.assertGreater(result["result_count"], PREVIEW_LIMIT)
        for condition_id in ("CS-001", "CS-002"):
            roster = store.get_roster(condition_id, result["roster_a" if condition_id == "CS-001" else "roster_b"])
            self.assertTrue(roster["complete"])
            self.assertEqual(roster["user_count"], len(roster["user_ids"]))

        # 产物上的数量必须能在绑定的完整名单上复现。
        reviewed = review_overlap("OA-001")
        self.assertTrue(reviewed["matches_recorded"])

    def test_review_fails_when_bound_version_missing(self) -> None:
        """绑定版本缺失时复核报错，而不是拿最新名单顶替。"""
        store.save_roster("CS-001", _user_ids(1, 700), kind="full", version="1")
        store.save_roster("CS-002", _user_ids(501, 900), kind="full", version="1")
        analyze_overlap("OA-001")
        store._rosters["CS-001"].clear()

        with self.assertRaises(ValueError):
            review_overlap("OA-001")


if __name__ == "__main__":
    unittest.main(verbosity=2)
