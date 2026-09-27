"""POST /v1/custody/audit 的端到端测试。"""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

FNC1 = "\x1d"  # ASCII 29 (GS)


def make_gtin(body13: str) -> str:
    """由 13 位本体计算模 10 校验位,生成合法 14 位 GTIN。"""
    assert len(body13) == 13 and body13.isdigit()
    total = sum(int(c) * (3 if i % 2 == 0 else 1) for i, c in enumerate(reversed(body13)))
    return body13 + str((10 - total % 10) % 10)


GTIN = make_gtin("0950600112345")
GTIN2 = make_gtin("0950600112346")


def scan(gtin=GTIN, serial=None, batch=None, expiry=None):
    """按 AI 01/17/10/21 构造扫描串(变长字段后按需插入 FNC1)。"""
    s = "01" + gtin
    if expiry is not None:
        s += "17" + expiry
    if batch is not None:
        s += "10" + batch
        if serial is not None:
            s += FNC1
    if serial is not None:
        s += "21" + serial
    return s


def ev(scan_str, occurred_at, to_location, from_location=None):
    return {
        "scan": scan_str,
        "occurred_at": occurred_at,
        "from_location": from_location,
        "to_location": to_location,
    }


def audit(events):
    resp = client.post("/v1/custody/audit", json={"events": events})
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---------- 正常流转 ----------

def test_happy_chain_holder_updates():
    s = scan(serial="SN9", batch="LOT1", expiry="251231")
    events = [
        ev(s, "2026-01-01T08:00:00+08:00", "WAREHOUSE-A", None),
        ev(s, "2026-01-02T08:00:00+08:00", "STORE-B", "WAREHOUSE-A"),
        ev(s, "2026-01-03T08:00:00+08:00", "CUSTOMER-C", "STORE-B"),
    ]
    data = audit(events)
    assert data["failures"] == []
    assert data["items"] == [
        {
            "gtin": GTIN,
            "serial": "SN9",
            "event_indices": [0, 1, 2],
            "holder": "CUSTOMER-C",
        }
    ]


def test_events_processed_in_utc_order_not_input_order():
    # 输入顺序乱序,且混用不同时区;成功下标须按 UTC 时间先后排列
    s = scan(serial="SN9", batch="LOT1", expiry="251231")
    events = [
        ev(s, "2026-01-03T00:00:00+00:00", "STORE-B", "WAREHOUSE-A"),   # 1
        ev(s, "2026-01-01T08:00:00+08:00", "WAREHOUSE-A", None),        # 0
        ev(s, "2026-01-03T10:00:00+08:00", "CUSTOMER-C", "STORE-B"),    # 2
    ]
    data = audit(events)
    assert data["failures"] == []
    assert data["items"][0]["event_indices"] == [1, 0, 2]
    assert data["items"][0]["holder"] == "CUSTOMER-C"


def test_same_instant_follows_input_order():
    # 同一 UTC 时刻:输入下标 0 的事件先处理,下标 1 的事件后处理
    s = scan(serial="SN9", batch="LOT1", expiry="251231")
    events = [
        ev(s, "2026-01-01T00:00:00+00:00", "WAREHOUSE-A", None),
        ev(s, "2026-01-01T08:00:00+08:00", "STORE-B", "WAREHOUSE-A"),
    ]
    data = audit(events)
    assert data["failures"] == []
    assert data["items"][0]["event_indices"] == [0, 1]


def test_same_instant_input_order_mismatch_is_failure():
    # 颠倒输入顺序后,下标 1(首次 null 交接)还未处理,下标 0 先失败
    s = scan(serial="SN9", batch="LOT1", expiry="251231")
    events = [
        ev(s, "2026-01-01T08:00:00+08:00", "STORE-B", "WAREHOUSE-A"),
        ev(s, "2026-01-01T00:00:00+00:00", "WAREHOUSE-A", None),
    ]
    data = audit(events)
    (failure,) = data["failures"]
    assert failure["index"] == 0
    assert "首次交接事件 from_location 须为 null" in failure["reason"]
    assert failure["offset"] is None
    # 下标 1 成为首次成功事件,持有人停在 WAREHOUSE-A
    assert data["items"][0]["event_indices"] == [1]
    assert data["items"][0]["holder"] == "WAREHOUSE-A"


