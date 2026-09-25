"""保存层：SQLite 表结构、事务读写与冲突台账维护。"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from . import domain
from .domain import DomainError, EPS, parse_date
from .schema import SCHEMA_SQL

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "water_rights.db"


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: str | os.PathLike[str] = DEFAULT_DB):
        self.path = str(path)
        self._init_schema()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def _init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA_SQL)

    # -- 通用辅助 ---------------------------------------------------------

    def _audit(self, conn: sqlite3.Connection, actor: str, action: str, entity_type: str,
               entity_id: int | None, details: dict[str, Any]) -> None:
        conn.execute(
            "INSERT INTO audit_log(actor,action,entity_type,entity_id,details,created_at) VALUES(?,?,?,?,?,?)",
            (actor, action, entity_type, entity_id, json.dumps(details, ensure_ascii=False), utcnow()),
        )

    def _account_row(self, conn: sqlite3.Connection, account_id: int) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
        if not row:
            raise DomainError("水权账户不存在", 404)
        return row

    def _pending_total(self, conn: sqlite3.Connection, account_id: int,
                       exclude_transfer_id: int | None = None) -> float:
        sql = "SELECT COALESCE(SUM(amount),0) total FROM transfers WHERE from_account_id=? AND status='pending'"
        params: list[Any] = [account_id]
        if exclude_transfer_id is not None:
            sql += " AND id<>?"
            params.append(exclude_transfer_id)
        return float(conn.execute(sql, params).fetchone()["total"])

    def _effective_reservations(self, conn: sqlite3.Connection, account_id: int) -> list[sqlite3.Row]:
        """生效中或已解除的预留（待生效不锁额度）。"""
        return conn.execute(
            "SELECT * FROM reservations WHERE account_id=? AND status IN ('active','released') ORDER BY start_date,id",
            (account_id,),
        ).fetchall()

    def _locked(self, conn: sqlite3.Connection, account_id: int, start: date, end: date) -> float:
        return domain.locked_amount(self._effective_reservations(conn, account_id), account_id, start, end)

    def _headroom(self, conn: sqlite3.Connection, account: sqlite3.Row, day: date,
                  exclude_transfer_id: int | None = None) -> float:
        locked = self._locked(conn, account["id"], day, day)
        pending = self._pending_total(conn, account["id"], exclude_transfer_id)
        return float(account["quota"]) - float(account["used"]) - locked - pending

    def _availability(self, conn: sqlite3.Connection, account: sqlite3.Row, day: date) -> dict[str, Any]:
        locked = self._locked(conn, account["id"], day, day)
        pending = self._pending_total(conn, account["id"])
        effective = max(0.0, float(account["quota"]) - locked)
        value = max(0.0, effective - float(account["used"]) - pending)
        return {"account_id": account["id"], "as_of": day.isoformat(),
                "quota": account["quota"], "used": account["used"],
                "ecological_reserved": locked, "effective_quota": effective,
                "reserved_outgoing": pending, "available": value}

    # -- 账户与规则 -------------------------------------------------------

    def create_account(self, actor: str, payload: dict[str, Any], role: str = "editor") -> dict[str, Any]:
        if role != "editor":
            raise DomainError("只有配额管理员可以创建账户", 403)
        name = str(payload.get("name", "")).strip()
        region = str(payload.get("region", "")).strip()
        holder = str(payload.get("holder", "")).strip()
        if not name or not region or not holder:
            raise DomainError("账户名称、地区和持有人不能为空")
        try:
            priority = int(payload.get("priority"))
            quota = float(payload.get("quota"))
        except (TypeError, ValueError) as exc:
            raise DomainError("优先级和额度必须是数值") from exc
        if not 1 <= priority <= 5 or quota < 0:
            raise DomainError("优先级应在 1 到 5 之间，额度不能为负")
        valid_from = parse_date(str(payload.get("valid_from", "")), "生效日期")
        valid_to = parse_date(str(payload.get("valid_to", "")), "失效日期")
        if valid_from > valid_to:
            raise DomainError("生效日期不能晚于失效日期")
        with self.connect() as conn:
            try:
                cur = conn.execute(
                    "INSERT INTO accounts(name,region,holder,priority,valid_from,valid_to,quota,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (name, region, holder, priority, valid_from.isoformat(), valid_to.isoformat(), quota, utcnow()),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("账户名称已存在", 409) from exc
            self._audit(conn, actor, "account.created", "account", cur.lastrowid, {"name": name, "quota": quota})
            return dict(conn.execute("SELECT * FROM accounts WHERE id=?", (cur.lastrowid,)).fetchone())

    def set_season_rule(self, actor: str, region: str, month: int, max_fraction: float,
                        note: str = "", role: str = "editor") -> dict[str, Any]:
        if role != "editor":
            raise DomainError("只有配额管理员可以设置季节规则", 403)
        if not 1 <= int(month) <= 12 or not 0 < float(max_fraction) <= 1:
            raise DomainError("月份或季节比例不合法")
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO season_rules(region,month,max_fraction,note) VALUES(?,?,?,?)
                   ON CONFLICT(region,month) DO UPDATE SET max_fraction=excluded.max_fraction,note=excluded.note""",
                (region.strip(), int(month), float(max_fraction), note),
            )
            self._audit(conn, actor, "season_rule.saved", "region", None, {"region": region, "month": month, "max_fraction": max_fraction})
        return {"region": region, "month": month, "max_fraction": max_fraction, "note": note}

    def set_impact_rule(self, actor: str, source_region: str, target_region: str, min_source_fraction: float,
                        note: str = "", role: str = "editor") -> dict[str, Any]:
        if role != "editor":
            raise DomainError("只有配额管理员可以设置第三方影响规则", 403)
        if not 0 <= float(min_source_fraction) <= 1:
            raise DomainError("最小留存比例必须在 0 到 1 之间")
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO impact_rules(source_region,target_region,min_source_fraction,note) VALUES(?,?,?,?)
                   ON CONFLICT(source_region,target_region) DO UPDATE SET min_source_fraction=excluded.min_source_fraction,note=excluded.note""",
                (source_region, target_region, float(min_source_fraction), note),
            )
            self._audit(conn, actor, "impact_rule.saved", "region", None, {"source": source_region, "target": target_region, "min_fraction": min_source_fraction})
        return {"source_region": source_region, "target_region": target_region, "min_source_fraction": min_source_fraction, "note": note}

    # -- 生态预留 ---------------------------------------------------------

    def _parse_reservation_payload(self, payload: dict[str, Any], role: str) -> tuple[int, date, date, float, str, str]:
        if role != "editor":
            raise DomainError("只有生态调度员可以登记生态预留", 403)
        try:
            account_id = int(payload.get("account_id"))
            amount = float(payload.get("amount"))
        except (TypeError, ValueError) as exc:
            raise DomainError("账户和预留水量必须是数值") from exc
        if amount <= 0:
            raise DomainError("预留水量必须大于 0")
        basis = str(payload.get("basis", "")).strip()
        operator = str(payload.get("operator", "")).strip()
        if not basis:
            raise DomainError("预留依据不能为空")
        if not operator:
            raise DomainError("经办人不能为空")
        start = parse_date(str(payload.get("start_date", "")), "开始日期")
        end = parse_date(str(payload.get("end_date", "")), "结束日期")
        if start > end:
            raise DomainError("开始日期不能晚于结束日期")
        return account_id, start, end, amount, basis, operator

    def _overlap_live(self, conn: sqlite3.Connection, account_id: int, start: date, end: date,
                      exclude_id: int | None = None) -> sqlite3.Row | None:
        sql = ("SELECT * FROM reservations WHERE account_id=? AND status IN ('pending','active') "
               "AND start_date<=? AND end_date>=?")
        params: list[Any] = [account_id, end.isoformat(), start.isoformat()]
        if exclude_id is not None:
            sql += " AND id<>?"
            params.append(exclude_id)
        return conn.execute(sql, params).fetchone()

    def _squeezed_scan(self, conn: sqlite3.Connection, account: sqlite3.Row,
                       effective_rows: list[Any], target: Any) -> list[dict[str, Any]]:
        """按各待审转让的生效日逐点判定，找出被 target 预留挤到的记录。"""
        pending_total = self._pending_total(conn, account["id"])
        transfers = conn.execute(
            "SELECT * FROM transfers WHERE from_account_id=? AND status='pending' ORDER BY effective_date,id",
            (account["id"],),
        ).fetchall()
        squeezed: list[dict[str, Any]] = []
        for t in transfers:
            day = parse_date(t["effective_date"], "生效日期")
            if not domain.locked_for(target, day, day):
                continue
            locked = domain.locked_amount(effective_rows, account["id"], day, day)
            other_pending = pending_total - float(t["amount"])
            room = float(account["quota"]) - float(account["used"]) - locked - other_pending
            if float(t["amount"]) > room + EPS:
                squeezed.append({
                    "transfer_id": t["id"], "transfer": dict(t),
                    "effective_date": t["effective_date"],
                    "amount": float(t["amount"]), "shortage": max(0.0, float(t["amount"]) - room),
                })
        return squeezed

    def preview_reservation(self, actor: str, payload: dict[str, Any], role: str = "editor") -> dict[str, Any]:
        """申请预检：不落库，只返回可用额度变化与将被挤到的待审转让。"""
        account_id, start, end, amount, basis, operator = self._parse_reservation_payload(payload, role)
        with self.connect() as conn:
            account = self._account_row(conn, account_id)
            if not (account["valid_from"] <= start.isoformat() and end.isoformat() <= account["valid_to"]):
                raise DomainError("预留区间必须在账户有效期内", 409)
            if self._overlap_live(conn, account_id, start, end):
                raise DomainError("同一账户的预留日期区间不能交叠", 409)
            existing = list(self._effective_reservations(conn, account_id))
            synthetic = {"account_id": account_id, "start_date": start.isoformat(),
                         "end_date": end.isoformat(), "amount": amount, "status": "active",
                         "released_at": None}
            before = self._availability(conn, account, start)
            locked_after = domain.locked_amount(existing + [synthetic], account_id, start, end)
            pending = self._pending_total(conn, account_id)
            available_after = max(0.0, float(account["quota"]) - float(account["used"]) - locked_after - pending)
            squeezed = self._squeezed_scan(conn, account, existing + [synthetic], synthetic)
        return {
            "account_id": account_id, "start_date": start.isoformat(), "end_date": end.isoformat(),
            "amount": amount, "basis": basis, "operator": operator,
            "available_before": before["available"], "available_after": available_after,
            "ecological_reserved_before": before["ecological_reserved"],
            "ecological_reserved_after": locked_after,
            "squeezed": [{"transfer_id": s["transfer_id"], "effective_date": s["effective_date"],
                          "amount": s["amount"], "shortage": s["shortage"]} for s in squeezed],
            "conflict_count": len(squeezed),
        }

    def create_reservation(self, actor: str, payload: dict[str, Any], role: str = "editor") -> dict[str, Any]:
        """确认申请：登记预留，仍为待生效；冲突暂不产生，生效时再核定。"""
        account_id, start, end, amount, basis, operator = self._parse_reservation_payload(payload, role)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            account = self._account_row(conn, account_id)
            if not (account["valid_from"] <= start.isoformat() and end.isoformat() <= account["valid_to"]):
                raise DomainError("预留区间必须在账户有效期内", 409)
            if self._overlap_live(conn, account_id, start, end):
                raise DomainError("同一账户的预留日期区间不能交叠", 409)
            cur = conn.execute(
                """INSERT INTO reservations(account_id,start_date,end_date,amount,basis,operator,status,created_by,created_at)
                   VALUES(?,?,?,?,?,?, 'pending', ?,?)""",
                (account_id, start.isoformat(), end.isoformat(), amount, basis, operator, actor, utcnow()),
            )
            self._audit(conn, actor, "reservation.created", "reservation", cur.lastrowid,
                        {"account_id": account_id, "start_date": start.isoformat(),
                         "end_date": end.isoformat(), "amount": amount, "basis": basis, "operator": operator})
            row = dict(conn.execute("SELECT * FROM reservations WHERE id=?", (cur.lastrowid,)).fetchone())
        return row

    def _reservation_row(self, conn: sqlite3.Connection, reservation_id: int) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM reservations WHERE id=?", (reservation_id,)).fetchone()
        if not row:
            raise DomainError("生态预留记录不存在", 404)
        return row

    def _refresh_reservation_conflicts(self, conn: sqlite3.Connection, target_row: sqlite3.Row, actor: str) -> list[dict[str, Any]]:
        account = self._account_row(conn, target_row["account_id"])
        effective_rows = list(self._effective_reservations(conn, account["id"]))
        current = self._squeezed_scan(conn, account, effective_rows, target_row)
        current_ids = {s["transfer_id"]: s for s in current}
        existing = conn.execute(
            "SELECT * FROM reservation_conflicts WHERE reservation_id=?", (target_row["id"],),
        ).fetchall()
        for old in existing:
            if old["transfer_id"] not in current_ids and old["status"] == "open":
                transfer = conn.execute("SELECT status FROM transfers WHERE id=?", (old["transfer_id"],)).fetchone()
                reason = f"转让已{transfer['status']}" if transfer and transfer["status"] != "pending" else "额度已恢复，冲突自动解除"
                conn.execute(
                    "UPDATE reservation_conflicts SET status='resolved',note=?,resolved_at=?,resolved_by=? WHERE id=?",
                    (reason, utcnow(), actor, old["id"]),
                )
        for s in current:
            old = conn.execute(
                "SELECT * FROM reservation_conflicts WHERE reservation_id=? AND transfer_id=?",
                (target_row["id"], s["transfer_id"]),
            ).fetchone()
            if old is None:
                conn.execute(
                    """INSERT INTO reservation_conflicts(reservation_id,transfer_id,shortage,note,created_at)
                       VALUES(?,?,?, '预留生效后待审转让额度不足，待处理', ?)""",
                    (target_row["id"], s["transfer_id"], s["shortage"], utcnow()),
                )
            elif abs(float(old["shortage"]) - s["shortage"]) > EPS:
                conn.execute("UPDATE reservation_conflicts SET shortage=? WHERE id=?", (s["shortage"], old["id"]))
        return current

    def _refresh_open_conflicts(self, conn: sqlite3.Connection, actor: str) -> None:
        """任何会改变额度的操作后，重算所有仍挂起的冲突。"""
        rows = conn.execute(
            "SELECT DISTINCT reservation_id FROM reservation_conflicts WHERE status='open'",
        ).fetchall()
        for r in rows:
            target = self._reservation_row(conn, r["reservation_id"])
            self._refresh_reservation_conflicts(conn, target, actor)

    def activate_reservation(self, reservation_id: int, actor: str, role: str = "editor",
                             as_of: str | None = None) -> dict[str, Any]:
        if role != "editor":
            raise DomainError("只有生态调度员可以确认预留生效", 403)
        day = parse_date(as_of, "生效日期") if as_of else date.today()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._reservation_row(conn, reservation_id)
            if row["status"] != "pending":
                raise DomainError("只有待生效的预留可以确认生效", 409)
            start, end = parse_date(row["start_date"]), parse_date(row["end_date"])
            if day < start:
                raise DomainError("未到预留开始日期，不能提前生效", 409)
            if day > end:
                raise DomainError("预留已超过结束日期，不能生效", 409)
            conn.execute(
                "UPDATE reservations SET status='active',activated_by=?,activated_at=? WHERE id=?",
                (actor, utcnow(), reservation_id),
            )
            target = self._reservation_row(conn, reservation_id)
            conflicts = self._refresh_reservation_conflicts(conn, target, actor)
            self._audit(conn, actor, "reservation.activated", "reservation", reservation_id,
                        {"as_of": day.isoformat(), "conflicts": [c["transfer_id"] for c in conflicts]})
            out = dict(target)
        out["conflicts"] = [{"transfer_id": c["transfer_id"], "effective_date": c["effective_date"],
                             "amount": c["amount"], "shortage": c["shortage"]} for c in conflicts]
        return out

    def release_reservation(self, reservation_id: int, actor: str, role: str = "editor",
                            as_of: str | None = None) -> dict[str, Any]:
        """提前解除：解除日之后仍被取水/待审转让占用的部分不恢复。"""
        if role != "editor":
            raise DomainError("只有生态调度员可以提前解除预留", 403)
        day = parse_date(as_of, "解除日期") if as_of else date.today()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._reservation_row(conn, reservation_id)
            if row["status"] != "active":
                raise DomainError("只有生效中的预留可以提前解除", 409)
            start, end = parse_date(row["start_date"]), parse_date(row["end_date"])
            if day < start:
                raise DomainError("预留尚未生效，不能解除", 409)
            if day >= end:
                raise DomainError("预留已到期，无需提前解除", 409)
            # 解除日（不含）之后至原区间结束，已发生的取水与待审转让预占。
            later_usage = conn.execute(
                "SELECT COALESCE(SUM(amount),0) total FROM usage_records WHERE account_id=? AND occurred_at>? AND occurred_at<=?",
                (row["account_id"], day.isoformat(), end.isoformat()),
            ).fetchone()["total"]
            later_pending = conn.execute(
                "SELECT COALESCE(SUM(amount),0) total FROM transfers WHERE from_account_id=? AND status='pending' AND effective_date>? AND effective_date<=?",
                (row["account_id"], day.isoformat(), end.isoformat()),
            ).fetchone()["total"]
            restored = domain.restorable_amount(float(row["amount"]), float(later_usage) + float(later_pending))
            conn.execute(
                "UPDATE reservations SET status='released',released_by=?,released_at=?,restored_amount=? WHERE id=?",
                (actor, day.isoformat(), restored, reservation_id),
            )
            target = self._reservation_row(conn, reservation_id)
            conflicts = self._refresh_reservation_conflicts(conn, target, actor)
            self._audit(conn, actor, "reservation.released", "reservation", reservation_id,
                        {"as_of": day.isoformat(), "restored_amount": restored,
                         "occupied_after_release": float(later_usage) + float(later_pending),
                         "remaining_conflicts": [c["transfer_id"] for c in conflicts]})
            out = dict(target)
        out["conflicts"] = [{"transfer_id": c["transfer_id"], "effective_date": c["effective_date"],
                             "amount": c["amount"], "shortage": c["shortage"]} for c in conflicts]
        return out

    def list_reservations(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT r.*, a.name account_name FROM reservations r
                   JOIN accounts a ON a.id=r.account_id ORDER BY r.id DESC""",
            ).fetchall()
        return [dict(row) for row in rows]

    def list_conflicts(self, status: str = "open") -> list[dict[str, Any]]:
        sql = ("""SELECT c.*, r.start_date reservation_start, r.end_date reservation_end,
                         r.amount reservation_amount, r.status reservation_status,
                         t.from_account_id, t.to_account_id, t.amount transfer_amount,
                         t.effective_date, t.status transfer_status, a.name account_name
                  FROM reservation_conflicts c
                  JOIN reservations r ON r.id=c.reservation_id
                  JOIN transfers t ON t.id=c.transfer_id
                  JOIN accounts a ON a.id=r.account_id""")
        params: list[Any] = []
        if status != "all":
            sql += " WHERE c.status=?"
            params.append(status)
        sql += " ORDER BY c.status DESC, c.id DESC"
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    # -- 转让 -------------------------------------------------------------

    def create_transfer(self, actor: str, payload: dict[str, Any], role: str = "editor") -> dict[str, Any]:
        if role != "editor":
            raise DomainError("只有水权编辑人员可以发起转让", 403)
        try:
            source_id = int(payload.get("from_account_id"))
            target_id = int(payload.get("to_account_id"))
            amount = float(payload.get("amount"))
        except (TypeError, ValueError) as exc:
            raise DomainError("账户和转让量必须是数值") from exc
        if source_id == target_id or amount <= 0:
            raise DomainError("转让账户不能相同，转让量必须大于 0")
        effective = parse_date(str(payload.get("effective_date", "")), "生效日期")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            source = self._account_row(conn, source_id)
            target = self._account_row(conn, target_id)
            if not (source["valid_from"] <= effective.isoformat() <= source["valid_to"]):
                raise DomainError("转出账户在生效日无效", 409)
            if not (target["valid_from"] <= effective.isoformat() <= target["valid_to"]):
                raise DomainError("转入账户在生效日无效", 409)
            available = self._headroom(conn, source, effective)
            if amount > available + EPS:
                locked = self._locked(conn, source_id, effective, effective)
                hint = "生态预留" if locked > EPS else "待审批转让"
                raise DomainError(f"可用额度不足，{hint}会占用额度", 409)
            # 更高优先级（数字更小）用户的受保护水量不能转给较低优先级账户。
            if int(source["priority"]) > int(target["priority"]):
                raise DomainError("不能把较低优先级水量转给更高优先级账户", 409)
            impact = conn.execute(
                "SELECT * FROM impact_rules WHERE source_region=? AND target_region=?",
                (source["region"], target["region"]),
            ).fetchone()
            if impact:
                minimum = float(source["quota"]) * float(impact["min_source_fraction"])
                if available - amount + EPS < minimum:
                    raise DomainError("转让会违反下游第三方最小留存约束", 409)
            cur = conn.execute(
                "INSERT INTO transfers(from_account_id,to_account_id,amount,effective_date,created_by,created_at) VALUES(?,?,?,?,?,?)",
                (source_id, target_id, amount, effective.isoformat(), actor, utcnow()),
            )
            self._refresh_open_conflicts(conn, actor)
            self._audit(conn, actor, "transfer.created", "transfer", cur.lastrowid,
                        {"source": source_id, "target": target_id, "amount": amount, "effective_date": effective.isoformat()})
            return dict(conn.execute("SELECT * FROM transfers WHERE id=?", (cur.lastrowid,)).fetchone())

    def approve_transfer(self, transfer_id: int, actor: str, role: str = "reviewer") -> dict[str, Any]:
        if role != "reviewer":
            raise DomainError("只有审核人可以批准转让", 403)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            transfer = conn.execute("SELECT * FROM transfers WHERE id=?", (transfer_id,)).fetchone()
            if not transfer:
                raise DomainError("转让记录不存在", 404)
            if transfer["status"] != "pending":
                raise DomainError("该转让已处理，不能重复批准", 409)
            if actor == transfer["created_by"]:
                raise DomainError("发起人不能批准自己的转让", 403)
            source = self._account_row(conn, transfer["from_account_id"])
            target = self._account_row(conn, transfer["to_account_id"])
            amount = float(transfer["amount"])
            effective = parse_date(transfer["effective_date"], "生效日期")
            available = self._headroom(conn, source, effective, exclude_transfer_id=transfer_id)
            if amount > available + EPS:
                raise DomainError("审批时额度已被生态预留或其他记录占用（冲突见待处理区），不能批准", 409)
            impact = conn.execute(
                "SELECT * FROM impact_rules WHERE source_region=? AND target_region=?",
                (source["region"], target["region"]),
            ).fetchone()
            if impact:
                minimum = float(source["quota"]) * float(impact["min_source_fraction"])
                if available - amount + EPS < minimum:
                    raise DomainError("审批时下游最小留存约束不再满足", 409)
            # 批准金额在许可额度间划转；生态预留不划转额度，只在窗口上扣减。
            conn.execute("UPDATE accounts SET quota=quota-? WHERE id=?", (amount, source["id"]))
            conn.execute("UPDATE accounts SET quota=quota+? WHERE id=?", (amount, target["id"]))
            conn.execute("UPDATE transfers SET status='approved',approved_by=?,approved_at=? WHERE id=?", (actor, utcnow(), transfer_id))
            self._refresh_open_conflicts(conn, actor)
            self._audit(conn, actor, "transfer.approved", "transfer", transfer_id, {"amount": amount})
            row = conn.execute("SELECT * FROM transfers WHERE id=?", (transfer_id,)).fetchone()
        return dict(row)

    def reject_transfer(self, transfer_id: int, actor: str, role: str = "reviewer") -> dict[str, Any]:
        if role != "reviewer":
            raise DomainError("只有审核人可以退回转让", 403)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM transfers WHERE id=?", (transfer_id,)).fetchone()
            if not row or row["status"] != "pending":
                raise DomainError("转让不存在或已经处理", 409)
            if actor == row["created_by"]:
                raise DomainError("发起人不能自行退回", 403)
            conn.execute("UPDATE transfers SET status='rejected',approved_by=?,approved_at=? WHERE id=?", (actor, utcnow(), transfer_id))
            self._refresh_open_conflicts(conn, actor)
            self._audit(conn, actor, "transfer.rejected", "transfer", transfer_id, {})
        return {"id": transfer_id, "status": "rejected"}

    # -- 取水 -------------------------------------------------------------

    def record_usage(self, actor: str, payload: dict[str, Any], role: str = "meter") -> dict[str, Any]:
        if role not in {"meter", "editor"}:
            raise DomainError("只有计量员可以登记取水", 403)
        try:
            account_id = int(payload.get("account_id"))
            amount = float(payload.get("amount"))
        except (TypeError, ValueError) as exc:
            raise DomainError("账户和取水量必须是数值") from exc
        meter_event_id = str(payload.get("meter_event_id", "")).strip()
        occurred = parse_date(str(payload.get("occurred_at", "")), "计量日期")
        if amount <= 0 or not meter_event_id:
            raise DomainError("取水量必须大于 0，计量事件编号不能为空")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            account = self._account_row(conn, account_id)
            if not (account["valid_from"] <= occurred.isoformat() <= account["valid_to"]):
                raise DomainError("取水日期不在许可有效期内", 409)
            available = self._headroom(conn, account, occurred)
            if amount > available + EPS:
                raise DomainError("取水超过扣减生态预留后的可用额度", 409)
            season = conn.execute("SELECT max_fraction FROM season_rules WHERE region=? AND month=?", (account["region"], occurred.month)).fetchone()
            month_total = conn.execute(
                "SELECT COALESCE(SUM(amount),0) total FROM usage_records WHERE account_id=? AND substr(occurred_at,1,7)=?",
                (account_id, occurred.strftime("%Y-%m")),
            ).fetchone()["total"]
            if season:
                locked = self._locked(conn, account_id, occurred, occurred)
                cap = max(0.0, float(account["quota"]) - locked) * float(season["max_fraction"])
                if float(month_total) + amount > cap + EPS:
                    raise DomainError("本次取水超过该月份扣减预留后的季节配额", 409)
            try:
                cur = conn.execute(
                    "INSERT INTO usage_records(account_id,meter_event_id,amount,occurred_at,actor,created_at) VALUES(?,?,?,?,?,?)",
                    (account_id, meter_event_id, amount, occurred.isoformat(), actor, utcnow()),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("计量事件已登记，不能重复计水", 409) from exc
            conn.execute("UPDATE accounts SET used=used+? WHERE id=?", (amount, account_id))
            self._refresh_open_conflicts(conn, actor)
            self._audit(conn, actor, "usage.recorded", "account", account_id,
                        {"amount": amount, "occurred_at": occurred.isoformat(), "meter_event_id": meter_event_id})
            row = conn.execute("SELECT * FROM usage_records WHERE id=?", (cur.lastrowid,)).fetchone()
        return dict(row)

    # -- 查询与干旱 -------------------------------------------------------

    def available(self, account_id: int, as_of: str | None = None) -> dict[str, Any]:
        day = parse_date(as_of, "查询日期") if as_of else date.today()
        with self.connect() as conn:
            account = self._account_row(conn, account_id)
            return self._availability(conn, account, day)

    def simulate_drought(self, total_supply: float, reduction: float = 0.0,
                         as_of: str | None = None, role: str = "viewer") -> dict[str, Any]:
        try:
            total_supply, reduction = float(total_supply), float(reduction)
        except (TypeError, ValueError) as exc:
            raise DomainError("供水量和削减比例必须是数值") from exc
        if total_supply < 0 or not 0 <= reduction < 1:
            raise DomainError("供水量不能为负，削减比例应在 0 到 1 之间")
        day = parse_date(as_of, "模拟日期") if as_of else date.today()
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM accounts ORDER BY priority,name").fetchall()
            accounts = []
            for r in rows:
                info = self._availability(conn, r, day)
                accounts.append((r, info))
        supply = total_supply * (1 - reduction)
        allocation: dict[int, float] = {}
        deficit: dict[int, float] = {}
        remaining = supply
        for priority in range(1, 6):
            group = [item for item in accounts if int(item[0]["priority"]) == priority]
            if not group:
                continue
            # 枯水时高优先级先取得扣减生态预留后的剩余额度，再供给低优先级。
            requested = sum(max(0.0, item[1]["effective_quota"] - float(item[0]["used"])) for item in group)
            take = min(remaining, requested)
            if requested <= 0:
                continue
            for row, info in group:
                quota_left = max(0.0, info["effective_quota"] - float(row["used"]))
                share = take * quota_left / requested
                allocation[int(row["id"])] = share
                deficit[int(row["id"])] = quota_left - share
            remaining -= take
            if remaining <= 1e-9:
                for row, info in accounts:
                    if int(row["priority"]) > priority:
                        left = max(0.0, info["effective_quota"] - float(row["used"]))
                        allocation[int(row["id"])] = 0.0
                        deficit[int(row["id"])] = left
                break
        return {"total_supply": total_supply, "reduction": reduction, "as_of": day.isoformat(),
                "effective_supply": supply, "unallocated": remaining, "allocations": [
                    {"account_id": int(r["id"]), "name": r["name"], "priority": r["priority"],
                     "ecological_reserved": info["ecological_reserved"],
                     "effective_quota": info["effective_quota"],
                     "allocation": allocation.get(int(r["id"]), 0.0),
                     "deficit": deficit.get(int(r["id"]), 0.0)}
                    for r, info in accounts]}

    def list_accounts(self, as_of: str | None = None) -> list[dict[str, Any]]:
        day = parse_date(as_of, "查询日期") if as_of else date.today()
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM accounts ORDER BY id").fetchall()
            return [self._availability(conn, row, day) | dict(row) for row in rows]

    def list_transfers(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM transfers ORDER BY id DESC").fetchall()
        return [dict(row) for row in rows]

    def audit(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM audit_log ORDER BY id DESC").fetchall()
        return [dict(row) for row in rows]
