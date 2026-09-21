# 科技杠杆 ETF 名义成交额监控 — 设计文档

> **用途**：在 Athena 网站增加 **美股科技向杠杆/反向 ETF 月度名义成交额折线图**，用于观察科技投机活跃度（降温/升温）。  
> **版本**：v1.0（2026-09-21）  
> **关联**：本仓库已验证代理算法与样例图  
> - `levered_etf_tech_notional_2025_2026sep.png` / `.csv`（2025–2026 样例）  
> - `levered_etf_notional_2025_2026sep.png`（全市场代理对比，可选）  
> **原则**：公开行情加总代理，**不宣称等于 Apex Fintech 专有数据**；可按年筛选；从 **2023-01** 起存全量月度序列。  
> **落地位置建议**：`/deals` 新增 Tab「杠杆活跃度」或挂在「AI 主线」页底部「投机温度」卡片；也可独立路由 `/labs/lev-etf-tech`。

---

## 0. 给 Athena 开发者的核心约束

1. **指标定义固定**：月度名义成交额 = 篮子内各标的当日 `Close × Volume` 之和，再按美东交易月汇总（美元十亿）。  
2. **只做科技向篮子**（见 §2）；全市场篮子可作为可选对比线，v1 可不做。  
3. **统计起点：2023-01**；UI 支持 **按年查看**（单年 / 全部年份）。  
4. **禁止把数字标成「Apex 官方」**：页面注明「Yahoo/公开行情代理；与媒体引用的 Apex 采访口径不同」。  
5. **需要独立历史库**：heatmap 现价快照不够用；本模块要日频 OHLCV 落库后再聚合月度。  
6. **不做自动下单、不推送手机**（v1）；网站只读图表。  
7. **时区**：日线归属按美东交易日；月键 `YYYY-MM`。  
8. 当月未结束时，标记 `is_partial=true`（例如「截至 MM-DD」）。

---

## 1. 项目目标

### 1.1 要解决什么问题

用户（及 Athena 读者）需要快速判断：

- 科技杠杆 ETF 投机是否在升温/降温  
- 与新闻叙事对照（如「杠杆 ETF 名义成交腰斩」）  
- 按年回看 2023 / 2024 / 2025 / 2026 形态  

### 1.2 系统目标

1. 每日收盘后拉取科技杠杆 ETF 篮子日频成交额并入库  
2. 聚合生成自 2023-01 起的月度序列  
3. 前端折线图 + **年份筛选**（`all` | `2023` | `2024` | …）  
4. API 只读输出，供 `/deals` Tab 或独立页消费  

### 1.3 不做什么（v1）

- 不爬取 Apex Investor Pulse / 采访数字当主数据源  
- 不做成分动态权重（市值加权）；默认等权加总成交额  
- 不预测下月成交  
- 不替代 AI 主线相对强弱模块  

---

## 2. 科技篮子定义

### 2.1 默认篮子 `tech_lev_etf`（v1）

写入配置：`data/lev_etf_tech_basket.json`

| 分组 | 代码 |
|------|------|
| 纳指 2x/3x | TQQQ, SQQQ, QLD, QID |
| 半导体 3x | SOXL, SOXS |
| 科技板块 3x | TECL, TECS |
| FANG / 主题 | FNGU, FNGD, BULZ, BERZ |
| 单股杠杆（科技巨头） | NVDL, NVDX, NVDU, NVDD, TSLL, TSLQ, CONL, AAPU, AAPD, MSFU, MSFD, AMZU, AMZD, METU, METD, GGLL, GGLS |

> 单股杠杆上市时间不一：某月尚无交易的标的，该月贡献为 0（自然存活偏置，文档需在 UI 脚注说明）。

### 2.2 JSON 结构

```json
{
  "version": 1,
  "key": "tech_lev_etf",
  "name": "科技杠杆/反向 ETF",
  "name_en": "Tech Leveraged/Inverse ETFs",
  "currency": "USD",
  "start_month": "2023-01",
  "groups": [
    {
      "key": "nasdaq",
      "name": "纳指杠杆",
      "tickers": ["TQQQ", "SQQQ", "QLD", "QID"]
    },
    {
      "key": "semi",
      "name": "半导体杠杆",
      "tickers": ["SOXL", "SOXS"]
    },
    {
      "key": "tech_sector",
      "name": "科技板块杠杆",
      "tickers": ["TECL", "TECS"]
    },
    {
      "key": "fang",
      "name": "FANG/主题",
      "tickers": ["FNGU", "FNGD", "BULZ", "BERZ"]
    },
    {
      "key": "single_stock",
      "name": "单股科技杠杆",
      "tickers": [
        "NVDL", "NVDX", "NVDU", "NVDD",
        "TSLL", "TSLQ", "CONL",
        "AAPU", "AAPD", "MSFU", "MSFD",
        "AMZU", "AMZD", "METU", "METD",
        "GGLL", "GGLS"
      ]
    }
  ]
}
```

### 2.3 可选对比篮子（v1.1）

