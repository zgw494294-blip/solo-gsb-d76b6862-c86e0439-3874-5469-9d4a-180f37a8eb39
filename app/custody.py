"""实物交接审计:按时间回放交接事件,校验持有人链与批号 / 有效期一致性。

处理规则:
- 复用 gs1.parse_scan 解析扫描串,以 (GTIN, AI 21 序列号) 识别单件实物;
  扫描非法或缺少序列号的事件按原下标记入 failures,不参与流转。
- 参与流转的事件统一转 UTC,按 (时间, 原始下标) 升序处理。
- 每件实物的首次成功事件 from_location 须为 null,并以该次扫描的
  批号 (AI 10) / 有效期 (AI 17) 为基准(含字段缺失)。
- 后续事件 from_location 须等于上一成功事件的 to_location,批号 / 有效期
  须与首次成功扫描一致;任一不满足即按原下标记录冲突,且不改变持有人。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from .gs1 import ParsedScan, ScanParseError, parse_scan
from .models import (
    CustodyAuditRequest,
    CustodyAuditResponse,
    CustodyEvent,
    CustodyFailure,
    CustodyItem,
)


@dataclass
class _ItemState:
    """单件实物在回放过程中的状态(由首次成功扫描建立)。"""

    batch: str | None
    expiry: date | None
    holder: str
    event_indices: list[int] = field(default_factory=list)


def _batch_conflict(baseline: str | None, actual: str | None) -> str:
    if baseline is None and actual is not None:
        return f"批号冲突: 首次成功扫描缺少批号 (AI 10),本事件批号为 {actual!r}"
    if baseline is not None and actual is None:
        return f"批号冲突: 首次成功扫描批号为 {baseline!r},本事件缺少批号 (AI 10)"
    return f"批号冲突: 与首次成功扫描不一致,期望 {baseline!r},实际 {actual!r}"


def _expiry_conflict(baseline: date | None, actual: date | None) -> str:
    if baseline is None and actual is not None:
        return f"有效期冲突: 首次成功扫描缺少有效期 (AI 17),本事件有效期为 {actual.isoformat()}"
    if baseline is not None and actual is None:
        return f"有效期冲突: 首次成功扫描有效期为 {baseline.isoformat()},本事件缺少有效期 (AI 17)"
    return (
        "有效期冲突: 与首次成功扫描不一致,"
        f"期望 {baseline.isoformat()},实际 {actual.isoformat()}"
    )


def _location_mismatch(expected: str, actual: str | None) -> str:
    actual_repr = "null" if actual is None else repr(actual)
    return (
        "持有人不一致: from_location 须等于上一成功事件的 to_location "
        f"{expected!r},实际为 {actual_repr}"
    )


def audit_custody(request: CustodyAuditRequest) -> CustodyAuditResponse:
    """审计一批实物交接事件,返回每件实物的流转结果与失败事件原因。"""
    failures: list[CustodyFailure] = []
    prepared: list[tuple[datetime, int, ParsedScan, CustodyEvent]] = []

    # 第一遍:解析与身份识别,非法 / 缺少序列号的事件在此剔除
    for index, event in enumerate(request.events):
        try:
            parsed = parse_scan(event.scan)
        except ScanParseError as exc:
            failures.append(
                CustodyFailure(index=index, offset=exc.offset, reason=exc.reason)
            )
            continue
        if parsed.serial is None:
            failures.append(
                CustodyFailure(index=index, reason="缺少 AI 21 序列号,无法识别实物")
            )
            continue
        prepared.append((event.occurred_at.astimezone(timezone.utc), index, parsed, event))

    # UTC 升序;同一时刻按原始下标(即输入顺序)
    prepared.sort(key=lambda item: (item[0], item[1]))

    # 第二遍:按时间回放,逐件校验持有人链与批号 / 有效期一致性
    states: dict[tuple[str, str], _ItemState] = {}
    for _, index, parsed, event in prepared:
        key = (parsed.gtin, parsed.serial)
        state = states.get(key)

        if state is None:
            # 该实物尚无成功事件:首次交接必须从 null 出发
            if event.from_location is not None:
                failures.append(
                    CustodyFailure(
                        index=index,
                        reason=(
                            "实物首次交接事件 from_location 须为 null,"
                            f"实际为 {event.from_location!r}"
                        ),
                    )
                )
                continue
            states[key] = _ItemState(
                batch=parsed.batch,
                expiry=parsed.expiry,
                holder=event.to_location,
                event_indices=[index],
            )
            continue

        if event.from_location != state.holder:
            failures.append(
                CustodyFailure(
                    index=index,
                    reason=_location_mismatch(state.holder, event.from_location),
                )
            )
            continue
        if parsed.batch != state.batch:
            failures.append(
                CustodyFailure(index=index, reason=_batch_conflict(state.batch, parsed.batch))
            )
            continue
        if parsed.expiry != state.expiry:
            failures.append(
                CustodyFailure(
                    index=index, reason=_expiry_conflict(state.expiry, parsed.expiry)
                )
            )
            continue

        # 校验通过:记录成功事件并更新持有人
        state.event_indices.append(index)
        state.holder = event.to_location

    items = [
        CustodyItem(
            gtin=gtin,
            serial=serial,
            event_indices=state.event_indices,
            holder=state.holder,
        )
        for (gtin, serial), state in sorted(states.items(), key=lambda kv: kv[0])
    ]
    failures.sort(key=lambda item: item.index)
    return CustodyAuditResponse(items=items, failures=failures)
