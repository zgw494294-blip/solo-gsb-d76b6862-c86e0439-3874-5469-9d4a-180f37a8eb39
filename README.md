# GS1 扫码核验 API

基于 FastAPI 的 GS1 条码解析服务,提供两个接口:

- **召回核验**:上传一批扫描串和一批召回规则,逐条返回解析出的字段(GTIN / 批号 / 序列号 / 日期)以及命中的规则 ID。
- **交接审计**:上传一批实物交接事件,按时间重放并审计每件实物(GTIN + 序列号)的持有人流转,返回成功事件与失败原因。

## 快速启动

```bash
docker compose up --build
```

服务监听 **8000** 端口:

- 召回核验:`POST http://localhost:8000/v1/recall/check`
- 交接审计:`POST http://localhost:8000/v1/custody/audit`
- 交互文档:`http://localhost:8000/docs`
- 健康检查:`GET http://localhost:8000/healthz`

本地开发(无需 Docker):

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

运行测试:

```bash
pip install pytest httpx
pytest
```

## 接口说明

### `POST /v1/recall/check`

请求体:

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `scans` | `string[]` | 1~100 条扫描串 |
| `rules` | `object[]` | 1~100 条规则,`id` 必须唯一 |

规则字段:

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `id` | 是 | 规则 ID,1~64 字符,请求内唯一 |
| `gtin` | 是 | 14 位数字,须通过 GS1 模 10 校验位 |
| `batch` | 否 | 批号,1~20 位可打印 ASCII;附带时扫描串须含 AI 10 且相等 |
| `serial` | 否 | 序列号,1~20 位可打印 ASCII;附带时扫描串须含 AI 21 且相等 |
| `date_from` / `date_to` | 否 | ISO 日期(`YYYY-MM-DD`)闭区间;附带时扫描串须含 AI 17 且日期落在区间内(含端点) |

规则不合法(GTIN 校验位错误、ID 重复、数量超限、日期区间倒置等)时整个请求返回 **422**。

### 扫描串语法

- 可带符号标识符前缀 `]d2`(GS1 DataMatrix)。
- FNC1 为 **ASCII 29**(`\x1d`,GS),用作字段分隔符。
- 支持的 AI,可按**任意顺序**出现,每种**至多一次**:

| AI | 含义 | 格式 |
| --- | --- | --- |
| `01` | GTIN | 定长 14 位数字,模 10 校验位,**必填** |
| `17` | 日期 | 定长 6 位 `YYMMDD`,年按 2000~2099 解释,日 `00` 取当月末日 |
| `10` | 批号 | 变长 1~20 位可打印 ASCII,**仅由 FNC1 或串尾结束** |
| `21` | 序列号 | 变长 1~20 位可打印 ASCII,**仅由 FNC1 或串尾结束** |

定长字段后的 FNC1 可省略;变长字段若后面还有 AI,必须用 FNC1 分隔,否则后续内容会被吞进该字段。

### 响应

```json
{
  "results": [
    {
      "index": 0,
      "ok": true,
      "fields": {
        "gtin": "09506001123454",
        "batch": "LOT1",
        "serial": "SN9",
        "date": "2025-12-31"
      },
      "matches": ["recall-2026-001"],
      "error": null
    },
    {
      "index": 1,
      "ok": false,
      "fields": null,
      "matches": [],
      "error": { "offset": 16, "reason": "未知 AI: '99'" }
    }
  ]
}
```

- `results` 与 `scans` 一一对应,`index` 为扫描串下标。
- `ok=true`:`fields` 为解析出的字段(`date` 为 AI 17 解析结果,ISO 格式),`matches` 为命中的规则 ID,**按请求中规则的顺序排列**。
- `ok=false`:解析失败,`error.offset` 为出错位置(**相对原始扫描串、含 `]d2` 前缀,从 0 计**),`error.reason` 为原因(未知 AI、非法字符、长度不足 / 超长、分隔符误用、校验位错误、日期非法、缺少 AI 01 等)。**单条失败不影响其余条目**。