`all_lev_etf`：在科技篮基础上加 UPRO/SPXU、TNA/TZA、FAS/FAZ、LABU/LABD、NUGT/DUST、BITX 等（见本仓库全市场样例）。v1 **可不实现**，仅预留 `series_key`。

---

## 3. 指标与算法

### 3.1 日度

对篮子每个 `ticker`：

```
daily_notional[t, d] = Close[t, d] * Volume[t, d]
```

缺行情 / 停牌 / 未上市：记 `null`，汇总时跳过（视为 0 贡献）。

```
daily_basket[d] = Σ daily_notional[t, d]   # over available tickers
```

### 3.2 月度

```
month_key = YYYY-MM  # 按美东交易日的日历月
monthly_notional_usd[m] = Σ daily_basket[d] for d in m
monthly_notional_bn[m]  = monthly_notional_usd[m] / 1e9
```

### 3.3 当月未完

```
is_partial = (month_key == current_et_month) AND (last_bar_date < month_end)
as_of_date = 最近一个有数据的美东交易日
```

前端：点上标注 `*` 或 Tooltip「截至 as_of_date」。

### 3.4 与媒体/Apex 数字的关系（必须写进 UI）

| 项目 | Apex 采访口径（例） | 本模块 |
|------|---------------------|--------|
| 来源 | 平台专有汇总 / 采访估算 | 公开成交额加总 |
| 宇宙 | 未知（可能全市场或平台子集） | 固定科技篮子 |
| 用途 | 叙事引用 | Athena 可持续监控 |

**结论**：趋势可对照，绝对值不必对齐。

---

## 4. 数据层

### 4.1 行情源

优先顺序（实现任选其一，写入 `config`）：

1. **已有 Athena 行情中台**（若已有日频 OHLCV 历史）——最优先复用  
2. Yahoo Chart API `v8/finance/chart/{symbol}?interval=1d&period1=&period2=`（注意限流；生产用官方/付费源更稳）  
3. Polygon / Tiingo / 券商历史 API  

> 本地验证：浏览器 Session 调 Yahoo chart 可拉通；服务端直连易 403/限流，**生产勿裸爬**。

### 4.2 表结构（逻辑）

**`lev_etf_daily`**

| 字段 | 类型 | 说明 |
|------|------|------|
| `trade_date` | date | 美东交易日 |
| `symbol` | text | |
| `close` | numeric | |
| `volume` | bigint | |
| `notional` | numeric | close×volume |
| `basket_key` | text | `tech_lev_etf` |
| `source` | text | |
| `updated_at` | timestamptz | |

唯一键：`(basket_key, symbol, trade_date)`

**`lev_etf_monthly`**

| 字段 | 类型 | 说明 |
|------|------|------|
| `month` | char(7) | `YYYY-MM` |
| `basket_key` | text | |
| `notional_usd` | numeric | |
| `notional_bn` | numeric | 展示用 |
| `trading_days` | int | |
| `ticker_count_avg` | numeric | 可选 |
| `is_partial` | bool | |
| `as_of_date` | date | |
| `updated_at` | timestamptz | |

唯一键：`(basket_key, month)`

**可选快照文件**（无 DB 时 MVP）：

```
data/lev_etf_tech_monthly.json
```

### 4.3 回填与增量 Job

| Job | 频率 | 内容 |
|-----|------|------|
| `lev_etf_backfill` | 一次性 / 改篮子时 | 自 `2023-01-01` 拉到昨日，写 daily + 重算 monthly |
| `lev_etf_daily_update` | 每个美东交易日收盘后（建议 16:30–18:00 ET） | 拉当日（及补缺），重算当月月度行 |
| `lev_etf_integrity` | 每周 | 检查缺口交易日、异常 0 成交 |

伪代码：

```
for symbol in basket.tickers:
    bars = fetch_ohlcv(symbol, start=2023-01-01, end=today_et)
    upsert lev_etf_daily

for month in months_since(2023-01):
    aggregate -> upsert lev_etf_monthly
```

---

## 5. API 设计

### 5.1 月度序列（主接口）

```
GET /api/lev-etf/tech/monthly?year=all|2023|2024|2025|2026
```

**Response 示例**

```json
{
  "basket_key": "tech_lev_etf",
  "name": "科技杠杆/反向 ETF",
  "unit": "USD_billion",
  "start_month": "2023-01",
  "year_filter": "all",
  "as_of_date": "2026-09-18",
  "disclaimer": "Public market proxy (Close×Volume sum). Not Apex Fintech proprietary data.",
  "points": [
    {
      "month": "2023-01",
      "notional_bn": 120.5,
      "is_partial": false,
      "trading_days": 20
    }
  ],
  "stats": {
    "min": {"month": "2023-xx", "notional_bn": 0},
    "max": {"month": "2026-06", "notional_bn": 654.1},
    "latest": {"month": "2026-09", "notional_bn": 226.1, "is_partial": true}
  },
  "available_years": [2023, 2024, 2025, 2026]
}
```

规则：

