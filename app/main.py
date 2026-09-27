"""GS1 召回扫码核验 API。"""

from __future__ import annotations

from fastapi import FastAPI

from .custody import audit_custody
from .gs1 import ScanParseError, parse_scan
from .matcher import match_rules
from .models import (
    CheckRequest,
    CheckResponse,
    CustodyAuditRequest,
    CustodyAuditResponse,
    ScanErrorBody,
    ScanFields,
    ScanResult,
)

app = FastAPI(
    title="GS1 召回扫码核验 / 实物交接审计 API",
    version="1.0.0",
    summary="解析 GS1 扫描串(AI 01/10/17/21):召回规则匹配与实物持有人链审计",
)


@app.get("/healthz", tags=["meta"])
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/recall/check", response_model=CheckResponse, tags=["recall"])
def recall_check(request: CheckRequest) -> CheckResponse:
    """逐条解析扫描串并匹配召回规则;单条解析失败不影响其余条目。"""
    results: list[ScanResult] = []
    for index, raw in enumerate(request.scans):
        try:
            parsed = parse_scan(raw)
        except ScanParseError as exc:
            results.append(
                ScanResult(
                    index=index,
                    ok=False,
                    error=ScanErrorBody(offset=exc.offset, reason=exc.reason),
                )
            )
            continue
        results.append(
            ScanResult(
                index=index,
                ok=True,
                fields=ScanFields(
                    gtin=parsed.gtin,
                    batch=parsed.batch,
                    serial=parsed.serial,
                    date=parsed.expiry,
                ),
                matches=match_rules(parsed, request.rules),
            )
        )
    return CheckResponse(results=results)


@app.post("/v1/custody/audit", response_model=CustodyAuditResponse, tags=["custody"])
def custody_audit(request: CustodyAuditRequest) -> CustodyAuditResponse:
    """审计 1~300 条实物交接事件,回放持有人链并校验批号 / 有效期一致性。"""
    return audit_custody(request)
