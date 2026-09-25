"""服务层：编排资料、判定与保存，形成生态预留台的业务用例。

HTTP 页面（app.py）只调用这里的方法。
"""
from __future__ import annotations

import sqlite3
from datetime import date
from typing import Any

from . import materials as mat
from . import rules
from .materials import DomainError
from .storage import Storage

ROLE_EDITOR = "editor"
ROLE_REVIEWER = "reviewer"
ROLE_METER = "meter"


class DeskService:
    def __init__(self, path: str | None = None):
        self.storage = Storage(path) if path is not None else Storage()

    # ---------- 内部小工具 ----------
    def _decorate_reservation(self, row: sqlite3.Row) -> dict[str, Any]:
        item = {k: row[k] for k in row.keys()}
        item["_start"] = date.fromisoformat(row["valid_from"])
        item["_end"] = date.fromisoformat(row["valid_to"])
        item["_released"] = date.fromisoformat(row["release_date"]) if row["release_date"] else None
        if row["status"] == "active":
            item["state"] = "active" if date.today() <= item["_end"] else "expired"
        else:
            item["state"] = "released"
        item["held_amount"] = float(row["amount"]) - float(row["occupied_amount"])
        return item

    @staticmethod
    def _public_reservation(item: dict[str, Any]) -> dict[str, Any]:
        """剥掉判定层用的内部日期字段，供 HTTP/JSON 输出。"""
        return {k: v for k, v in item.items() if not k.startswith("_")}

    def _all_reservations(self, conn: sqlite3.Connection, account_id: int) -> list[dict[str, Any]]:
        rows = conn.execute(
            "SELECT * FROM reservations WHERE account_id=? ORDER BY id", (account_id,)
        ).fetchall()
        return [self._decorate_reservation(r) for r in rows]

    def _active_reservations(self, conn: sqlite3.Connection, account_id: int) -> list[dict[str, Any]]:
        return [r for r in self._all_reservations(conn, account_id) if r["status"] == "active"]

    def _availability_on(self, conn: sqlite3.Connection, account: sqlite3.Row,
                         on_date: date) -> dict[str, float]:
        hold = rules.hold_on(self._active_reservations(conn, int(account["id"])), on_date)
        pending = self.storage.pending_outgoing(conn, int(account["id"]))
        return rules.availability(dict(account), hold, pending)

    # ---------- 账户 ----------
    def create_account(self, actor: str, payload: dict[str, Any], role: str = ROLE_EDITOR) -> dict[str, Any]:
        if role != ROLE_EDITOR:
            raise DomainError("只有配额管理员可以创建账户", 403)
        m = mat.read_account(payload)
        with self.storage.connect() as conn:
            try:
                account_id = self.storage.insert_account(conn, m)
            except sqlite3.IntegrityError as exc:
                raise DomainError("账户名称已存在", 409) from exc
            self.storage.audit_insert(conn, actor, "account.created", "account", account_id,
                                      {"name": m.name, "quota": m.quota})
            return dict(self.storage.get_account(conn, account_id))

    def list_accounts(self) -> list[dict[str, Any]]:
        today = date.today()
        with self.storage.connect() as conn:
            result = []
            for row in self.storage.list_accounts(conn):
                item = dict(row)
                info = self._availability_on(conn, row, today)
                item["ecology_reserved"] = info["ecology_reserved"]
                item["effective_quota"] = info["effective_quota"]
                item["available"] = info["available"]
                result.append(item)
            return result

    def available(self, account_id: int, as_of: str | None = None) -> dict[str, Any]:
        on_date = mat.parse_optional_date(as_of, "查询日期") or date.today()
        with self.storage.connect() as conn:
            account = self.storage.require_account(conn, account_id)
            info = self._availability_on(conn, account, on_date)
        info["account_id"] = account_id
        info["as_of"] = on_date.isoformat()
        return info

    # ---------- 规则 ----------
    def set_season_rule(self, actor: str, region: str, month: int, max_fraction: float,
                        note: str = "", role: str = ROLE_EDITOR) -> dict[str, Any]:
        if role != ROLE_EDITOR:
            raise DomainError("只有配额管理员可以设置季节规则", 403)
        if not 1 <= int(month) <= 12 or not 0 < float(max_fraction) <= 1:
            raise DomainError("月份或季节比例不合法")
        with self.storage.connect() as conn:
            self.storage.upsert_season_rule(conn, region, int(month), float(max_fraction), note)
            self.storage.audit_insert(conn, actor, "season_rule.saved", "region", None,
                                      {"region": region, "month": month, "max_fraction": max_fraction})
        return {"region": region, "month": int(month), "max_fraction": float(max_fraction), "note": note}

    def set_impact_rule(self, actor: str, source_region: str, target_region: str, min_source_fraction: float,
                        note: str = "", role: str = ROLE_EDITOR) -> dict[str, Any]:
        if role != ROLE_EDITOR:
            raise DomainError("只有配额管理员可以设置第三方影响规则", 403)
        if not 0 <= float(min_source_fraction) <= 1:
            raise DomainError("最小留存比例必须在 0 到 1 之间")
        with self.storage.connect() as conn:
            self.storage.upsert_impact_rule(conn, source_region, target_region, float(min_source_fraction), note)
            self.storage.audit_insert(conn, actor, "impact_rule.saved", "region", None,
                                      {"source": source_region, "target": target_region,
                                       "min_fraction": min_source_fraction})
        return {"source_region": source_region, "target_region": target_region,
                "min_source_fraction": float(min_source_fraction), "note": note}

    # ---------- 转让 ----------
    def create_transfer(self, actor: str, payload: dict[str, Any], role: str = ROLE_EDITOR) -> dict[str, Any]:
        if role != ROLE_EDITOR:
            raise DomainError("只有水权编辑人员可以发起转让", 403)
        m = mat.read_transfer(payload)
        with self.storage.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            source = self.storage.require_account(conn, m.from_account_id)
            target = self.storage.require_account(conn, m.to_account_id)
            eff = m.effective_date.isoformat()
            if not (source["valid_from"] <= eff <= source["valid_to"]):
                raise DomainError("转出账户在生效日无效", 409)
            if not (target["valid_from"] <= eff <= target["valid_to"]):
                raise DomainError("转入账户在生效日无效", 409)
            avail = self._availability_on(conn, source, m.effective_date)
            if rules.transfer_blocked(avail, m.amount):
                raise DomainError("可用额度不足，待审批转让预占和生态预留会占用额度", 409)
            if int(source["priority"]) > int(target["priority"]):
                raise DomainError("不能把较低优先级水量转给更高优先级账户", 409)
            impact = self.storage.get_impact_rule(conn, source["region"], target["region"])
            if impact and rules.minimum_left_violated(
                avail["effective_quota"], float(impact["min_source_fraction"]),
                avail["available"] - m.amount,
            ):
                raise DomainError("转让会违反下游第三方最小留存约束", 409)
            transfer_id = self.storage.insert_transfer(conn, m, actor)
            self.storage.audit_insert(conn, actor, "transfer.created", "transfer", transfer_id,
                                      {"source": m.from_account_id, "target": m.to_account_id,
                                       "amount": m.amount, "effective_date": eff})
            return dict(self.storage.get_transfer(conn, transfer_id))

    def approve_transfer(self, transfer_id: int, actor: str, role: str = ROLE_REVIEWER) -> dict[str, Any]:
        if role != ROLE_REVIEWER:
            raise DomainError("只有审核人可以批准转让", 403)
        with self.storage.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            transfer = self.storage.get_transfer(conn, transfer_id)
            if not transfer:
                raise DomainError("转让记录不存在", 404)
            if transfer["status"] != "pending":
                raise DomainError("该转让已处理，不能重复批准", 409)
            if actor == transfer["created_by"]:
                raise DomainError("发起人不能批准自己的转让", 403)
            conflicts = self.storage.pending_conflicts_for_transfer(conn, transfer_id)
            if conflicts:
                ids = ", ".join(str(c["id"]) for c in conflicts)
                raise DomainError(
                    f"该转让与生态预留冲突（待处理记录 {ids}），请在生态预留台占用预留或退回", 409,
                )
            self._approve_locked(conn, transfer, actor, exclude_id=transfer_id)
            return dict(self.storage.get_transfer(conn, transfer_id))

    def _approve_locked(self, conn: sqlite3.Connection, transfer: sqlite3.Row,
                        actor: str, exclude_id: int) -> None:
        """额度复核后把批准量在账户间划转。调用方须已开启事务。"""
        source = self.storage.require_account(conn, transfer["from_account_id"])
        target = self.storage.require_account(conn, transfer["to_account_id"])
        amount = float(transfer["amount"])
        effective = date.fromisoformat(transfer["effective_date"])
        hold = rules.hold_on(self._active_reservations(conn, int(source["id"])), effective)
        other_reserved = self.storage.pending_outgoing(conn, int(source["id"]), exclude_id=exclude_id)
        available = float(source["quota"]) - float(source["used"]) - hold - other_reserved
        if amount > available + rules.EPS:
            raise DomainError("审批时额度已被其他记录占用，不能批准", 409)
        impact = self.storage.get_impact_rule(conn, source["region"], target["region"])
        if impact and rules.minimum_left_violated(
            float(source["quota"]) - hold, float(impact["min_source_fraction"]), available - amount,
        ):
            raise DomainError("审批时下游最小留存约束不再满足", 409)
        self.storage.move_quota(conn, int(source["id"]), int(target["id"]), amount)
        self.storage.mark_transfer(conn, int(transfer["id"]), "approved", actor)
        self.storage.audit_insert(conn, actor, "transfer.approved", "transfer", int(transfer["id"]),
                                  {"amount": amount})

    def reject_transfer(self, transfer_id: int, actor: str, role: str = ROLE_REVIEWER) -> dict[str, Any]:
        if role != ROLE_REVIEWER:
            raise DomainError("只有审核人可以退回转让", 403)
        with self.storage.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self.storage.get_transfer(conn, transfer_id)
            if not row or row["status"] != "pending":
                raise DomainError("转让不存在或已经处理", 409)
            if actor == row["created_by"]:
                raise DomainError("发起人不能自行退回", 403)
            self.storage.mark_transfer(conn, transfer_id, "rejected", actor)
            self.storage.resolve_conflicts_for_transfer(conn, transfer_id, "rejected", "转让被退回")
            self.storage.audit_insert(conn, actor, "transfer.rejected", "transfer", transfer_id, {})
        return {"id": transfer_id, "status": "rejected"}

    def list_transfers(self) -> list[dict[str, Any]]:
        with self.storage.connect() as conn:
            return [dict(r) for r in self.storage.list_transfers(conn)]

    # ---------- 取水 ----------
    def record_usage(self, actor: str, payload: dict[str, Any], role: str = ROLE_METER) -> dict[str, Any]:
        if role not in {ROLE_METER, ROLE_EDITOR}:
            raise DomainError("只有计量员可以登记取水", 403)
        m = mat.read_usage(payload)
        with self.storage.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            account = self.storage.require_account(conn, m.account_id)
            if not (account["valid_from"] <= m.occurred_at.isoformat() <= account["valid_to"]):
                raise DomainError("取水日期不在许可有效期内", 409)
            avail = self._availability_on(conn, account, m.occurred_at)
            if rules.transfer_blocked(avail, m.amount):
                raise DomainError("取水超过扣减生态预留后的可用额度", 409)
            season = self.storage.get_season_rule(conn, account["region"], m.occurred_at.month)
            month_total = self.storage.month_usage(conn, m.account_id, m.occurred_at.strftime("%Y-%m"))
            if season and rules.seasonal_cap_exceeded(
                float(account["quota"]), float(season["max_fraction"]), month_total, m.amount,
            ):
                raise DomainError("本次取水超过该月份的季节配额", 409)
            try:
                usage_id = self.storage.insert_usage(conn, m, actor)
            except sqlite3.IntegrityError as exc:
                raise DomainError("计量事件已登记，不能重复计水", 409) from exc
            self.storage.add_used(conn, m.account_id, m.amount)
            self.storage.audit_insert(conn, actor, "usage.recorded", "account", m.account_id,
                                      {"amount": m.amount, "occurred_at": m.occurred_at.isoformat(),
                                       "meter_event_id": m.meter_event_id})
            return dict(self.storage.get_usage(conn, usage_id))

    # ---------- 生态预留：试算 ----------
    def _reservation_preview_data(self, conn: sqlite3.Connection, m: mat.ReservationMaterial,
                                  account: sqlite3.Row) -> dict[str, Any]:
        existing = self._all_reservations(conn, m.account_id)
        for res in existing:
            if res["status"] == "released" and res["_released"] is not None:
                res_end = res["_released"]
            else:
                res_end = res["_end"]
            # 已提前解除的预留只持有到解除日当天；之后区间恢复自由，可登记新预留。
            if rules.intervals_overlap(m.valid_from, m.valid_to, res["_start"], res_end):
                span = f"{res['valid_from']}~{res_end.isoformat()}"
                raise DomainError(
                    f"与同一账户已登记的预留 #{res['id']}（{span}）日期区间交叠", 409,
                )
        # 同一账户区间交叠已在上面拒绝；新区间内不再有其他生效预留。
        pending_rows = self.storage.pending_outgoing_from(conn, m.account_id)
        pending = []
        outside_pending = 0.0
        for row in pending_rows:
            item = {k: row[k] for k in row.keys()}
            item["_effective"] = date.fromisoformat(row["effective_date"])
            pending.append(item)
            if not (m.valid_from <= item["_effective"] <= m.valid_to):
                outside_pending += float(row["amount"])
        # 容量保守计算：已用量（不限日期）与区间外待审预占都视为不能动用；
        # 区间内待审预占正是下面 squeezed_transfers 的排队判定对象。
        capacity = max(0.0, float(account["quota"]) - float(account["used"]) - outside_pending)
        if m.amount > capacity + rules.EPS:
            raise DomainError(
                f"预留水量超过区间内可预留额度（{capacity:.3f}），已用量或区间外待审转让已占用其余额度", 409,
            )
        squeezed = rules.squeezed_transfers(pending, m.valid_from, m.valid_to, m.amount, capacity)
        hold_before = rules.hold_on(existing, m.valid_from)
        info_before = rules.availability(dict(account), hold_before,
                                         self.storage.pending_outgoing(conn, m.account_id))
        info_after = rules.availability(dict(account), hold_before + m.amount,
                                        self.storage.pending_outgoing(conn, m.account_id))
        return {
            "account_id": m.account_id,
            "account_name": account["name"],
            "amount": m.amount,
            "valid_from": m.valid_from.isoformat(),
            "valid_to": m.valid_to.isoformat(),
            "basis": m.basis,
            "operator": m.operator,
            "availability_before": {k: info_before[k] for k in
                                     ("quota", "used", "ecology_reserved", "reserved_outgoing",
                                      "effective_quota", "available")},
            "availability_after": {k: info_after[k] for k in
                                    ("quota", "used", "ecology_reserved", "reserved_outgoing",
                                     "effective_quota", "available")},
            "available_change": round(info_after["available"] - info_before["available"], 6),
            "squeezed": squeezed,
        }

    def preview_reservation(self, actor: str, payload: dict[str, Any],
                            role: str = ROLE_EDITOR) -> dict[str, Any]:
        if role != ROLE_EDITOR:
            raise DomainError("只有生态调度员可以登记预留", 403)
        m = mat.read_reservation(payload, actor)
        with self.storage.connect() as conn:
            account = self.storage.require_account(conn, m.account_id)
            if not (account["valid_from"] <= m.valid_from.isoformat()
                    and m.valid_to.isoformat() <= account["valid_to"]):
                raise DomainError("预留区间必须在账户许可有效期内", 409)
            preview = self._reservation_preview_data(conn, m, account)
        preview["confirmed"] = False
        return preview

    # ---------- 生态预留：确认 ----------
    def confirm_reservation(self, actor: str, payload: dict[str, Any],
                            role: str = ROLE_EDITOR) -> dict[str, Any]:
        if role != ROLE_EDITOR:
            raise DomainError("只有生态调度员可以确认预留", 403)
        m = mat.read_reservation(payload, actor)
        with self.storage.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            account = self.storage.require_account(conn, m.account_id)
            if not (account["valid_from"] <= m.valid_from.isoformat()
                    and m.valid_to.isoformat() <= account["valid_to"]):
                raise DomainError("预留区间必须在账户许可有效期内", 409)
            preview = self._reservation_preview_data(conn, m, account)
            reservation_id = self.storage.insert_reservation(conn, m)
            conflict_ids = []
            for item in preview["squeezed"]:
                note = f"预留 {reservation_id} 生效后缺口 {item['shortfall']:.3f}"
                cid = self.storage.insert_conflict(
                    conn, reservation_id, item["transfer_id"], item["shortfall"], note,
                )
                conflict_ids.append(cid)
            self.storage.audit_insert(conn, actor, "reservation.confirmed", "reservation", reservation_id,
                                      {"account_id": m.account_id, "amount": m.amount,
                                       "valid_from": m.valid_from.isoformat(),
                                       "valid_to": m.valid_to.isoformat(), "basis": m.basis,
                                       "squeezed_transfer_ids": [s["transfer_id"] for s in preview["squeezed"]]})
            row = self.storage.get_reservation(conn, reservation_id)
            result = self._public_reservation(self._decorate_reservation(row))
        result["squeezed"] = preview["squeezed"]
        result["conflict_ids"] = conflict_ids
        return result

    def list_reservations(self, account_id: int | None = None) -> list[dict[str, Any]]:
        with self.storage.connect() as conn:
            rows = self.storage.list_reservations(conn, account_id)
            return [self._public_reservation(self._decorate_reservation(r)) for r in rows]

    # ---------- 生态预留：待处理区 ----------
    def list_conflicts(self, status: str | None = None) -> list[dict[str, Any]]:
        with self.storage.connect() as conn:
            return [dict(r) for r in self.storage.list_conflicts(conn, status)]

    def occupy_conflict(self, conflict_id: int, actor: str, role: str = ROLE_REVIEWER) -> dict[str, Any]:
        """审核人确认被挤到的转让动用预留水量。

        缺口（shortfall）是该转让必须动用预留才能成立的部分，计入预留占用、随转让划转；
        预留其余未动用部分继续保留，提前解除时仍可恢复。
        """
        if role != ROLE_REVIEWER:
            raise DomainError("只有审核人可以确认占用预留", 403)
        with self.storage.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conflict = self.storage.get_conflict(conn, conflict_id)
            if not conflict:
                raise DomainError("待处理记录不存在", 404)
            if conflict["status"] != "pending":
                raise DomainError("该待处理记录已处理", 409)
            reservation = self.storage.get_reservation(conn, conflict["reservation_id"])
            transfer = self.storage.get_transfer(conn, conflict["transfer_id"])
            if not transfer or transfer["status"] != "pending":
                self.storage.resolve_conflict(conn, conflict_id, "dismissed", "转让已不在待审状态")
                raise DomainError("对应转让已处理，冲突记录已关闭", 409)
            if actor == transfer["created_by"]:
                raise DomainError("发起人不能审批自己的转让", 403)
            amount = float(transfer["amount"])
            hold_draw = float(conflict["shortfall"])
            remaining_hold = float(reservation["amount"]) - float(reservation["occupied_amount"])
            if hold_draw > remaining_hold + rules.EPS:
                raise DomainError(
                    f"预留可占用水量不足：需要动用 {hold_draw:.3f}，仅剩 {remaining_hold:.3f}", 409,
                )
            # 复核普通额度：其他普通待审（不在待处理区的）仍按预占扣减；
            # 同样挂着预留冲突的待审转让由各自的冲突解决，不能在这里重复预占。
            source = self.storage.require_account(conn, int(transfer["from_account_id"]))
            target = self.storage.require_account(conn, int(transfer["to_account_id"]))
            effective = date.fromisoformat(transfer["effective_date"])
            other_rows = conn.execute(
                "SELECT id,amount FROM transfers WHERE from_account_id=? AND status='pending' AND id<>?",
                (int(source["id"]), int(transfer["id"])),
            ).fetchall()
            blocked_ids = {
                int(c["transfer_id"])
                for c in conn.execute(
                    "SELECT DISTINCT transfer_id FROM reservation_conflicts WHERE status='pending'"
                ).fetchall()
            }
            other_reserved = sum(float(r["amount"]) for r in other_rows
                                 if int(r["id"]) not in blocked_ids)
            free_available = (float(source["quota"]) - float(source["used"])
                              - rules.hold_on(self._active_reservations(conn, int(source["id"])), effective)
                              - other_reserved)
            if amount > free_available + hold_draw + rules.EPS:
                raise DomainError("审批时账户余量与剩余预留之和已不足以支撑该转让", 409)
            # 整笔水量在账户间划转；缺口部分记账为预留占用。
            self.storage.move_quota(conn, int(transfer["from_account_id"]),
                                    int(transfer["to_account_id"]), amount)
            self.storage.mark_transfer(conn, int(transfer["id"]), "approved", actor)
            self.storage.add_occupied(conn, int(reservation["id"]), hold_draw)
            self.storage.resolve_conflict(conn, conflict_id, "occupied",
                                          f"动用预留 {hold_draw:.3f}（转让 {amount:.3f}）")
            # 同一转让若还挂着同一转出账户其他预留的待处理记录，这笔水已划出，一并关闭。
            for other in self.storage.pending_conflicts_for_transfer(conn, int(transfer["id"])):
                if int(other["id"]) == conflict_id:
                    continue
                other_res = self.storage.get_reservation(conn, int(other["reservation_id"]))
                if other_res and int(other_res["account_id"]) == int(source["id"]):
                    self.storage.resolve_conflict(conn, int(other["id"]), "dismissed",
                                                  "转让已动用另一笔预留")
            self.storage.audit_insert(conn, actor, "reservation.conflict.occupied", "reservation_conflict",
                                      conflict_id,
                                      {"reservation_id": int(reservation["id"]),
                                       "transfer_id": int(transfer["id"]),
                                       "amount": amount, "hold_draw": hold_draw})
            return {"conflict_id": conflict_id, "status": "occupied", "transfer_id": int(transfer["id"]),
                    "occupied_amount": hold_draw, "transfer_amount": amount}

    def dismiss_conflict(self, conflict_id: int, actor: str, role: str = ROLE_REVIEWER) -> dict[str, Any]:
        """在待处理区登记人工处理结论（不改动转让本身）。"""
        if role != ROLE_REVIEWER:
            raise DomainError("只有审核人可以关闭待处理记录", 403)
        with self.storage.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            note = ""
            changed = self.storage.resolve_conflict(conn, conflict_id, "dismissed", note)
            if not changed:
                raise DomainError("待处理记录不存在或已处理", 404)
            self.storage.audit_insert(conn, actor, "reservation.conflict.dismissed",
                                      "reservation_conflict", conflict_id, {})
        return {"conflict_id": conflict_id, "status": "dismissed"}

    # ---------- 生态预留：提前解除 ----------
    def release_reservation(self, actor: str, payload: dict[str, Any],
                            role: str = ROLE_EDITOR) -> dict[str, Any]:
        if role != ROLE_EDITOR:
            raise DomainError("只有生态调度员可以解除预留", 403)
        m = mat.read_release(payload, actor)
        with self.storage.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self.storage.get_reservation(conn, m.reservation_id)
            if not row:
                raise DomainError("预留记录不存在", 404)
            if row["status"] != "active":
                raise DomainError("预留已解除，不能重复操作", 409)
            res = self._decorate_reservation(row)
            if m.on_date > res["_end"]:
                raise DomainError("预留已到期，到期记录自动失效，无需提前解除", 409)
            restored = rules.releasable_amount(res, m.on_date)
            retained = float(row["occupied_amount"])
            self.storage.update_reservation_release(
                conn, m.reservation_id, m.on_date.isoformat(), m.reason, m.operator,
            )
            self.storage.audit_insert(conn, actor, "reservation.released", "reservation",
                                      m.reservation_id,
                                      {"release_date": m.on_date.isoformat(), "restored": restored,
                                       "retained_occupied": retained, "reason": m.reason})
            result = self._public_reservation(self._decorate_reservation(
                self.storage.get_reservation(conn, m.reservation_id)))
        result["restored_amount"] = restored
        result["retained_occupied"] = retained
        return result

    # ---------- 干旱分配 ----------
    def simulate_drought(self, total_supply: float, reduction: float = 0.0,
                         as_of: str | None = None, role: str = "viewer") -> dict[str, Any]:
        try:
            total_supply, reduction = float(total_supply), float(reduction)
        except (TypeError, ValueError) as exc:
            raise DomainError("供水量和削减比例必须是数值") from exc
        if total_supply < 0 or not 0 <= reduction < 1:
            raise DomainError("供水量不能为负，削减比例应在 0 到 1 之间")
        on_date = mat.parse_optional_date(as_of, "模拟日期") or date.today()
        with self.storage.connect() as conn:
            accounts = self.storage.list_accounts(conn)
            effective_rows = []
            for row in accounts:
                item = dict(row)
                hold = rules.hold_on(self._active_reservations(conn, int(row["id"])), on_date)
                item["quota"] = max(0.0, float(row["quota"]) - hold)
                item["_license_quota"] = float(row["quota"])
                item["_hold"] = hold
                effective_rows.append(item)
            plan = rules.drought_plan(effective_rows, total_supply, reduction)
        return {"total_supply": total_supply, "reduction": reduction,
                "effective_supply": total_supply * (1 - reduction),
                "as_of": on_date.isoformat(),
                "unallocated": plan["remaining"], "allocations": [
                    {"account_id": int(r["id"]), "name": r["name"], "priority": r["priority"],
                     "license_quota": r["_license_quota"], "ecology_reserved": r["_hold"],
                     "effective_quota": r["quota"],
                     "allocation": plan["allocation"].get(int(r["id"]), 0.0),
                     "deficit": plan["deficit"].get(int(r["id"]), 0.0)}
                    for r in effective_rows
                ]}

    # ---------- 审计 ----------
    def audit(self) -> list[dict[str, Any]]:
        with self.storage.connect() as conn:
            return [dict(r) for r in self.storage.list_audit(conn)]


