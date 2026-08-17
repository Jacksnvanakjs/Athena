# AI 产业链「大厂 ↔ 小厂」合作快讯监控 — 设计文档

> **用途**：在 Athena 网站项目中实现「合作消息 → 解析受益小票 → 去重推送」功能。  
> **版本**：v1.0（2026-08-17）  
> **原则**：不设固定 50 家小票围墙；用 **市值分档 T0/T1/T2 + 相对角色** 决定推送对象。

---

## 1. 项目目标

### 1.1 要解决什么问题

当 **AI 产业链大公司** 与 **相对小市值上市公司** 出现 **算力/数据中心/租赁类** 合作新闻时，小票常在 **盘前或盘中短时间内暴涨**（例：RIOT 与 Anthropic 算力协议）。

系统目标：

1. **尽早发现** 材料性合作新闻（英文为主，中文为辅）
2. **自动识别** 合作双方，并判定 **谁是锚点、谁是受益推送对象**
3. **去重推送**：同一受益 ticker **7 天内同类事件只推一次**
4. 在 Athena 网站提供 **快讯列表页 + API**，并可选 **微信 PushPlus / Server酱**

### 1.2 不做什么（v1 范围外）

- 不做自动下单、不做投资建议
- 不保证「暴涨前」推送（延迟取决于数据源）
- 不覆盖 A 股/港股（v1 仅 **美股**）
- 不做完整 NLP 情感分析（v1 用规则 + 关键词 + 材料性打分）

---

## 2. 核心设计：市值分档 + 相对角色

### 2.1 分档定义（美元，可配置）

| 层级 | 市值区间（默认） | 角色 |
|------|------------------|------|
| **T0** | **> 5000 亿美元**，或 **未上市巨头**（见种子表） | 仅作 **新闻源 / 锚点**，本身一般不推「小票逻辑」 |
| **T1** | **50 亿～5000 亿美元**（$5B～$500B） | **双向**：相对 T0 是受益方；相对 T2 是锚点 |
| **T2** | **< 50 亿美元**（$5B） | **主要推送对象** |
| **UNKNOWN** | 查不到 ticker / 未上市非巨头 | 不推送，仅记日志 |

**说明**

- 市值用 **流通市值**（float market cap）优先，缺省用总市值
- 分档 **每日或每周刷新**（从行情 API 更新），避免 CRWV 涨大后仍被当 T2 推
- 「50 亿 / 5000 亿」为默认值，写入 `config.py` / 环境变量

### 2.2 推送规则（相对角色）

对一条新闻解析出的合作双方 **A、B**（均已映射到 ticker 或 T0 未上市锚点）：

| 组合 | 推送对象 | 说明 |
|------|----------|------|
| **T0 ↔ T2** | **T2** | 最直接，优先推送 |
| **T0 ↔ T1** | **T1**（材料性够） | 如 OpenAI ↔ CoreWeave |
| **T1 ↔ T2** | **T2** | CoreWeave ↔ RIOT 类 |
| **T0 ↔ T0** | **市值较小的一侧** | 按你的要求：仍推送，但标 `tier_pair=T0_T0` |
| **T1 ↔ T1** | **市值较小的一侧** | 标 `tier_pair=T1_T1`，可选提高材料性门槛 |
| **T2 ↔ T2** | **市值更小者**（或双推） | 默认只推更小者；配置 `T2_T2_PUSH_BOTH=true` 时可推两个 |
| **T0 ↔ UNKNOWN** | 不推 | 无法交易 |
| **仅单方出现、无合作语义** | 不推 | 关键词命中但非「合作」 |

**锚点（anchor）**：组合中 **市值较大** 的一方；**受益（beneficiary）**：**被推送** 的一方。

**CoreWeave 示例**

```
OpenAI (T0 未上市) ↔ CRWV (T1)  → 推 CRWV，锚点 OpenAI
CRWV (T1) ↔ RIOT (T2)           → 推 RIOT，锚点 CRWV
ORCL (T0) ↔ CRWV (T1)           → 推 CRWV，锚点 ORCL
MSFT (T0) ↔ GOOG (T0)           → 推市值较小者（通常仍很大，见 2.3）
```

### 2.3 T0 ↔ T0 的特殊处理