def test_batch_and_expiry_absents_are_consistent():
    # 首次扫描与后续扫描都没有批号 / 有效期 → 一致(含字段缺失)
    s = scan(serial="SN9")
    events = [
        ev(s, "2026-01-01T08:00:00+08:00", "A", None),
        ev(s, "2026-01-02T08:00:00+08:00", "B", "A"),
    ]
    data = audit(events)
    assert data["failures"] == []
    assert data["items"][0]["holder"] == "B"


# ---------- 扫描非法 / 缺少序列号:按原下标失败,不参与流转 ----------

def test_invalid_scan_reports_offset_and_reason():
    good = scan(serial="SN9")
    bad = "01" + GTIN + "99" + "AB"
    events = [
        ev(good, "2026-01-01T08:00:00+08:00", "A", None),
        ev(bad, "2026-01-01T09:00:00+08:00", "B", "A"),
    ]
    data = audit(events)
    (failure,) = data["failures"]
    assert failure == {"index": 1, "reason": "未知 AI: '99'", "offset": 2 + 14}
    # 非法事件不改变持有人
    assert data["items"][0]["event_indices"] == [0]
    assert data["items"][0]["holder"] == "A"


def test_missing_serial_reports_original_index():
    events = [
        ev(scan(serial="SN9", batch="LOT1"), "2026-01-01T08:00:00+08:00", "A", None),
        ev(scan(batch="LOT1"), "2026-01-02T08:00:00+08:00", "B", "A"),  # 无 AI 21
    ]
    data = audit(events)
    (failure,) = data["failures"]
    assert failure["index"] == 1
    assert "AI 21" in failure["reason"]
    assert failure["offset"] is None
    assert data["items"][0]["event_indices"] == [0]


def test_bad_gtin_check_digit_is_parse_failure():
    bad_gtin = GTIN[:-1] + str((int(GTIN[-1]) + 1) % 10)
    events = [ev(scan(gtin=bad_gtin, serial="SN9"), "2026-01-01T08:00:00+08:00", "A", None)]
    data = audit(events)
    assert data["items"] == []
    (failure,) = data["failures"]
    assert failure["index"] == 0
    assert failure["offset"] == 2 + 13
    assert "校验位" in failure["reason"]


# ---------- 持有人链 ----------

def test_first_event_from_location_must_be_null():
    s = scan(serial="SN9")
    events = [
        ev(s, "2026-01-01T08:00:00+08:00", "A", "SOMEWHERE"),
        ev(s, "2026-01-02T08:00:00+08:00", "B", "A"),
    ]
    data = audit(events)
    assert data["items"] == []
    assert [f["index"] for f in data["failures"]] == [0, 1]
    assert "首次交接事件 from_location 须为 null" in data["failures"][0]["reason"]
    # 实物从未建立,下标 1 同样按首次交接处理
    assert "首次交接事件 from_location 须为 null" in data["failures"][1]["reason"]


def test_chain_mismatch_does_not_change_holder():
    s = scan(serial="SN9", batch="LOT1", expiry="251231")
    events = [
        ev(s, "2026-01-01T08:00:00+08:00", "A", None),
        ev(s, "2026-01-02T08:00:00+08:00", "X", "WRONG"),   # 持有人不符
        ev(s, "2026-01-03T08:00:00+08:00", "B", "A"),       # 持有人仍为 A,可继续
    ]
    data = audit(events)
    (failure,) = data["failures"]
    assert failure["index"] == 1
    assert "持有人不一致" in failure["reason"]
    assert "'A'" in failure["reason"]
    assert "'WRONG'" in failure["reason"]
    assert data["items"][0]["event_indices"] == [0, 2]
    assert data["items"][0]["holder"] == "B"


