"""资料层：登记资料的读取、格式校验与示例资料。

这一层只处理“登记了什么”，不判断额度够不够，也不碰数据库。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any


class DomainError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def parse_date(value: str, field: str = "日期") -> date:
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise DomainError(f"{field}必须是 YYYY-MM-DD") from exc


def _text(payload: dict[str, Any], key: str, label: str, required: bool = True) -> str:
    value = str(payload.get(key, "") if payload.get(key) is not None else "").strip()
    if required and not value:
        raise DomainError(f"{label}不能为空")
    return value


def _number(payload: dict[str, Any], key: str, label: str) -> float:
    try:
        return float(payload.get(key))
    except (TypeError, ValueError) as exc:
        raise DomainError(f"{label}必须是数值") from exc


@dataclass(frozen=True)
class AccountMaterial:
    name: str
    region: str
    holder: str
    priority: int
    quota: float
    valid_from: date
    valid_to: date


def read_account(payload: dict[str, Any]) -> AccountMaterial:
    name = _text(payload, "name", "账户名称")
    region = _text(payload, "region", "地区")
    holder = _text(payload, "holder", "持有人")
    try:
        priority = int(payload.get("priority"))
    except (TypeError, ValueError) as exc:
        raise DomainError("优先级必须是 1 到 5 的整数") from exc
    quota = _number(payload, "quota", "额度")
    if not 1 <= priority <= 5:
        raise DomainError("优先级应在 1 到 5 之间")
    if quota < 0:
        raise DomainError("额度不能为负")
    valid_from = parse_date(payload.get("valid_from", ""), "生效日期")
    valid_to = parse_date(payload.get("valid_to", ""), "失效日期")
    if valid_from > valid_to:
        raise DomainError("生效日期不能晚于失效日期")
    return AccountMaterial(name, region, holder, priority, quota, valid_from, valid_to)


@dataclass(frozen=True)
class TransferMaterial:
    from_account_id: int
    to_account_id: int
    amount: float
    effective_date: date


def read_transfer(payload: dict[str, Any]) -> TransferMaterial:
    try:
        source_id = int(payload.get("from_account_id"))
        target_id = int(payload.get("to_account_id"))
    except (TypeError, ValueError) as exc:
        raise DomainError("账户编号必须是整数") from exc
    amount = _number(payload, "amount", "转让量")
    if source_id == target_id:
        raise DomainError("转让账户不能相同")
    if amount <= 0:
        raise DomainError("转让量必须大于 0")
    effective = parse_date(payload.get("effective_date", ""), "生效日期")
    return TransferMaterial(source_id, target_id, amount, effective)


@dataclass(frozen=True)
class UsageMaterial:
    account_id: int
    amount: float
    meter_event_id: str
    occurred_at: date


def read_usage(payload: dict[str, Any]) -> UsageMaterial:
    try:
        account_id = int(payload.get("account_id"))
    except (TypeError, ValueError) as exc:
        raise DomainError("账户编号必须是整数") from exc
    amount = _number(payload, "amount", "取水量")
    meter_event_id = _text(payload, "meter_event_id", "计量事件编号")
    occurred = parse_date(payload.get("occurred_at", ""), "计量日期")
    if amount <= 0:
        raise DomainError("取水量必须大于 0")
    return UsageMaterial(account_id, amount, meter_event_id, occurred)


@dataclass(frozen=True)
class ReservationMaterial:
    """生态预留登记资料：账户、起止日期、预留水量、依据和经办人。"""

    account_id: int
    amount: float
    valid_from: date
    valid_to: date
    basis: str
    operator: str


def read_reservation(payload: dict[str, Any], actor: str) -> ReservationMaterial:
    try:
        account_id = int(payload.get("account_id"))
    except (TypeError, ValueError) as exc:
        raise DomainError("账户编号必须是整数") from exc
    amount = _number(payload, "amount", "预留水量")
    if amount <= 0:
        raise DomainError("预留水量必须大于 0")
    valid_from = parse_date(payload.get("valid_from", ""), "预留开始日期")
    valid_to = parse_date(payload.get("valid_to", ""), "预留结束日期")
    if valid_from > valid_to:
        raise DomainError("预留开始日期不能晚于结束日期")
    basis = _text(payload, "basis", "预留依据（调度文件/政策）")
    operator = _text(payload, "operator", "经办人", required=False) or actor
    return ReservationMaterial(account_id, amount, valid_from, valid_to, basis, operator)


@dataclass(frozen=True)
class ReleaseMaterial:
    reservation_id: int
    on_date: date
    operator: str
    reason: str


def read_release(payload: dict[str, Any], actor: str) -> ReleaseMaterial:
    try:
        reservation_id = int(payload.get("reservation_id"))
    except (TypeError, ValueError) as exc:
        raise DomainError("预留记录编号必须是整数") from exc
    on_date = parse_date(payload.get("release_date") or date.today().isoformat(), "解除日期")
    operator = _text(payload, "operator", "经办人", required=False) or actor
    reason = _text(payload, "reason", "解除原因")
    return ReleaseMaterial(reservation_id, on_date, operator, reason)


def parse_optional_date(value: str | None, field: str) -> date | None:
    if value is None or str(value).strip() == "":
        return None
    return parse_date(value, field)


# 初始化示例资料（与原原型保持一致，另加一条夏季生态预留）。
DEMO_ACCOUNTS = [
    {"name": "北区水库", "region": "upstream", "holder": "北区水务公司", "priority": 1,
     "valid_from": "2026-01-01", "valid_to": "2026-12-31", "quota": 1000},
    {"name": "河口灌区", "region": "downstream", "holder": "河口合作社", "priority": 2,
     "valid_from": "2026-01-01", "valid_to": "2026-12-31", "quota": 500},
]
DEMO_SEASON = {"region": "upstream", "month": 7, "max_fraction": 0.35, "note": "夏季上限"}
DEMO_IMPACT = {"source_region": "upstream", "target_region": "downstream",
               "min_source_fraction": 0.4, "note": "保障河口最小生态流量"}
DEMO_USAGE = {"account": "北区水库", "amount": 100, "meter_event_id": "UP-2026-0001", "occurred_at": "2026-03-01"}
DEMO_RESERVATION = {"account": "北区水库", "amount": 150,
                    "valid_from": "2026-07-01", "valid_to": "2026-08-31",
                    "basis": "2026年夏季河道生态流量调度预案", "operator": "eco-dispatcher"}