T0↔T0 仍可能带动 **相对弱势一侧** 的短线波动（如 ORCL vs META 生态合作），但 **暴涨概率低于 T0↔T2**。

建议：

- **仍推送**，受益方为 **市值较小者**
- 消息模板增加标签：`⚠️ 双巨头合作，小票弹性有限`
- 材料性打分 **门槛提高**（默认 ≥ 70 才推，普通组合 ≥ 55）
- 可选配置：`T0_T0_PUSH_ENABLED=false` 关闭此类推送

---

## 3. 系统架构（对齐 Athena 现有结构）

Athena 已有：`app/heatmap.py`、`app/scheduler.py`、`app/routes.py`、`app/database.py`、PushPlus/Server酱。

建议新增模块：

```
app/
  deal_monitor/
    __init__.py
    config.py          # 阈值、关键词、轮询间隔
    tiers.py           # 分档逻辑、相对角色判定
    entities.py        # 公司名 → ticker、别名表
    keywords.py        # 中英关键词、负向词
    materiality.py     # 材料性打分
    fetchers/
      sec_edgar.py     # 8-K material definitive agreement
      pr_wire.py       # PR Newswire / GlobeNewswire RSS
      company_ir.py    # 可选：大厂 IR RSS
    parser.py          # 标题+正文抽取、合作双方 NER
    pipeline.py        # 抓取 → 解析 → 打分 → 去重 → 入库 → 推送
    market_cap.py      # 市值缓存刷新
  database.py          # 新增 DealEvent 等表（见 §8）
  routes.py            # 新增 /deals API
  scheduler.py           # 新增定时任务
templates/
  deals.html             # 快讯列表页
data/
  entities_seed.json     # 种子：T0 未上市、别名、AI 链公司
  market_cap_cache.json  # 可选本地缓存
```

### 3.1 数据流

```
┌─────────────┐     ┌──────────────┐     ┌─────────────┐
│ 数据源轮询   │────▶│ 关键词+材料性 │────▶│ 实体识别     │
│ RSS/8-K/... │     │ 初筛         │     │ 公司名→ticker│
└─────────────┘     └──────────────┘     └──────┬──────┘
                                                 │
                    ┌──────────────┐     ┌──────▼──────┐
                    │ 推送         │◀────│ 分档+角色    │
                    │ PushPlus/页面│     │ T0/T1/T2    │
                    └──────────────┘     └──────┬──────┘
                                                 │
                                          ┌──────▼──────┐
                                          │ 去重+入库    │
                                          │ SQLite/Turso │
                                          └─────────────┘
```

### 3.2 监控源（双轨，缺一不可）

| 轨道 | 说明 | 优先级 |
|------|------|--------|
| **A. 锚点源** | T0/T1 名单相关新闻（大厂 PR、8-K） | 发现「被点名」的受益方 |
| **B. 受益方源** | 全市场 PR/RSS：关键词 + **市值 < T2 上限** | 抓 **小票自己发的合作稿**（暴涨常从这里开始） |

**仅做轨道 A 会漏报**；轨道 B 可用「标题含关键词 + 发文公司为 T2」过滤。

---

## 4. 材料性打分（Materiality Score）

关键词命中 ≠ 值得推送。对每条候选新闻计算 **0～100 分**。

### 4.1 加分项

| 信号 | 分值 | 示例 |
|------|------|------|
| 含金额（$X million/billion） | +15～25 | `$9.1 billion` |
| 含年限 multi-year / X-year | +10～15 | `5-year agreement` |
| definitive agreement / 正式协议 | +15 | 8-K Item 1.01 |
| 算力/GW/MW/GPU 容量数字 | +10～20 | `4.5 GW`, `100MW` |
| 标题出现受益方 ticker 公司全名 | +10 | |
| 来源为 PR wire 或 8-K | +5～10 | |
| 合作动词：signs, enters, awards, lease | +5 | |

### 4.2 减分项

| 信号 | 分值 | 示例 |
|------|------|------|
| MOU / 谅解备忘录 / explore / 探索 | -20 | |
| non-binding / 非约束 | -25 | |
| strategic partnership 无金额无期限 | -15 | |
| 纯研究合作 / 大学 / 实验室 | -30 | |

### 4.3 推送门槛（默认）

