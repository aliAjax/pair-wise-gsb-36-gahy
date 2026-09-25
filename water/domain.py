"""判定层：日期区间、生态预留扣减、冲突与解除的纯逻辑，不访问数据库。"""
from __future__ import annotations

from datetime import date
from typing import Any, Iterable, Sequence

EPS = 1e-9


class DomainError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def parse_date(value: str, field: str = "日期") -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise DomainError(f"{field}必须是 YYYY-MM-DD") from exc


def overlap(start_a: date, end_a: date, start_b: date, end_b: date) -> bool:
    """闭区间是否交叠；首尾相接（a 结束次日 b 开始）不算交叠。"""
    return start_a <= end_b and start_b <= end_a


def locked_for(reservation: Any, start: date, end: date) -> bool:
    """该预留记录是否在给定窗口内锁定额度。

    active 在整个登记区间锁定；released 只锁定到实际解除日（含），
    解除日之后的部分已按解除时的核算恢复。
    """
    r_start = date.fromisoformat(reservation["start_date"])
    if reservation["status"] == "active":
        r_end = date.fromisoformat(reservation["end_date"])
    elif reservation["status"] == "released":
        r_end = date.fromisoformat(reservation["released_at"][:10])
    else:
        return False
    return overlap(r_start, r_end, start, end)


def locked_amount(reservations: Iterable[Any], account_id: int, start: date, end: date) -> float:
    """窗口内生效预留的锁定量。同日多段交叠时保守取最大金额，避免重复扣减。"""
    hits = [float(r["amount"]) for r in reservations
            if int(r["account_id"]) == account_id and locked_for(r, start, end)]
    return max(hits, default=0.0)


def effective_quota(quota: float, reservations: Iterable[Any], account_id: int,
                    start: date, end: date | None = None) -> float:
    """扣减窗口内生态预留后的许可额度（许可本身不划转）。"""
    if end is None:
        end = start
    locked = locked_amount(reservations, account_id, start, end)
    return max(0.0, float(quota) - locked)


def squeezed_pending(window_transfers: Sequence[Any], *, quota: float, used: float,
                     locked: float, other_pending: float) -> list[dict[str, Any]]:
    """筛选会被预留挤到的待审转让（调用方已按窗口过滤、按生效日排序）。

    预留生效后的统一可用额度里，窗口内待审转让按生效日先后先到先占；放不下
    的记录计入冲突，并给出各自缺口。窗口外的待审转让通过 other_pending 预占。
    """
    room = float(quota) - float(locked) - float(used) - float(other_pending)
    result: list[dict[str, Any]] = []
    for t in window_transfers:
        amount = float(t["amount"])
        if amount > room + EPS:
            result.append({"transfer": t, "shortage": max(0.0, amount - room)})
        else:
            room -= amount
    return result


def restorable_amount(amount: float, occupied_after_release: float) -> float:
    """提前解除时只恢复未被后续操作占用的部分，其余跟着原记录继续走。"""
    return max(0.0, float(amount) - float(occupied_after_release))