def seed_demo(db: DeskService) -> dict[str, int]:
    if db.list_accounts():
        return {a["name"]: int(a["id"]) for a in db.list_accounts()}
    ids: dict[str, int] = {}
    for payload in mat.DEMO_ACCOUNTS:
        account = db.create_account("alice", payload, ROLE_EDITOR)
        ids[payload["name"]] = int(account["id"])
    db.set_season_rule("alice", mat.DEMO_SEASON["region"], mat.DEMO_SEASON["month"],
                       mat.DEMO_SEASON["max_fraction"], mat.DEMO_SEASON["note"], ROLE_EDITOR)
    db.set_impact_rule("alice", mat.DEMO_IMPACT["source_region"], mat.DEMO_IMPACT["target_region"],
                       mat.DEMO_IMPACT["min_source_fraction"], mat.DEMO_IMPACT["note"], ROLE_EDITOR)
    db.record_usage("meter-01",
                    {"account_id": ids[mat.DEMO_USAGE["account"]],
                     "amount": mat.DEMO_USAGE["amount"],
                     "meter_event_id": mat.DEMO_USAGE["meter_event_id"],
                     "occurred_at": mat.DEMO_USAGE["occurred_at"]}, ROLE_METER)
    demo = mat.DEMO_RESERVATION
    db.confirm_reservation(
        "eco-dispatcher",
        {"account_id": ids[demo["account"]], "amount": demo["amount"],
         "valid_from": demo["valid_from"], "valid_to": demo["valid_to"],
         "basis": demo["basis"], "operator": demo["operator"]},
        ROLE_EDITOR,
    )
    return ids