| 组合 | 最低分 |
|------|--------|
| T0 ↔ T2 | **55** |
| T0 ↔ T1 | **60** |
| T1 ↔ T2 | **55** |
| T0 ↔ T0 | **70** |
| T1 ↔ T1 | **65** |
| T2 ↔ T2 | **55** |

---

## 5. 关键词库

### 5.1 高价值（中英）

**中文**

```
算力协议, 算力服务, 计算能力, 数据中心, 机房, 托管, 租赁协议,
容量协议, GPU, 训练, 推理, 智算, 多年期, 独家, 千兆瓦, 兆瓦
```

**英文**

```
compute agreement, capacity agreement, cloud services agreement,
colocation, hosting agreement, data center lease, AI infrastructure,
GPU deployment, training capacity, inference capacity,
multi-year, gigawatt, MW capacity, power capacity,
hyperscaler, lease, definitive agreement, material definitive agreement
```

### 5.2 负向（降权或排除）

```
MOU, memorandum of understanding, explore partnership,
strategic partnership（无金额时）, non-binding,
joint research, academic collaboration
```

### 5.3 合作触发词（需与关键词共现）

```
sign, signed, enters, enter into, award, awarded, partner, partnership,
collaborate, agreement, contract, lease, expand, deploy
```

---

## 6. 实体识别与公司库

### 6.1 不维护「固定 50 家小票」

维护三类数据：

1. **T0 未上市锚点表**（固定，人工维护，量少）
2. **别名表**（公司名 / 旧名 → ticker，可持续追加）
3. **动态 ticker 库**（从 SEC / 行情 API 拉全量美股，按市值分档）

### 6.2 T0 未上市锚点（种子，需人工更新）

| 名称 | 类型 | 备注 |
|------|------|------|
| OpenAI | AI 实验室 | 无 ticker |
| Anthropic | AI 实验室 | 无 ticker |
| xAI | AI 实验室 | 无 ticker |
| SoftBank |  conglomerate | 可选：ADR `SFTBY` 作 T0 上市代表 |
| Stargate / Stargate AI | 项目名 | 非单一 ticker，作关键词非实体 |

### 6.3 T0 上市锚点（种子，市值 > $500B 或 AI 核心）

| Ticker | 名称 | 备注 |
|--------|------|------|
| MSFT | Microsoft | |
| GOOG / GOOGL | Alphabet | |
| AMZN | Amazon | |
| META | Meta | |
| NVDA | Nvidia | |
| ORCL | Oracle | |
| AAPL | Apple | 可选：AI 合作较少仍作 T0 |
| TSM | TSMC | ADR |

### 6.4 T1 种子（50亿～5000亿，AI 算力/云/服务器，双向）

| Ticker | 名称 | 子类 |
|--------|------|------|
| CRWV | CoreWeave | Neo-cloud |
| NBIS | Nebius | Neo-cloud |
| SMCI | Super Micro | AI 服务器 |
| DELL | Dell | AI 服务器 |
| ANET | Arista Networks | 网络 |
| VRT | Vertiv | 电力/散热 |
| MU | Micron | 存储/HBM |
| AVGO | Broadcom | 芯片+网络 |
| MRVL | Marvell | 互联 |
| EQIX | Equinix | 数据中心 REIT |
| DLR | Digital Realty | 数据中心 REIT |
| IREN | IREN | 矿转算力 |
| CIFR | Cipher Mining | 矿转算力 |
| WULF | TeraWulf | 矿转算力 |
| CORZ | Core Scientific | 矿转算力 |
| RIOT | Riot Platforms | 矿转算力 |
| APLD | Applied Digital | 算力托管 |
| HUT | Hut 8 | 矿转算力 |
| CLSK | CleanSpark | 矿转算力 |
| BITF | Bitfarms | 矿转算力 |

> 以上为 **别名与冷启动** 用，**不限制**监控范围；未在表中的 ticker 只要市值分档正确仍可推送。

### 6.5 T2 推送典型形态（< $50B，非穷举）

矿转算力、小型 Neo-cloud、小型数据中心运营商、部分光模块小票等。  
**轨道 B** 通过市值过滤自动覆盖，无需穷举。

### 6.6 别名表示例（`entities_seed.json` 结构）

