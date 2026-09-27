"""请求 / 响应模型。"""

from __future__ import annotations

from datetime import date as dt_date
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .gs1 import VARIABLE_MAX, gtin_check_ok


def _is_printable(s: str) -> bool:
    return all(" " <= c <= "~" for c in s)


class Rule(BaseModel):
    """召回规则:GTIN 必填且须通过模 10 校验;批号 / 序列号 / ISO 日期闭区间可选。

    规则中附带的字段,扫描串里必须存在且相等才算命中。
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    gtin: str
    batch: str | None = None
    serial: str | None = None
    date_from: dt_date | None = None
    date_to: dt_date | None = None

    @field_validator("gtin")
    @classmethod
    def _gtin_valid(cls, v: str) -> str:
        if len(v) != 14 or any(not "0" <= c <= "9" for c in v):
            raise ValueError("GTIN 须为 14 位数字")
        if not gtin_check_ok(v):
            raise ValueError("GTIN 校验位错误 (模 10)")
        return v

    @field_validator("batch", "serial")
    @classmethod
    def _variable_valid(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if not 1 <= len(v) <= VARIABLE_MAX or not _is_printable(v):
            raise ValueError(f"须为 1~{VARIABLE_MAX} 位可打印 ASCII")
        return v

    @model_validator(mode="after")
    def _date_range_valid(self) -> "Rule":
        if (
            self.date_from is not None
            and self.date_to is not None
            and self.date_from > self.date_to
        ):
            raise ValueError("date_from 不得晚于 date_to")
        return self


class CheckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scans: list[str] = Field(min_length=1, max_length=100)
    rules: list[Rule] = Field(min_length=1, max_length=100)

    @field_validator("rules")
    @classmethod
    def _rule_ids_unique(cls, v: list[Rule]) -> list[Rule]:
        ids = [r.id for r in v]
        if len(set(ids)) != len(ids):
            raise ValueError("规则 ID 必须唯一")
        return v


class ScanFields(BaseModel):
    gtin: str
    batch: str | None = None
    serial: str | None = None
    date: dt_date | None = None  # AI 17 解析结果,ISO 格式


class ScanErrorBody(BaseModel):
    offset: int  # 相对原始扫描串(含 ]d2 前缀),从 0 计
    reason: str


class ScanResult(BaseModel):
    index: int
    ok: bool
    fields: ScanFields | None = None
    matches: list[str] = Field(default_factory=list)
    error: ScanErrorBody | None = None


class CheckResponse(BaseModel):
    results: list[ScanResult]


# ---------- POST /v1/custody/audit ----------


class CustodyEvent(BaseModel):
    """一条实物交接事件。

    scan 为 GS1 扫描串(须含 AI 01/21);occurred_at 须带时区;
    from_location 首次交接须为 null,其后须等于上一成功事件的 to_location;
    to_location 为非空的接收位置。
    """

    model_config = ConfigDict(extra="forbid")

    scan: str = Field(min_length=1, max_length=512)
    occurred_at: datetime
    from_location: str | None = Field(default=None, max_length=128)
    to_location: str = Field(min_length=1, max_length=128)

    @field_validator("from_location")
    @classmethod
    def _from_location_nonempty(cls, v: str | None) -> str | None:
        if v is not None and not v:
            raise ValueError("from_location 可为 null 或非空字符串,不能为空字符串")
        return v

    @field_validator("occurred_at")
    @classmethod
    def _occurred_at_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None or v.utcoffset() is None:
            raise ValueError("occurred_at 必须带时区信息,如 2026-09-26T08:00:00+08:00")
        return v


class CustodyAuditRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    events: list[CustodyEvent] = Field(min_length=1, max_length=300)


class CustodyItem(BaseModel):
    """单件实物(GTIN + 序列号)的成功流转结果。"""

    gtin: str
    serial: str
    event_indices: list[int] = Field(description="成功事件的原始下标,按处理顺序(UTC 升序)")
    holder: str = Field(description="最终持有人,即最后一次成功事件的 to_location")


class CustodyFailure(BaseModel):
    """按原始下标记录的失败事件;offset 仅在扫描串解析失败时存在。"""

    index: int
    reason: str
    offset: int | None = None


class CustodyAuditResponse(BaseModel):
    items: list[CustodyItem]  # 按 (GTIN, 序列号) 排序
    failures: list[CustodyFailure]  # 按原始下标排序
