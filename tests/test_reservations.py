import tempfile
import unittest
from pathlib import Path

from app import Database, DomainError, seed_demo


class ReservationFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "test.db")
        self.accounts = seed_demo(self.db)
        self.source = self.accounts["北区水库"]   # quota 1000, used 100
        self.target = self.accounts["河口灌区"]   # quota 500

    def tearDown(self):
        self.tmp.cleanup()

    def _seed_reservation_id(self):
        return self.db.list_reservations()[0]["id"]

    def test_registration_fields_and_no_overlap(self):
        with self.assertRaisesRegex(DomainError, "经办人"):
            self.db.create_reservation("alice", {"account_id": self.source, "start_date": "2026-05-01",
                                                 "end_date": "2026-05-31", "amount": 100, "basis": "令"}, "editor")
        with self.assertRaisesRegex(DomainError, "不能交叠"):
            # 演示数据已登记 2026-07-01~09-30 的待生效夏季预留。
            self.db.create_reservation("alice", {"account_id": self.source, "start_date": "2026-07-15",
                                                 "end_date": "2026-08-15", "amount": 50,
                                                 "basis": "令", "operator": "调度员"}, "editor")
        # 首尾相接不算交叠。
        tail = self.db.create_reservation("alice", {"account_id": self.source, "start_date": "2026-10-01",
                                                    "end_date": "2026-10-31", "amount": 50,
                                                    "basis": "秋汛令", "operator": "调度员"}, "editor")
        self.assertEqual(tail["status"], "pending")
        with self.assertRaisesRegex(DomainError, "403|只有生态调度员"):
            self.db.create_reservation("bob", {"account_id": self.source, "start_date": "2026-04-01",
                                               "end_date": "2026-04-30", "amount": 50,
                                               "basis": "令", "operator": "调度员"}, "viewer")

    def test_preview_shows_available_change_and_squeezed_transfers(self):
        # 放开演示数据里的下游最小留存约束，便于构造大额待审转让。
        self.db.set_impact_rule("alice", "upstream", "downstream", 0.1, "测试", "editor")
        # 待审转让 700，生效日落在拟登记的 5 月预留区间。
        self.db.create_transfer("alice", {"from_account_id": self.source, "to_account_id": self.target,
                                          "amount": 700, "effective_date": "2026-05-15"}, "editor")
        preview = self.db.preview_reservation("alice", {"account_id": self.source, "start_date": "2026-05-01",
                                                        "end_date": "2026-05-31", "amount": 300,
                                                        "basis": "生态令", "operator": "调度员"}, "editor")
        self.assertEqual(preview["available_before"], 200)          # 1000-100-700
        self.assertEqual(preview["available_after"], 0)
        self.assertEqual(preview["ecological_reserved_after"], 300)
        self.assertEqual(preview["conflict_count"], 1)
        squeezed = preview["squeezed"][0]
        self.assertEqual(squeezed["shortage"], 100)                 # 700-(1000-100-300)
        # 预检不落库。
        self.assertEqual([r for r in self.db.list_reservations() if r["basis"] == "生态令"], [])

    def test_activation_creates_conflict_and_resolves_on_reject(self):
        rid = self._seed_reservation_id()
        self.db.set_impact_rule("alice", "upstream", "downstream", 0.1, "测试", "editor")
        # 夏季预留还只是 pending 时，7 月转让可以发起，不扣减额度。
        transfer = self.db.create_transfer("alice", {"from_account_id": self.source, "to_account_id": self.target,
                                                     "amount": 750, "effective_date": "2026-07-15"}, "editor")
        activated = self.db.activate_reservation(rid, "alice", "editor", "2026-07-01")
        self.assertEqual(activated["status"], "active")
        open_conflicts = self.db.list_conflicts("open")
        self.assertEqual(len(open_conflicts), 1)
        self.assertEqual(open_conflicts[0]["transfer_id"], transfer["id"])
        self.assertEqual(open_conflicts[0]["shortage"], 50)         # 750-(1000-100-200)
        # 冲突未解除前不能批准。
        with self.assertRaisesRegex(DomainError, "待处理区"):
            self.db.approve_transfer(transfer["id"], "bob", "reviewer")
        # 退回后冲突记录留在台账里但自动转为 resolved。
        self.db.reject_transfer(transfer["id"], "bob", "reviewer")
        self.assertEqual(self.db.list_conflicts("open"), [])
        resolved = self.db.list_conflicts("all")[0]
        self.assertEqual(resolved["status"], "resolved")
        self.assertIn("rejected", resolved["note"])

    def test_transfer_usage_and_drought_use_reduced_quota(self):
        rid = self._seed_reservation_id()
        self.db.set_impact_rule("alice", "upstream", "downstream", 0.0, "测试", "editor")
        self.db.activate_reservation(rid, "alice", "editor", "2026-07-01")
        info = self.db.available(self.source, "2026-07-10")
        self.assertEqual(info["ecological_reserved"], 200)
        self.assertEqual(info["effective_quota"], 800)
        self.assertEqual(info["available"], 700)
        # 季节上限按扣减后的许可额度：800*0.35=280。
        self.db.record_usage("m1", {"account_id": self.source, "amount": 280,
                                    "meter_event_id": "UP-JUL-LOCK", "occurred_at": "2026-07-10"}, "meter")
        with self.assertRaisesRegex(DomainError, "季节配额"):
            self.db.record_usage("m2", {"account_id": self.source, "amount": 1,
                                        "meter_event_id": "UP-JUL-X", "occurred_at": "2026-07-11"}, "meter")
        # 窗口内转让按扣减后额度：1000-380-200=420。
        self.db.create_transfer("alice", {"from_account_id": self.source, "to_account_id": self.target,
                                          "amount": 420, "effective_date": "2026-08-01"}, "editor")
        with self.assertRaisesRegex(DomainError, "生态预留"):
            self.db.create_transfer("alice", {"from_account_id": self.source, "to_account_id": self.target,
                                              "amount": 1, "effective_date": "2026-08-02"}, "editor")
        sim = self.db.simulate_drought(500, 0.0, "2026-08-01")
        by_id = {a["account_id"]: a for a in sim["allocations"]}
        self.assertEqual(by_id[self.source]["effective_quota"], 800)
        self.assertAlmostEqual(by_id[self.source]["allocation"], 420)   # 高优先级先拿满
        self.assertAlmostEqual(by_id[self.target]["allocation"], 80)    # 剩余 80
        # 预留到期后锁定自动消失（记录仍在）。
        self.assertEqual(self.db.available(self.source, "2026-10-01")["ecological_reserved"], 0)

    def test_early_release_restores_only_unoccupied_part(self):
        r = self.db.create_reservation("alice", {"account_id": self.source, "start_date": "2026-05-01",
                                                 "end_date": "2026-05-31", "amount": 300,
                                                 "basis": "五月生态令", "operator": "调度员"}, "editor")
        self.db.activate_reservation(r["id"], "alice", "editor", "2026-05-01")
        # 先有一笔解除日之后的待审转让，再发生窗口内取水。
        self.db.create_transfer("alice", {"from_account_id": self.source, "to_account_id": self.target,
                                          "amount": 60, "effective_date": "2026-05-25"}, "editor")
        self.db.record_usage("m1", {"account_id": self.source, "amount": 200,
                                    "meter_event_id": "MAY-1", "occurred_at": "2026-05-10"}, "meter")
        self.db.record_usage("m2", {"account_id": self.source, "amount": 150,
                                    "meter_event_id": "MAY-2", "occurred_at": "2026-05-20"}, "meter")
        with self.assertRaisesRegex(DomainError, "已到期"):
            self.db.release_reservation(r["id"], "alice", "editor", "2026-05-31")
        released = self.db.release_reservation(r["id"], "alice", "editor", "2026-05-15")
        # 解除日后：取水 150 + 待审 60 占用，300-210=90 可恢复。
        self.assertEqual(released["status"], "released")
        self.assertEqual(released["restored_amount"], 90)
        # 解除日之后额度恢复：无锁定，待审转让仍在。
        after = self.db.available(self.source, "2026-05-25")
        self.assertEqual(after["ecological_reserved"], 0)
        self.assertEqual(after["available"], 1000 - 450 - 60)
        # 解除日之前仍按原记录锁定，历史判定不变。
        before = self.db.available(self.source, "2026-05-10")
        self.assertEqual(before["ecological_reserved"], 300)
        with self.assertRaisesRegex(DomainError, "只有生效中的预留"):
            self.db.release_reservation(r["id"], "alice", "editor", "2026-05-31")


if __name__ == "__main__":
    unittest.main()