```json
{
  "aliases": [
    {"names": ["Riot Platforms", "Riot Blockchain", "RIOT"], "ticker": "RIOT"},
    {"names": ["CoreWeave", "Core Weave"], "ticker": "CRWV"},
    {"names": ["Applied Digital", "Applied Digital Corporation"], "ticker": "APLD"},
    {"names": ["Cipher Mining"], "ticker": "CIFR"},
    {"names": ["TeraWulf"], "ticker": "WULF"},
    {"names": ["Core Scientific"], "ticker": "CORZ"},
    {"names": ["Super Micro Computer", "Supermicro", "SMCI"], "ticker": "SMCI"}
  ],
  "unlisted_anchors": [
    {"names": ["OpenAI", "Open AI"], "id": "openai"},
    {"names": ["Anthropic"], "id": "anthropic"},
    {"names": ["xAI", "X.AI"], "id": "xai"}
  ]
}
```

---

## 7. 去重策略

### 7.1 规则

| 维度 | 规则 |
|------|------|
| **主键** | `beneficiary_ticker` + `event_type` + **7 天滚动窗口** |
| **event_type** | 默认 `compute_deal`；可扩展 `dc_lease`, `partnership` |
| **同文转载** | `url` 或 `headline_hash`（归一化标题 MD5）相同 → 跳过 |
| **更新稿** | 标题含 `amend`, `expand`, `extend`, `追加`, `上调` 或金额变化 >20% → 允许 **二次推送**，标 `is_update=true` |

### 7.2 推送频率保护

- 同一 beneficiary **24 小时内最多 1 条推送**（即使 event_type 不同，可配置）
- 全局 **每小时最多 N 条**（防 PR wire 刷屏，默认 N=10）

---

## 8. 数据库设计（SQLAlchemy，对齐 Athena）

### 8.1 表：`deal_events`

| 字段 | 类型 | 说明 |
|------|------|------|
| id | Integer PK | |
| published_at | DateTime | 新闻发布时间（UTC 存，展示转北京/美东） |
| fetched_at | DateTime | 抓取时间 |
| headline | String(500) | 标题 |
| summary | Text | 摘要/正文前 2k 字符 |
| source | String(50) | `pr_newswire` / `globe` / `sec_8k` / `rss` |
| source_url | String(500) | 唯一索引 |
| headline_hash | String(32) | MD5 去重 |
| anchor_name | String(100) | 锚点显示名 |
| anchor_ticker | String(20) nullable | T0 未上市则 NULL |
| anchor_tier | String(10) | T0/T1/T2/UNLISTED |
| beneficiary_ticker | String(20) | 推送对象 |
| beneficiary_name | String(100) | |
| beneficiary_tier | String(10) | |
| beneficiary_market_cap_usd | Float | 抓取时市值 |
| tier_pair | String(20) | `T0_T2`, `T1_T2`, `T0_T0`, ... |
| materiality_score | Integer | 0-100 |
| matched_keywords | String(500) | JSON 数组字符串 |
| event_type | String(30) | 默认 `compute_deal` |
| is_update | Boolean | 是否更新稿 |
| pushed_at | DateTime nullable | 推送时间 |
| push_channel | String(30) | pushplus / serverchan / none |

**索引**：`beneficiary_ticker`, `published_at`, `headline_hash`, `(beneficiary_ticker, published_at)`

### 8.2 表：`entity_aliases`

| 字段 | 类型 | 说明 |
|------|------|------|
| id | Integer PK | |
| name | String(200) | 别名，唯一 |
| ticker | String(20) nullable | 上市则填 |
| unlisted_id | String(50) nullable | 未上市则填 |
| updated_at | DateTime | |

### 8.3 表：`market_cap_cache`

| 字段 | 类型 | 说明 |
|------|------|------|
| ticker | String(20) PK | |
| market_cap_usd | Float | |
| tier | String(10) | T0/T1/T2 |
| refreshed_at | DateTime | |

---

## 9. API 与页面

### 9.1 HTTP API（建议）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/deals` | 分页列表，`?days=7&tier_pair=T0_T2&min_score=55` |
| GET | `/deals/{id}` | 单条详情 |
| GET | `/deals/stats` | 近 7 日推送数、按 tier_pair 统计 |
| POST | `/deals/run` | 手动触发一轮抓取（需 admin token） |