def test_later_from_null_is_chain_mismatch():
    s = scan(serial="SN9")
    events = [
        ev(s, "2026-01-01T08:00:00+08:00", "A", None),
        ev(s, "2026-01-02T08:00:00+08:00", "B", None),
    ]
    data = audit(events)
    (failure,) = data["failures"]
    assert failure["index"] == 1
    assert "持有人不一致" in failure["reason"]
    assert "null" in failure["reason"]
    assert data["items"][0]["holder"] == "A"


def test_identities_are_independent():
    # 两件实物互不干扰,各自独立成链
    s1, s2 = scan(serial="SN1"), scan(serial="SN2")
    events = [
        ev(s1, "2026-01-01T08:00:00+08:00", "A1", None),
        ev(s2, "2026-01-01T08:00:00+08:00", "A2", None),
        ev(s1, "2026-01-02T08:00:00+08:00", "B1", "A1"),
        ev(s2, "2026-01-02T08:00:00+08:00", "B2", "A2"),
    ]
    data = audit(events)
    assert data["failures"] == []
    assert [(i["serial"], i["holder"], i["event_indices"]) for i in data["items"]] == [
        ("SN1", "B1", [0, 2]),
        ("SN2", "B2", [1, 3]),
    ]


# ---------- 批号 / 有效期冲突 ----------

def test_batch_mismatch_conflict_does_not_change_holder():
    s1 = scan(serial="SN9", batch="LOT1", expiry="251231")
    s2 = scan(serial="SN9", batch="LOT2", expiry="251231")
    events = [
        ev(s1, "2026-01-01T08:00:00+08:00", "A", None),
        ev(s2, "2026-01-02T08:00:00+08:00", "B", "A"),  # 批号冲突
        ev(s1, "2026-01-03T08:00:00+08:00", "C", "A"),  # 持有人仍是 A
    ]
    data = audit(events)
    (failure,) = data["failures"]
    assert failure["index"] == 1
    assert "批号冲突" in failure["reason"]
    assert "'LOT1'" in failure["reason"] and "'LOT2'" in failure["reason"]
    assert data["items"][0]["event_indices"] == [0, 2]
    assert data["items"][0]["holder"] == "C"


def test_batch_present_then_missing_is_conflict():
    s1 = scan(serial="SN9", batch="LOT1")
    s2 = scan(serial="SN9")
    events = [
        ev(s1, "2026-01-01T08:00:00+08:00", "A", None),
        ev(s2, "2026-01-02T08:00:00+08:00", "B", "A"),
    ]
    data = audit(events)
    (failure,) = data["failures"]
    assert failure["index"] == 1
    assert "批号冲突" in failure["reason"]
    assert "缺少批号" in failure["reason"]
    assert data["items"][0]["holder"] == "A"


def test_batch_missing_then_present_is_conflict():
    s1 = scan(serial="SN9")
    s2 = scan(serial="SN9", batch="LOT1")
    events = [
        ev(s1, "2026-01-01T08:00:00+08:00", "A", None),
        ev(s2, "2026-01-02T08:00:00+08:00", "B", "A"),
    ]
    data = audit(events)
    (failure,) = data["failures"]
    assert failure["index"] == 1
    assert "批号冲突" in failure["reason"]
    assert "缺少批号" in failure["reason"]


def test_expiry_mismatch_conflict_does_not_change_holder():
    s1 = scan(serial="SN9", expiry="251231")
    s2 = scan(serial="SN9", expiry="260101")
    events = [
        ev(s1, "2026-01-01T08:00:00+08:00", "A", None),
        ev(s2, "2026-01-02T08:00:00+08:00", "B", "A"),
    ]
    data = audit(events)
    (failure,) = data["failures"]
    assert failure["index"] == 1
    assert "有效期冲突" in failure["reason"]
    assert "2025-12-31" in failure["reason"]
    assert "2026-01-01" in failure["reason"]
    assert data["items"][0]["holder"] == "A"


