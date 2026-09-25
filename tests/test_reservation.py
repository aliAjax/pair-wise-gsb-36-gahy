import tempfile
import unittest
from pathlib import Path

from app import DomainError
from waterright import DeskService, seed_demo


class ReservationDeskTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DeskService(Path(self.tmp.name) / "eco.db")
        self.ids = seed_demo(self.db)
        self.source = self.ids["北区水库"]
        self.target = self.ids["河口灌区"]
        # 大额挤占场景不受下游最小留存规则约束，本套件显式移除该规则；
        # 同时移除种子的夏季预留，让每个用例自行登记区间。
        with self.db.storage.connect() as conn:
            conn.execute("DELETE FROM impact_rules")
            conn.execute("DELETE FROM reservations")

    def tearDown(self):
        self.tmp.cleanup()

    def _reserve(self, account, amount, start, end, actor="eco-dispatcher", **extra):
        payload = {"account_id": account, "amount": amount, "valid_from": start, "valid_to": end,
                   "basis": "夏季生态流量预案", "operator": "eco-dispatcher"}
        payload.update(extra)
        return self.db.confirm_reservation(actor, payload, "editor")

    def _pending_transfer(self, amount, day):
        return self.db.create_transfer(
            "alice", {"from_account_id": self.source, "to_account_id": self.target,
                      "amount": amount, "effective_date": day}, "editor")

    # ---------- 登记资料与区间交叠 ----------
    def test_reservation_registers_dates_basis_operator_and_rejects_overlap(self):
        saved = self._reserve(self.source, 120, "2026-09-01", "2026-09-30")
        self.assertEqual(saved["basis"], "夏季生态流量预案")
        self.assertEqual(saved["operator"], "eco-dispatcher")
        self.assertEqual(saved["state"], "active")
        # 相邻区间允许（首尾相接不算交叠）；有一天重合就拒绝。
        ok = self.db.preview_reservation("eco-dispatcher", {
            "account_id": self.source, "amount": 10, "valid_from": "2026-10-01",
            "valid_to": "2026-10-10", "basis": "x", "operator": "o"}, "editor")
        self.assertEqual(ok["squeezed"], [])
        with self.assertRaisesRegex(DomainError, "交叠"):
            self._reserve(self.source, 10, "2026-09-20", "2026-10-02")

    def test_role_required_for_reservation(self):
        with self.assertRaisesRegex(DomainError, "生态调度员"):
            self.db.preview_reservation("v", {"account_id": self.source, "amount": 1,
                                              "valid_from": "2026-09-01", "valid_to": "2026-09-02",
                                              "basis": "b"}, "viewer")

    def test_reservation_window_must_inside_license_and_positive(self):
        with self.assertRaisesRegex(DomainError, "许可有效期"):
            self._reserve(self.source, 10, "2026-09-01", "2027-01-05")
        with self.assertRaisesRegex(DomainError, "预留水量必须大于 0"):
            self._reserve(self.source, 0, "2026-09-01", "2026-09-02")

    def test_reservation_cannot_exceed_capacity(self):
        # 区间外 2026-03 的 200 待审预占压减可预留容量：1000-100-200=700。
        self._pending_transfer(200, "2026-03-15")
        with self.assertRaisesRegex(DomainError, "可预留额度"):
            self._reserve(self.source, 701, "2026-09-01", "2026-09-30")

    # ---------- 试算与挤占 ----------
    def test_preview_shows_availability_change_and_squeezed_transfers(self):
        # 许可 1000、已用 100、无区间外预占；9 月窗口容量 900。
        # 转让 850 在区间内，普通余量剩 50；预留 200 时缺口 = 850-(900-200)=150。
        t = self._pending_transfer(850, "2026-09-15")
        preview = self.db.preview_reservation("eco-dispatcher", {
            "account_id": self.source, "amount": 200, "valid_from": "2026-09-01",
            "valid_to": "2026-09-30", "basis": "补充预留", "operator": "eco-dispatcher"}, "editor")
        self.assertAlmostEqual(preview["available_change"], -50)  # 50 -> 0，可用不会为负
        self.assertEqual(preview["availability_before"]["ecology_reserved"], 0)
        self.assertAlmostEqual(preview["availability_after"]["ecology_reserved"], 200)
        self.assertAlmostEqual(preview["availability_before"]["available"], 50)
        self.assertAlmostEqual(preview["availability_after"]["available"], 0)
        # 但扣减后额度（不夹待审预占）实打实下降 200。
        self.assertAlmostEqual(
            preview["availability_after"]["effective_quota"] - preview["availability_before"]["effective_quota"],
            -200)
        self.assertEqual([s["transfer_id"] for s in preview["squeezed"]], [t["id"]])
        self.assertAlmostEqual(preview["squeezed"][0]["shortfall"], 150)
        # 试算不落库。
        self.assertNotIn(200, [r["amount"] for r in self.db.list_reservations()
                               if r["valid_from"] == "2026-09-01"])

    def test_confirm_keeps_conflicts_in_pending_area_and_blocks_direct_approval(self):
        t = self._pending_transfer(850, "2026-09-15")
        saved = self._reserve(self.source, 200, "2026-09-01", "2026-09-30")
        self.assertEqual(len(saved["conflict_ids"]), 1)
        pending = self.db.list_conflicts("pending")
        self.assertEqual([c["transfer_id"] for c in pending], [t["id"]])
        self.assertAlmostEqual(pending[0]["shortfall"], 150)
        with self.assertRaisesRegex(DomainError, "生态预留台"):
            self.db.approve_transfer(t["id"], "bob", "reviewer")

    # ---------- 生效后的额度扣减 ----------
    def test_transfer_and_usage_use_reduced_quota(self):
        self._reserve(self.source, 200, "2026-09-01", "2026-09-30")
        # 区间内可用 700；701 失败。
        with self.assertRaisesRegex(DomainError, "生态预留"):
            self._pending_transfer(701, "2026-09-10")
        self._pending_transfer(300, "2026-09-10")
        # 取水受同样扣减：700-300=400，取 401 失败。
        with self.assertRaisesRegex(DomainError, "生态预留"):
            self.db.record_usage("m", {"account_id": self.source, "amount": 401,
                                       "meter_event_id": "X1", "occurred_at": "2026-09-11"}, "meter")
        self.db.record_usage("m", {"account_id": self.source, "amount": 100,
                                   "meter_event_id": "X2", "occurred_at": "2026-09-11"}, "meter")
        # 区间外（6 月）不受该预留影响。
        self.db.record_usage("m", {"account_id": self.source, "amount": 50,
                                   "meter_event_id": "X3", "occurred_at": "2026-06-11"}, "meter")

    def test_available_reports_hold_by_date(self):
        self._reserve(self.source, 200, "2026-09-01", "2026-09-30")
        self.assertEqual(self.db.available(self.source, "2026-06-30")["ecology_reserved"], 0)
        self.assertEqual(self.db.available(self.source, "2026-09-01")["ecology_reserved"], 200)
        self.assertEqual(self.db.available(self.source, "2026-10-01")["ecology_reserved"], 0)

    def test_drought_uses_reduced_quota(self):
        self._reserve(self.source, 200, "2026-09-01", "2026-09-30")
        sim = self.db.simulate_drought(1000, 0.0, as_of="2026-09-11")
        up = next(a for a in sim["allocations"] if a["account_id"] == self.source)
        self.assertEqual(up["ecology_reserved"], 200)
        self.assertEqual(up["effective_quota"], 800)
        # 剩余可分配 = 800 - 已用 100 = 700，全部拿到。
        self.assertAlmostEqual(up["allocation"], 700)
        self.assertAlmostEqual(up["deficit"], 0)
        # 区间外模拟时预留不扣减。
        sim_out = self.db.simulate_drought(1000, 0.0, as_of="2026-06-01")
        up_out = next(a for a in sim_out["allocations"] if a["account_id"] == self.source)
        self.assertEqual(up_out["effective_quota"], 1000)

    # ---------- 动用预留 ----------
    def test_occupy_conflict_moves_quota_and_tracks_occupation(self):
        t = self._pending_transfer(850, "2026-09-15")
        saved = self._reserve(self.source, 200, "2026-09-01", "2026-09-30")
        cid = saved["conflict_ids"][0]
        with self.assertRaisesRegex(DomainError, "不能审批自己"):
            self.db.occupy_conflict(cid, "alice", "reviewer")
        result = self.db.occupy_conflict(cid, "bob", "reviewer")
        self.assertEqual(result["status"], "occupied")
        self.assertEqual(result["transfer_id"], t["id"])
        self.assertAlmostEqual(result["occupied_amount"], 150)  # 只记缺口
        record = next(r for r in self.db.list_reservations() if r["id"] == saved["id"])
        self.assertAlmostEqual(record["occupied_amount"], 150)
        self.assertEqual(self.db.list_conflicts("pending"), [])
        approved = next(x for x in self.db.list_transfers() if x["id"] == t["id"])
        self.assertEqual(approved["status"], "approved")
        # 划转后北区 150、河口 1350；预留仍持有 50。
        self.assertEqual(self.db.available(self.source, "2026-09-16")["quota"], 150)
        self.assertAlmostEqual(self.db.available(self.source, "2026-09-16")["ecology_reserved"], 50)

    def test_occupy_rejects_when_remaining_hold_insufficient(self):
        # 两笔区间内待审转让合计 900，用满全部普通余量；预留只有 100。
        # 排队口径下两笔累计动用预留均为 50；先批走第一笔，预留全部耗尽，第二笔即被拒。
        first = self._pending_transfer(850, "2026-09-15")
        second = self._pending_transfer(50, "2026-09-20")
        self._reserve(self.source, 100, "2026-09-01", "2026-09-30")
        pending = self.db.list_conflicts("pending")
        self.assertEqual({c["transfer_id"] for c in pending}, {first["id"], second["id"]})
        first_cid = next(c["id"] for c in pending if c["transfer_id"] == first["id"])
        second_cid = next(c["id"] for c in pending if c["transfer_id"] == second["id"])
        first_result = self.db.occupy_conflict(first_cid, "bob", "reviewer")
        self.assertAlmostEqual(first_result["occupied_amount"], 50)
        with self.assertRaisesRegex(DomainError, "可占用水量不足"):
            self.db.occupy_conflict(second_cid, "bob", "reviewer")

    def test_reject_transfer_closes_conflicts(self):
        t = self._pending_transfer(850, "2026-09-15")
        saved = self._reserve(self.source, 200, "2026-09-01", "2026-09-30")
        self.db.reject_transfer(t["id"], "bob", "reviewer")
        conflicts = self.db.list_conflicts()
        self.assertEqual(conflicts[0]["status"], "rejected")
        self.assertEqual(self.db.list_conflicts("pending"), [])
        # 转让退回后预占解除，预留仍完整持有。
        self.assertAlmostEqual(self.db.available(self.source, "2026-09-15")["ecology_reserved"], 200)

    def test_dismiss_conflict_only_updates_pending_area(self):
        self._pending_transfer(850, "2026-09-15")
        saved = self._reserve(self.source, 200, "2026-09-01", "2026-09-30")
        cid = saved["conflict_ids"][0]
        self.db.dismiss_conflict(cid, "bob", "reviewer")
        self.assertEqual(self.db.list_conflicts("pending"), [])
        # 转让仍待审。
        transfer = self.db.list_transfers()[0]
        self.assertEqual(transfer["status"], "pending")

    # ---------- 提前解除：只恢复未占用部分 ----------
    def test_early_release_restores_only_unoccupied_part(self):
        t = self._pending_transfer(850, "2026-09-15")
        saved = self._reserve(self.source, 200, "2026-09-01", "2026-09-30")
        self.db.occupy_conflict(saved["conflict_ids"][0], "bob", "reviewer")
        # 200 中 150 已随转让占用并划转，持有剩 50。
        self.assertAlmostEqual(self.db.available(self.source, "2026-09-16")["ecology_reserved"], 50)
        released = self.db.release_reservation("eco-dispatcher",
                                               {"reservation_id": saved["id"], "release_date": "2026-09-16",
                                                "reason": "来水偏丰"}, "editor")
        self.assertAlmostEqual(released["restored_amount"], 50)
        self.assertAlmostEqual(released["retained_occupied"], 150)
        self.assertEqual(self.db.available(self.source, "2026-09-16")["ecology_reserved"], 0)
        record = next(r for r in self.db.list_reservations() if r["id"] == saved["id"])
        self.assertEqual(record["status"], "released")
        self.assertAlmostEqual(record["occupied_amount"], 150)
        self.assertEqual(record["release_reason"], "来水偏丰")
        with self.assertRaisesRegex(DomainError, "已解除"):
            self.db.release_reservation("eco-dispatcher",
                                        {"reservation_id": saved["id"], "release_date": "2026-09-17",
                                         "reason": "again"}, "editor")

    def test_release_before_start_restores_all_held(self):
        saved = self._reserve(self.source, 200, "2026-09-01", "2026-09-30")
        released = self.db.release_reservation("eco-dispatcher",
                                               {"reservation_id": saved["id"], "release_date": "2026-08-15",
                                                "reason": "计划取消"}, "editor")
        self.assertAlmostEqual(released["restored_amount"], 200)
        self.assertEqual(self.db.available(self.source, "2026-09-10")["ecology_reserved"], 0)

    def test_release_after_end_is_refused(self):
        saved = self._reserve(self.source, 200, "2026-09-01", "2026-09-30")
        with self.assertRaisesRegex(DomainError, "已到期"):
            self.db.release_reservation("eco-dispatcher",
                                        {"reservation_id": saved["id"], "release_date": "2026-10-01",
                                         "reason": "晚了"}, "editor")

    def test_can_register_in_released_span_but_not_in_held_span(self):
        saved = self._reserve(self.source, 200, "2026-09-01", "2026-09-30")
        self.db.release_reservation("eco-dispatcher",
                                    {"reservation_id": saved["id"], "release_date": "2026-09-15",
                                     "reason": "提前"}, "editor")
        # 解除日之后（16 日起）可以登记新预留。
        new = self._reserve(self.source, 80, "2026-09-16", "2026-09-25")
        self.assertEqual(new["state"], "active")
        # 与仍持有的 9/1~9/15 段交叠则拒绝。
        with self.assertRaisesRegex(DomainError, "交叠"):
            self._reserve(self.source, 80, "2026-09-14", "2026-09-14")


if __name__ == "__main__":
    unittest.main()