### 9.2 页面 `deals.html`

- 表格：时间、受益 ticker、锚点、标题、分数、tier_pair、链接
- 筛选：tier_pair、最低分、仅已推送
- 首页可加「最新 5 条合作快讯」卡片

---

## 10. 推送消息模板

### 10.1 PushPlus / Server酱 标题

```
[AI合作] {beneficiary_ticker} ← {anchor_name} ({tier_pair}) 分{materiality_score}
```

### 10.2 正文 Markdown

```markdown
🔔 AI 产业链合作快讯

**受益**：{beneficiary_name} (`{beneficiary_ticker}`, {beneficiary_tier}, 市值约 ${cap_B}B)
**锚点**：{anchor_name} ({anchor_ticker_or_未上市}, {anchor_tier})
**关系**：{tier_pair}

**材料性**：{materiality_score}/100
**关键词**：{matched_keywords}

**标题**：{headline}
**时间**：{published_at_beijing}
**来源**：[链接]({source_url})

---
⚠️ 非投资建议；7 日内 `{beneficiary_ticker}` 同类事件仅推一次。
```

### 10.3 T0↔T0 附加行

```
⚠️ 双巨头合作，小票弹性有限，请谨慎。
```

---

## 11. 定时任务（scheduler）

| 任务 | 频率 | 说明 |
|------|------|------|
| `poll_pr_wires` | **每 2～5 分钟** | 盘前/盘中可加密集；夜间可降频 |
| `poll_sec_8k` | **每 5～10 分钟** | 8-K 偏慢但权威 |
| `refresh_market_cap` | **每日 1 次**（美东收盘后） | 更新分档 |
| `purge_old_events` | **每周** | 可选：保留 90 天 |

**美东盘前重点时段**（可选加密集）：04:00–09:30 ET，轮询间隔改为 1～2 分钟。

---

## 12. 配置项（环境变量）

```bash
# 分档阈值（美元）
DEAL_T0_MIN_CAP=500000000000      # 5000亿
DEAL_T1_MIN_CAP=5000000000        # 50亿
DEAL_T2_MAX_CAP=5000000000        # 同 T1 下限

# 材料性门槛
DEAL_SCORE_MIN_DEFAULT=55
DEAL_SCORE_MIN_T0_T0=70

# 去重
DEAL_DEDUP_DAYS=7
DEAL_MAX_PUSH_PER_HOUR=10

# 功能开关
DEAL_T0_T0_PUSH_ENABLED=true
DEAL_T2_T2_PUSH_BOTH=false

# 推送（沿用 Athena）
PUSHPLUS_TOKEN=
SERVERCHAN_SENDKEY=
DEAL_PUSH_ENABLED=true

# 数据源（按实现选填）
SEC_USER_AGENT=YourName your@email.com
FINNHUB_API_KEY=          # 可选：市值/新闻
POLYGON_API_KEY=          # 可选：市值

# Admin
DEAL_ADMIN_TOKEN=         # 手动 /deals/run
```

---

## 13. 数据源清单（实现参考）

| 来源 | URL/API | 延迟 | 备注 |
|------|---------|------|------|
| SEC EDGAR full-text | `efts.sec.gov/LATEST/search-index` 或 company 8-K RSS | 分钟～小时 | Item 1.01 材料协议 |
| PR Newswire RSS | 按关键词订阅 | 较快 | 英文稿主源 |
| GlobeNewswire RSS | 同上 | 较快 | |
| Business Wire | 部分需授权 | | v2 可选 |
| Finnhub Company News | `/company-news` | 快 | 需 API key |
| 公司 IR RSS | T0/T1 名单 | 中 | 补充 |

**User-Agent**：SEC 要求带联系邮箱。

---

## 14. 解析 pipeline 伪逻辑（无代码，供实现）

```
for each raw_item in fetch_all_sources():
    if not keyword_match(raw_item): continue
    if negative_keyword_dominates(raw_item): continue

    entities = extract_entities(raw_item)  # 公司名列表
    map entities → (ticker | unlisted_id | unknown)

    pairs = infer_partnership_pair(raw_item, entities)  # 启发式：标题/正文「与 X 合作」
    if not pairs: continue

    (anchor, beneficiary) = assign_roles_by_market_cap(pairs)
    tier_pair = format_tier_pair(anchor, beneficiary)

    score = materiality_score(raw_item)
    if score < threshold(tier_pair): continue

    if dedup_blocked(beneficiary.ticker, event_type, window=7d): continue

    save deal_events
    if DEAL_PUSH_ENABLED: push_notification()
```