def test_expiry_missing_then_present_is_conflict():
    s1 = scan(serial="SN9")
    s2 = scan(serial="SN9", expiry="251231")
    events = [
        ev(s1, "2026-01-01T08:00:00+08:00", "A", None),
        ev(s2, "2026-01-02T08:00:00+08:00", "B", "A"),
    ]
    data = audit(events)
    (failure,) = data["failures"]
    assert failure["index"] == 1
    assert "有效期冲突" in failure["reason"]
    assert "缺少有效期" in failure["reason"]


# ---------- 响应排序 ----------

def test_items_sorted_by_gtin_then_serial():
    # 同一 GTIN:SN2 先于 SN1 出现,响应仍按序列号排序
    events = [
        ev(scan(gtin=GTIN2, serial="SN1"), "2026-01-01T08:00:00+08:00", "X", None),
        ev(scan(gtin=GTIN, serial="SN2"), "2026-01-01T08:00:00+08:00", "Y", None),
        ev(scan(gtin=GTIN, serial="SN1"), "2026-01-01T08:00:00+08:00", "Z", None),
    ]
    data = audit(events)
    assert [(i["gtin"], i["serial"]) for i in data["items"]] == [
        (GTIN, "SN1"),
        (GTIN, "SN2"),
        (GTIN2, "SN1"),
    ]


def test_failures_sorted_by_original_index():
    events = [
        ev("01" + GTIN + "99", "2026-01-01T08:00:00+08:00", "B", "A"),  # 非法
        ev(scan(serial="SN9"), "2026-01-01T09:00:00+08:00", "A", None),
        ev(scan(), "2026-01-01T10:00:00+08:00", "C", "A"),               # 无序列号
        ev(scan(serial="SN9"), "2026-01-01T11:00:00+08:00", "D", None),  # 链冲突
    ]
    data = audit(events)
    assert [f["index"] for f in data["failures"]] == [0, 2, 3]
    assert data["failures"][0]["offset"] == 2 + 14
    assert data["failures"][1]["offset"] is None
    assert data["failures"][2]["offset"] is None


# ---------- 请求体验证(422) ----------

def _expect_422(payload):
    resp = client.post("/v1/custody/audit", json=payload)
    assert resp.status_code == 422, resp.text


def test_validation_event_count_bounds():
    good = ev(scan(serial="SN9"), "2026-01-01T08:00:00+08:00", "A", None)
    _expect_422({"events": []})
    _expect_422({"events": [good] * 301})


def test_validation_occurred_at_must_be_timezone_aware():
    _expect_422(
        {
            "events": [
                ev(scan(serial="SN9"), "2026-01-01T08:00:00", "A", None),
            ]
        }
    )


def test_validation_to_location_nonempty():
    _expect_422(
        {"events": [ev(scan(serial="SN9"), "2026-01-01T08:00:00+08:00", "", None)]}
    )


def test_validation_from_location_empty_string_rejected():
    _expect_422(
        {"events": [ev(scan(serial="SN9"), "2026-01-01T08:00:00+08:00", "A", "")]}
    )


def test_validation_occurred_at_required():
    payload = {"events": [{"scan": scan(serial="SN9"), "to_location": "A"}]}
    _expect_422(payload)


def test_validation_extra_field_rejected():
    _expect_422(
        {
            "events": [
                {
                    "scan": scan(serial="SN9"),
                    "occurred_at": "2026-01-01T08:00:00+08:00",
                    "from_location": None,
                    "to_location": "A",
                    "carrier": "X",
                }
            ]
        }
    )


def test_occurred_at_returned_as_utc_not_applicable_to_response():
    # 响应中不回显时间;确认 +09:00 与同日 +00:00 的事件按 UTC 先后串成链
    s = scan(serial="SN9")
    events = [
        ev(s, "2026-01-02T00:30:00+09:00", "B", "A"),   # UTC 01-01 15:30
        ev(s, "2026-01-01T10:00:00+00:00", "A", None),  # UTC 01-01 10:00
    ]
    data = audit(events)
    assert data["failures"] == []
    assert data["items"][0]["event_indices"] == [1, 0]