- `year=all`：返回 `month >= 2023-01` 全部点  
- `year=2025`：仅 `2025-01`…`2025-12`  
- 非法年份 → 400  

### 5.2 元数据

```
GET /api/lev-etf/tech/meta
```

返回篮子成分、分组、数据源、`start_month`、更新时间。

### 5.3 （可选）成分贡献 Top

```
GET /api/lev-etf/tech/contributors?month=2026-06&limit=10
```

用于 Tooltip「本月谁贡献最大」（TQQQ/SOXL 等）。

---

## 6. 前端 UI

### 6.1 布局

卡片标题：**科技杠杆 ETF 名义成交额（月）**

控件：

| 控件 | 行为 |
|------|------|
| 年份 Select | `全部` / `2023` / `2024` / `2025` / `2026`…（来自 `available_years`） |
| （可选）对比开关 | 叠加全市场代理线 — v1.1 |

图表：

- X：`YYYY-MM`  
- Y：十亿美元  
- 折线 + 面积浅填充  
- 当前部分月：空心点或 `*`  
- 峰值点可选标注  

脚注（固定文案）：

> 名义成交额 = 科技杠杆/反向 ETF 篮子当日收盘价×成交量之和的月度合计。公开行情代理，非 Apex 专有数据。单股杠杆按上市后自然纳入。

### 6.2 按年看的交互

- 切换年份 → 重新请求 `?year=` 或前端过滤已缓存的 `all`  
- 推荐：**先拉 `year=all`，前端按年过滤**（点数少，~几十个月），切换零延迟  
- 若未来改日频再改为服务端过滤  

### 6.3 空态 / 加载

- 加载中 Skeleton  
- 无数据：提示「回填任务未完成」  
- 部分月：图例说明  

### 6.4 建议组件

复用站内已有图表库（ECharts / Chart.js / Recharts）。示例系列：

```ts
type Point = { month: string; notional_bn: number; is_partial: boolean };
// filter: year === 'all' ? points : points.filter(p => p.month.startsWith(year))
```

---

## 7. 配置项

```yaml
# config / env
LEV_ETF_TECH_START: "2023-01-01"
LEV_ETF_TECH_BASKET_PATH: "data/lev_etf_tech_basket.json"
LEV_ETF_DATA_SOURCE: "yahoo_chart"   # or polygon / internal
LEV_ETF_UPDATE_CRON: "30 21 * * 1-5"  # 例：美东收盘后，按服务器时区调整
LEV_ETF_SHOW_ALL_COMPARE: false
```

---

## 8. 目录与模块建议（Athena）

```
data/lev_etf_tech_basket.json
data/lev_etf_tech_monthly.json          # MVP 可无 DB
app/lev_etf/
  basket.py
  fetch_ohlcv.py
  aggregate.py
  jobs.py
  api.py
web/ (或 templates/static)
  LevEtfTechChart.tsx / .vue / 模板片段
```

路由挂载示例：

- Tab：`/deals` →「杠杆活跃度」  
- 或：`/labs/lev-etf-tech`  

---

## 9. 验收清单

- [ ] 月度序列自 **2023-01** 起连续（允许个别标的早期缺失）  
- [ ] `year=all` 与单年筛选结果正确  
- [ ] 当月 `is_partial` + `as_of_date` 展示正确  
- [ ] 脚注含「非 Apex」声明  
- [ ] 改篮子 JSON 后可触发重算  
- [ ] 日更 Job 失败有日志/告警  
- [ ] 与样例趋势同向：2026-06 高点、2026-08/09 回落（数量级因源/复权可略有差异）  

---

## 10. 分阶段落地

### Phase 0 — 配置

- 提交 `lev_etf_tech_basket.json`  
- 定 Tab 位置与文案  

### Phase 1 — MVP

- 回填 2023→今 → 写 `lev_etf_tech_monthly.json`  
- `GET /api/lev-etf/tech/monthly`  
- 前端折线 + 年份 Select  

### Phase 2 — 自动化

- 日更 Job + DB 表  
- 完整性检查  

### Phase 3 — 增强（可选）

- 成分 Top 贡献  
- 全市场对比线  
- 同比/环比标注  

---

## 11. 本地已验证参考（给实现对照）

本 Sandisk 仓库手工验证（Yahoo 日线，科技 29 标的）：

| 月 | notional_bn（约） |
|----|-------------------|
| 2025-04 | 531.7 |
| 2026-06 | 654.1（近峰） |
| 2026-09* | 226.1 |

文件：`levered_etf_tech_notional_2025_2026sep.csv`  
Athena 回填 2023 后，数值不必逐点一致，但 **峰谷形态应可辨**。

---

## 12. 一句话产品说明（可直接上页面）

> **科技杠杆 ETF 名义成交额**：统计自 2023 年起、美股科技向杠杆/反向 ETF 的月度公开成交活跃度；支持按年查看。用于观察科技投机温度，数据为公开代理口径。

---

**文档结束。** 将本文与 `data/lev_etf_tech_basket.json`（实现时创建）一并交给 Athena 即可开工；阈值/成分变更请升 `basket.version`。
