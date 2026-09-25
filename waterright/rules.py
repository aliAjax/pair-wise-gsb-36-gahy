"""判定层：额度、日期区间、挤占关系和干旱分配的纯计算。

输入都是普通字典/日期，不读写数据库，方便单独测试。
约定金额比较的容差 EPS。
"""
from __future__ import annotations

from datetime import date
from typing import Any

EPS = 1e-9


def intervals_overlap(a_start: date, a_end: date, b_start: date, b_end: date) -> bool:
    """同一账户的预留区间若有任何一天重合即视为交叠。"""
    return a_start <= b_end and b_start <= a_end


def hold_on(reservations: list[dict[str, Any]], on_date: date) -> float:
    """某日仍在生效（未提前解除，或解除日当天仍占用）的预留合计。"""
    total = 0.0
    for res in reservations:
        start = res["_start"]
        end = res["_released"] if res["_released"] is not None else res["_end"]
        if start <= on_date <= end:
            total += float(res["amount"]) - float(res["occupied_amount"])
    return total


def availability(account: dict[str, Any], ecology_hold: float, pending_outgoing: float) -> dict[str, float]:
    """扣减实际用量、生态预留和待审转让预占后的可用额度分解。"""
    quota = float(account["quota"])
    used = float(account["used"])
    effective_quota = quota - ecology_hold
    raw = quota - used - pending_outgoing
    value = max(0.0, quota - used - ecology_hold - pending_outgoing)
    return {
        "quota": quota,
        "used": used,
        "ecology_reserved": ecology_hold,
        "reserved_outgoing": pending_outgoing,
        "effective_quota": effective_quota,
        "available": value,
        "before_hold_available": max(0.0, raw),
    }


def squeezed_transfers(
    pending: list[dict[str, Any]],
    reservation_start: date,
    reservation_end: date,
    hold_amount: float,
    capacity: float,
) -> list[dict[str, Any]]:
    """判定哪些待审转让会被本次预留挤到。

    只考虑生效日落入预留区间的待审转出；区间外待审预占已从 capacity 扣减。
    按提交先后排队，普通余量（capacity - 本次预留）被前面的转让吃光后，
    后面的转让动用预留；shortfall 记“到该转让为止累计需要预留的量”，
    先批走前面的会吃掉预留。
    """
    in_window = [
        t for t in pending
        if reservation_start <= t["_effective"] <= reservation_end
    ]
    in_window.sort(key=lambda t: (t["created_at"], t["id"]))
    free_room = max(0.0, capacity - hold_amount)
    total_pending = 0.0
    result: list[dict[str, Any]] = []
    for t in in_window:
        amount = float(t["amount"])
        total_pending += amount
        cumulative_need = max(0.0, total_pending - free_room)
        if cumulative_need > EPS:
            result.append({
                "transfer_id": t["id"],
                "from_account_id": t["from_account_id"],
                "to_account_id": t["to_account_id"],
                "amount": amount,
                "effective_date": t["effective_date"],
                "shortfall": round(cumulative_need, 6),
                "created_by": t["created_by"],
            })
    return result


def transfer_blocked(avail: dict[str, float], amount: float) -> bool:
    return amount > avail["available"] + EPS


def seasonal_cap_exceeded(quota: float, max_fraction: float, month_used: float, amount: float) -> bool:
    cap = float(quota) * float(max_fraction)
    return float(month_used) + amount > cap + EPS


def minimum_left_violated(remaining_base: float, min_source_fraction: float, remaining_after: float) -> bool:
    """生态预留生效后，留存基数是扣减预留后的额度；未预留时即原许可额度。"""
    minimum = float(remaining_base) * float(min_source_fraction)
    return remaining_after + EPS < minimum


def releasable_amount(reservation: dict[str, Any], on_date: date) -> float:
    """提前解除时只能恢复未被后续操作占用的部分。"""
    if reservation["_start"] > on_date:
        # 解除日早于开始日：整条预留都还没起作用，全部可恢复。
        return float(reservation["amount"]) - float(reservation["occupied_amount"])
    held = float(reservation["amount"]) - float(reservation["occupied_amount"])
    if reservation["_released"] is not None or not (reservation["_start"] <= on_date <= reservation["_end"]):
        return 0.0
    return held


def drought_plan(rows: list[dict[str, Any]], total_supply: float, reduction: float) -> dict[str, Any]:
    """按扣减生态预留后的剩余额度做高优先级先行分配，同级按剩余额度比例。"""
    supply = total_supply * (1 - reduction)
    allocation: dict[int, float] = {}
    deficit: dict[int, float] = {}
    remaining = supply
    for priority in range(1, 6):
        group = [r for r in rows if int(r["priority"]) == priority]
        if not group:
            continue
        requested = sum(max(0.0, float(r["quota"]) - float(r["used"])) for r in group)
        if requested <= 0:
            continue
        take = min(remaining, requested)
        for row in group:
            quota_left = max(0.0, float(row["quota"]) - float(row["used"]))
            share = take * quota_left / requested
            allocation[int(row["id"])] = share
            deficit[int(row["id"])] = quota_left - share
        remaining -= take
        if remaining <= EPS:
            for lower in rows:
                if int(lower["priority"]) > priority:
                    left = max(0.0, float(lower["quota"]) - float(lower["used"]))
                    allocation.setdefault(int(lower["id"]), 0.0)
                    deficit.setdefault(int(lower["id"]), left)
            break
    return {"remaining": remaining, "allocation": allocation, "deficit": deficit}