**`infer_partnership_pair`** 启发式：

- 标题模式：`X Signs ... with Y` / `X and Y Announce`
- 正文：双方公司名均出现 + 合作动词 50 字范围内共现
- 8-K：Item 1.01 正文 counterparty 字段

---

## 15. MVP 分阶段

### Phase 1（1～2 周）

- PR Newswire + GlobeNewswire RSS
- 关键词 + 材料性打分
- `entities_seed.json` + 简单别名匹配
- 市值：Finnhub 或 Yahoo _chart API（日更）
- 分档 + 推送规则（含 T0↔T0）
- SQLite 入库 + PushPlus
- `/deals` API + 简单页面

### Phase 2

- SEC 8-K 接入
- 轨道 B：小市值公司 PR 扫描
- 标题 NER 增强（可选 spaCy / 正则库）
- 管理后台：别名表 CRUD

### Phase 3

- 盘前加密集轮询
- 与 heatmap 联动：推送时在页面高亮 sector
- 可选：Telegram Bot

---

## 16. 效果预期（校准预期）

| 指标 | MVP 保守估计 |
|------|----------------|
| 真阳性（推了且当天波动明显） | 20%～35% |
| 盘前 15 分钟内收到 | 10%～25% |
| 误报 | 40%～55% |
| 漏报（事后发现没推） | 高，尤其仅轨道 A 时 |

**改进漏报的关键**：轨道 B + 缩短轮询 + 小票自发 PR。

---

## 17. 测试用例（实现后验收）

| # | 输入摘要 | 期望 anchor | 期望 beneficiary | tier_pair |
|---|----------|-------------|------------------|-----------|
| 1 | Anthropic compute agreement with Riot Platforms | Anthropic | RIOT | T0_T2 |
| 2 | CoreWeave partners with … miner | CRWV | T2 ticker | T1_T2 |
| 3 | Oracle OpenAI $300B cloud（若正文仅 OpenAI+Oracle） | ORCL | — | 不推或仅 ORCL 若判 T1（ORCL 为 T0 则不推） |
| 4 | MOU explore partnership AI | — | — | 不推（低分） |
| 5 | MSFT and AMZN cloud deal | 市值较小 T0 | 市值较大 T0 | T0_T0，分数≥70 才推 |

---

## 18. 与 Athena 现有模块关系

| 现有 | 关系 |
|------|------|
| `heatmap.py` | 独立；可选：deals 页展示 beneficiary 所属 sector 涨跌幅 |
| `scheduler.py` | 追加 `deal_monitor` 任务 |
| `database.py` | 追加 Model + `create_all` |
| `config.py` | 追加 DEAL_* 配置 |
| PushPlus / Server酱 | 复用 `service.py` 或同类 notify 函数 |

---

## 19. 实现时在另一 Cursor 项目中的提示词（可复制）

```
请阅读《AI合作快讯监控-设计文档.md》，在 Athena 项目中实现 deal_monitor 模块：
1. 按 §3 目录结构创建 app/deal_monitor/
2. 按 §8 建表并 migrate
3. 实现 Phase 1：RSS 抓取、关键词、entities_seed.json、tiers 分档、材料性打分、7 天去重、PushPlus
4. 推送规则严格按 §2.2，含 T0↔T0 推市值较小者
5. 新增 /deals API 与 deals.html
6. scheduler 每 3 分钟 poll 一次
配置项见 §12，种子数据见 §6
```

---

## 20. 文档变更记录

| 版本 | 日期 | 说明 |
|------|------|------|
| v1.0 | 2026-08-17 | 初版：T0/T1/T2 分档、T0↔T0 推送较小市值、Athena 对齐 |

---

**文件位置**：`/Users/admin/USA/Sandisk/AI合作快讯监控-设计文档.md`  
实现时请复制到 Athena 项目根目录或 `docs/` 下供 Agent 读取。