### 示例

```bash
curl -s http://localhost:8000/v1/recall/check \
  -H 'Content-Type: application/json' \
  -d '{
    "scans": ["]d201095060011234541725123110LOT1\u001d21SN9"],
    "rules": [
      {"id": "recall-2026-001", "gtin": "09506001123454", "batch": "LOT1",
       "date_from": "2025-01-01", "date_to": "2026-12-31"}
    ]
  }'
```

> JSON 中 FNC1 写作 `\u001d`;shell 里也可用 `$'...\x1d...'` 构造。

### `POST /v1/custody/audit`

审计一批实物交接事件:按发生时间重放,维护每件实物(**GTIN + AI 21 序列号**)的持有人,返回每件实物的成功递交结果与失败事件的原因。

请求体:

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `events` | `object[]` | 1~300 条交接事件 |

事件字段:

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `scan` | 是 | GS1 扫描串,语法同召回核验;须含 AI 01 与 AI 21(序列号) |
| `occurred_at` | 是 | ISO 8601 时间,**须带时区**(如 `2026-09-01T08:30:00+08:00`) |
| `from_location` | 是(可为 `null`) | 交出方;实物的首次事件须为 `null` |
| `to_location` | 是 | 接收方,**非空**字符串 |

处理规则:

1. 事件按 `occurred_at` **转 UTC 后升序**处理;同一时刻依输入顺序。
2. 扫描非法或缺少 AI 21 序列号的事件按原下标报错,不参与流转。
3. 每件实物的**首次成功事件** `from_location` 须为 `null`;后续事件 `from_location` 须等于**上次成功事件**的 `to_location`。
4. 批号(AI 10)、有效期(AI 17)须与首次成功扫描一致(**含字段缺失**:首次没有该字段时,后续也不能有),否则记录冲突且**不改变持有人**。
5. 只有成功事件参与流转;失败事件不影响后续事件的判定基准。

响应:

```json
{
  "items": [
    {
      "gtin": "09506001123454",
      "serial": "SN9",
      "events": [1, 0],
      "holder": "WH-B"
    }
  ],
  "errors": [
    { "index": 2, "reason": "from_location 须等于上次成功事件的 to_location 'WH-B',实际为 'WH-X'" }
  ]
}
```

- `items` 按 **GTIN、序列号**排序;`events` 为该实物成功事件的原始下标(按处理顺序,即时间升序),`holder` 为最终持有人(最后一次成功事件的 `to_location`)。
- `errors` 按**原始下标**排序,`reason` 说明失败原因(扫描非法、缺少序列号、首次事件 `from_location` 非 `null`、交接链断裂、批号 / 有效期冲突)。
- 请求体不合法(事件数量超限、`occurred_at` 缺时区、`to_location` 为空、字段缺失或多余等)时整个请求返回 **422**。

示例:

```bash
curl -s http://localhost:8000/v1/custody/audit \
  -H 'Content-Type: application/json' \
  -d '{
    "events": [
      {"scan": "01095060011234541725123110LOT1\u001d21SN9",
       "occurred_at": "2026-09-01T09:00:00+08:00",
       "from_location": null, "to_location": "WH-A"},
      {"scan": "01095060011234541725123110LOT1\u001d21SN9",
       "occurred_at": "2026-09-01T10:00:00+08:00",
       "from_location": "WH-A", "to_location": "WH-B"}
    ]
  }'
```

## 项目结构

```
app/
  main.py     # FastAPI 入口与路由
  gs1.py      # GS1 扫描串解析(AI 01/10/17/21、FNC1、]d2、校验位、日期)
  models.py   # 请求 / 响应模型与规则校验
  matcher.py  # 召回规则匹配
  custody.py  # 实物交接审计(按时间重放事件,维护持有人)
tests/
  test_recall.py
  test_custody.py
Dockerfile
docker-compose.yml
requirements.txt
```
