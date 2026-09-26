# GS1 召回扫码核验 API

基于 FastAPI 的 GS1 条码解析与召回规则匹配服务。上传一批扫描串和一批召回规则,逐条返回解析出的字段(GTIN / 批号 / 序列号 / 日期)以及命中的规则 ID。

## 快速启动

```bash
docker compose up --build
```

服务监听 **8000** 端口:

- 接口:`POST http://localhost:8000/v1/recall/check`
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

## 项目结构

```
app/
  main.py     # FastAPI 入口与路由
  gs1.py      # GS1 扫描串解析(AI 01/10/17/21、FNC1、]d2、校验位、日期)
  models.py   # 请求 / 响应模型与规则校验
  matcher.py  # 召回规则匹配
tests/
  test_recall.py
Dockerfile
docker-compose.yml
requirements.txt
```
