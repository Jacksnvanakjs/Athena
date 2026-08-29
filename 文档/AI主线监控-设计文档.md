# AI 主线监控 — 设计文档

> **用途**：在 Athena「AI 产业链合作快讯」页面（`/deals`）增加 **AI 子板块相对强弱 → 当前主线判断** 能力。  
> **版本**：v1.0（2026-08-29）  
> **关联文档**：  
> - [AI合作快讯监控-设计文档.md](./AI合作快讯监控-设计文档.md)  
> - [小公司财报监控-设计文档.md](./小公司财报监控-设计文档.md)  
> - [黄仁勋A档产业动作监控-设计文档.md](./黄仁勋A档产业动作监控-设计文档.md)  
> **原则**：不宣称板块互斥；用 **相对强弱 + 持续天数** 判断主线；优先复用 `app/heatmap.py` 行情，不新建行情源。  
> **落地位置**：`/deals` 新增 Tab「AI 主线」，与「财报日历」「合作 / 黄仁勋」并列。

---

## 0. 给开发者的核心约束

1. **只做相对强弱，不做互斥预言**：主线 = 跑赢 AI 综合基准且排名靠前的子线，不是「A 涨则 B 必跌」。
2. **行情复用 heatmap**：报价拉取、缓存、快照尽量走现有 `get_heatmap_data` / snapshot，禁止平行再写一套爬虫。
3. **AI 专用篮子与全市场 THEMES 分离**：全站热力图可继续展示雪球式大主题；本模块只用 `data/ai_theme_baskets.json` 里的 **AI 子线**（可映射到已有 `THEMES.key`）。
4. **网站默认展示；手机推送默认关闭或极低频**：仅「主线切换且确认」才推（见 §6），避免每日刷屏。
5. **不做自动下单**；页面与推送含免责声明。
6. v1 **仅美股**；涨跌幅时区与 heatmap 一致（美东交易日）。

---

## 1. 项目目标

### 1.1 要解决什么问题

用户需要一眼知道：**当前资金更偏向 AI 哪条链**（算力、光互联、电力、云软件、安全、应用等），以便：

- 解释合作快讯 / 财报暴涨落在哪条叙事上  
- 避免在「应用兑现周」还去追已休息的纯硬件 β  
- 与个股监控互补：主线看方向，事件/财报看标的  

### 1.2 系统目标

1. 每日（及盘中缓存）计算各 AI 子线的 **1D / 5D / 20D** 等权涨跌  
2. 相对 **AI 综合基准** 排名，输出 `current_mainline` / `secondary`  
3. 在 `/deals` 页面 Tab 展示排行榜 + 主线徽章 + 简短解读  
4. （可选）主线切换确认后低频推送  

### 1.3 不做什么（v1）

- 不预测明天主线  
- 不输出买卖点位（买卖窗口留给合作 / 黄仁勋 / 财报模块）  
- 不把非 AI 主题（油气、黄金等）混进主线候选  
- 不替代 `/heatmap` 全市场热力图页（本模块是 AI 子集视图）

---

## 2. AI 子线定义（主线候选池）

### 2.1 默认 10 条子线

写入 `data/ai_theme_baskets.json`（可配置增删）：

| key | 中文名 | 叙事 | 代表成分（示意，以 JSON 为准） | 可映射 heatmap `THEMES.key` |
|-----|--------|------|--------------------------------|------------------------------|
| `gpu_semi` | 算力芯片 | 卖铲核心 | NVDA, AMD, AVGO, MRVL, TSM | `semi` 子集 / `ai_compute` 芯片侧 |
| `optics_cpo` | 光互联/CPO | 机柜互联 | LITE, COHR, AAOI, CIEN, FN, GLW, MTSI | `cpo` |
| `memory` | 存储/HBM 链 | 内存瓶颈 | MU, WDC, STX, PSTG | `storage` |
| `neo_cloud` | GPU 云/租赁 | 算力出租 | CRWV, NBIS, IREN, APLD, CORZ, WULF | `ai_compute` 云侧 |
| `dc_infra` | 数据中心基建 | IDC/机柜 | EQIX, DLR, VRT, ANET, SMCI | `datacenter` |
| `power` | AI 电力 | 电是上限 | VST, CEG, NRG, GEV, OKLO | `dc_power` + 部分 `nuclear` |
| `grid_cooling` | 电网/电气/液冷 | 配电与散热 | ETN, PWR, EMR, GNRC, VRT | `grid` + VRT |
| `cloud_saas` | 云与平台软件 | 大厂平台 | MSFT, ORCL, CRM, NOW, SNOW, AMZN, GOOGL | `cloud_saas` |
| `ai_app` | AI 应用/数据 | 落地应用 | PLTR, DDOG, MDB, PATH, AI, ADBE | `ai_software` |
| `ai_sec` | AI 安全/身份 | Agent 配套 | CRWD, OKTA, ZS, PANW, FTNT, S | `cybersecurity` |

**可选 v1.1 扩展**（默认关闭）：`materials`（AXTI, GLW）、`network`（ANET, CSCO）、`eda`（SNPS, CDNS）。

### 2.2 JSON 结构

```json
{
  "version": 1,
  "benchmark_key": "ai_bench",
  "themes": [
    {
      "key": "optics_cpo",
      "name": "光互联/CPO",
      "heatmap_theme_key": "cpo",
      "enabled": true,
      "tickers": [
        {"symbol": "LITE", "name": "Lumentum", "weight": 1.0},
        {"symbol": "COHR", "name": "Coherent", "weight": 1.0}
      ]
    }
  ]
}
```

- `weight` 默认 1.0（等权）；预留市值加权  
- `heatmap_theme_key`：若与现有 THEMES 完全一致，可直接复用其 tickers，减少双维护；不一致则以本 JSON 为准  

### 2.3 AI 综合基准 `ai_bench`

```
ai_bench = 所有 enabled 子线的成分股去重后等权平均涨跌
```

用途：

- 子线 `rel_1d = theme_1d - bench_1d`  
- 主线必须满足 `rel_5d > 0`（默认），避免「跌得少」被误认为主线  

---

## 3. 主线判定算法

### 3.1 每个子线计算字段

| 字段 | 含义 |
|------|------|
| `ret_1d` | 成分等权日涨跌（%），缺行情的成分剔除后再平均 |
| `ret_5d` | 近 5 个交易日累计（或收盘快照累加） |
| `ret_20d` | 近 20 交易日 |
| `breadth` | 上涨家数 / 有效家数 |
| `rel_1d` / `rel_5d` / `rel_20d` | 相对 `ai_bench` |
| `rank_5d` | 按 `rel_5d` 降序排名（主排序） |
| `n_valid` | 有效报价成分数；`< 3` 则该子线不参与主线竞选 |

**报价规则**：与 heatmap 一致；某 symbol 失败则跳过，不把 0 当真实涨跌。

### 3.2 主线确认规则（默认）

```
候选 = enabled 且 n_valid ≥ 3 的子线

primary（当前主线）：
  1. 按 rel_5d 降序取 Top1
  2. 且 rel_5d ≥ MAINLINE_MIN_REL_5D（默认 +1.0 个百分点）
  3. 且 breadth ≥ MAINLINE_MIN_BREADTH（默认 0.55）
  4. 且连续 CONFIRM_DAYS 个交易日（默认 3）满足「进入 Top2 且 rel_5d>0」
     → status = confirmed
  若不满足 4 → status = emerging（新兴主线，页面可显示但标「未确认」）

secondary（观察）：
  Top2（若 rel_5d>0），否则 null

no_mainline：
  - 全市场/AI 基准单日大跌且子线离散度低（σ(ret_1d) < 阈值）
  - 或没有任何子线 rel_5d ≥ 门槛
  → 文案：「无明确主线（宏观/共振下跌或普涨）」
```

### 3.3 配置项

```python
AI_MAINLINE_ENABLED = True
AI_MAINLINE_CONFIRM_DAYS = 3          # 确认主线所需连续交易日
AI_MAINLINE_MIN_REL_5D = 1.0          # 相对基准至少 +1pct
AI_MAINLINE_MIN_BREADTH = 0.55
AI_MAINLINE_MIN_VALID = 3
AI_MAINLINE_PUSH_ENABLED = False      # v1 默认关；确认切换后再开
AI_MAINLINE_PUSH_COOLDOWN_DAYS = 5    # 两次主线切换推送最小间隔
```

### 3.4 解读文案模板（自动生成）

```
当前主线：{name}（confirmed｜emerging）
近5日相对 AI 基准：{rel_5d:+.1f}%｜板块 {ret_5d:+.1f}%｜上涨占比 {breadth:.0%}
次强：{secondary_name 或「无」}
说明：相对强弱判断，非互斥；不构成投资建议。
```

校准例（逻辑自检，非写死数据）：

- 2026-08 末财报周：若 `ai_sec`、`cloud_saas` 的 `rel_5d` 明显高于 `gpu_semi` → 主线应为 **安全/云软件**，与 OKTA/CRWD/CRM 行情一致。

---

## 4. 系统架构（对齐 Athena）

```
app/
  ai_mainline/
    __init__.py
    config.py           # 或从 app.config 读取
    baskets.py          # 加载 data/ai_theme_baskets.json
    metrics.py          # 等权收益、相对基准、breadth
    ranking.py          # 排名、confirmed/emerging/no_mainline
    persistence.py      # 每日主线快照（可选表）
    pipeline.py         # 拉行情 → 算指标 → 存快照 →（可选）推送
    push.py             # 主线切换文案
  heatmap.py            # 复用报价；可 export 按 symbols 取涨跌的 helper
  routes.py             # /api/ai-mainline*
  scheduler.py          # 美东收盘后写日快照；盘中可读 heatmap 缓存
templates/
  deals.html            # 新增 Tab「AI 主线」
data/
  ai_theme_baskets.json
```

### 4.1 数据流

```
盘中（用户打开 /deals 或 API）
  → 复用 heatmap 报价缓存（TTL 同现有 ~120s）
  → metrics + ranking（内存计算）
  → 返回 JSON 给前端

美东收盘后（建议 16:35 ET，可紧跟 heatmap snapshot）
  → 写入 AiMainlineDailySnapshot（每子线一行 + 一行 meta）
  → 更新「连续 Top2 天数」用于 confirmed
  → 若主线 key 相对上次 confirmed 发生变化且 PUSH 开启 → 推送
```

### 4.2 与 heatmap 的接口约定

在 `heatmap.py` 增加或复用：

```python
async def get_quotes_for_symbols(symbols: list[str]) -> dict[str, dict]:
    """返回 {sym: {chg_pct, price, ...}}，内部走现有多源行情。"""
```

`ai_mainline` **只消费** 该函数 + 历史 snapshot 表算 5D/20D；若盘中无历史，5D/20D 用 snapshot 累加，1D 用实时。

---

## 5. 数据模型

### 5.1 `AiMainlineDailySnapshot`

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | PK | |
| `trade_date` | date | 美东交易日 |
| `theme_key` | str | 子线 key；特殊值 `_meta` 存主线结论 |
| `ret_1d` / `ret_5d` / `ret_20d` | float | |
| `rel_1d` / `rel_5d` / `rel_20d` | float | |
| `breadth` | float | |
| `rank_5d` | int\|null | |
| `n_valid` | int | |
| `payload_json` | text | `_meta` 时存 primary/secondary/status 等 |

唯一约束：`(trade_date, theme_key)`。

### 5.2 `_meta.payload_json` 示例

```json
{
  "primary_key": "ai_sec",
  "primary_name": "AI 安全/身份",
  "status": "confirmed",
  "secondary_key": "cloud_saas",
  "bench_ret_5d": 1.2,
  "streak_days": 3,
  "summary": "当前主线：AI 安全/身份…"
}
```

---

## 6. 手机推送（默认关闭）

| 规则 | 说明 |
|------|------|
| 触发 | `status` 从非本 key 的 confirmed → 新 `primary_key` confirmed |
| 冷却 | `AI_MAINLINE_PUSH_COOLDOWN_DAYS`（默认 5）内不重复推 |
| 不推 | `emerging`、`no_mainline`、仅 1D 闪崩闪涨 |
| 标题 | `【AI 主线切换】{旧} → {新}` |
| 正文 | summary + Top3 子线 rel_5d + 免责声明 |

v1 建议 **先只做网站**；推送开关默认 `false`。

---

## 7. 网站展示（`/deals` Tab「AI 主线」）

### 7.1 Tab 结构（与现有对齐）

```
[ AI 主线 ]  [ 财报日历 ]  [ 合作 / 黄仁勋 ]
```

或：

```
[ 财报日历 ]  [ 合作 / 黄仁勋 ]  [ AI 主线 ]
```

顺序可按产品偏好；本模块不依赖另外两个 Tab 的数据。

### 7.2 页头主线卡（必须）

```
┌─────────────────────────────────────────────┐
│ 当前主线：AI 安全/身份          ● 已确认    │
│ 近5日 +8.2%｜相对基准 +5.1%｜上涨 5/6       │
│ 次强：云与平台软件                          │
│ 更新：美东 2026-08-28 收盘 / 盘中缓存 12:01 │
└─────────────────────────────────────────────┘
```

`emerging` 用橙色「观察中」；`no_mainline` 灰色「暂无明确主线」。

### 7.3 排行表格列

| 列 | 内容 |
|----|------|
| 排名 | rank_5d |
| 子线 | 中文名 |
| 1D | ret_1d |
| 5D | ret_5d |
| 20D | ret_20d |
| 相对5D | rel_5d（主排序依据，高亮） |
| 广度 | breadth |
| 状态 | 主线 / 次强 / — |
| 成分预览 | 前 3 个 ticker，可点开 |

颜色：rel_5d > 0 绿，< 0 红；当前主线整行左边框高亮（类似现有 `.row-nvda`）。

### 7.4 Info 文案（写入 info-box 或 Tab 内提示）

> AI 主线用各子板块 **相对 AI 综合基准的 5 日强弱** 判断资金在哪条链，**不是**软硬件互斥。  
> 「已确认」需连续约 3 个交易日排名靠前。仅供研究，非投资建议。

### 7.5 与另两 Tab 的联动（v1 可选，建议做轻量）

- 主线卡下增加一行：「相关监控：财报日历里本赛道标的」「合作快讯筛选」  
- 例如主线 `optics_cpo` → 链到财报 Tab 并预填 sector（若 sector 枚举可对齐）  

v1 不做强耦合也可以，先独立 Tab。

---

## 8. API 设计

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/ai-mainline` | 当前排名 + primary/secondary/status + summary |
| GET | `/api/ai-mainline/history` | `?days=30` 每日 primary_key 序列 |
| POST | `/api/ai-mainline/run` | 强制重算（需 token；调试用） |

### 8.1 `GET /api/ai-mainline` 响应示例

```json
{
  "as_of": "2026-08-28T16:00:00-04:00",
  "source": "heatmap_cache+snapshots",
  "bench": {"key": "ai_bench", "ret_1d": 0.4, "ret_5d": 1.2},
  "primary": {
    "key": "ai_sec",
    "name": "AI 安全/身份",
    "status": "confirmed",
    "streak_days": 3,
    "ret_5d": 8.2,
    "rel_5d": 5.1,
    "breadth": 0.83
  },
  "secondary": {
    "key": "cloud_saas",
    "name": "云与平台软件",
    "ret_5d": 4.0,
    "rel_5d": 2.8
  },
  "summary": "当前主线：AI 安全/身份…",
  "themes": [
    {
      "key": "ai_sec",
      "name": "AI 安全/身份",
      "rank_5d": 1,
      "ret_1d": 1.2,
      "ret_5d": 8.2,
      "ret_20d": 15.0,
      "rel_5d": 5.1,
      "breadth": 0.83,
      "n_valid": 6,
      "leaders": ["OKTA", "CRWD", "ZS"]
    }
  ]
}
```

---

## 9. 调度

| Job | 触发 | 作用 |
|-----|------|------|
| 复用 heatmap 缓存 | 用户请求时 | 盘中 1D |
| `ai_mainline_daily` | 美东 16:35（heatmap snapshot 之后） | 写日快照、更新 streak、可选推送 |

可与 `scheduled_heatmap_snapshot` 串在同一协程末尾调用 `run_ai_mainline_daily()`，减少调度条目。

---

## 10. 测试用例（验收）

| # | 场景 | 期望 |
|---|------|------|
| T1 | 所有子线行情正常 | 返回 10 行 themes + bench；有 rank_5d |
| T2 | `ai_sec` 连续 3 日 rel_5d Top1 | `primary.status=confirmed` |
| T3 | 仅今日冲上 Top1 | `emerging`，不推送 |
| T4 | 全线大跌、rel 均接近 0 | `no_mainline` 或 primary 为空 |
| T5 | 某子线仅 1 个 ticker 有行情 | 该子线不参与竞选 |
| T6 | `/deals` Tab 切换 | 主线卡 + 表格渲染；与合作列表互不干扰 |
| T7 | PUSH 关闭时主线切换 | 不发手机；网站更新 |
| T8 | 成分与 heatmap `cpo` 对齐 | optics 涨跌与热力图 CPO 主题方向一致（允许子集差异） |

---

## 11. 实施顺序（建议）

1. `data/ai_theme_baskets.json` + `baskets.py` / `metrics.py` / `ranking.py`  
2. `GET /api/ai-mainline`（可先只做 1D+用 snapshot 拼 5D）  
3. `/deals` 增加 Tab UI（主线卡 + 表）  
4. 收盘日快照 + streak → confirmed  
5. （可选）推送开关  

预计可先用 heatmap 现成成分快速对齐：`cpo`、`cybersecurity`、`cloud_saas`、`ai_software`、`dc_power`、`storage`、`ai_compute` 拆分。

---

## 12. 与对话结论对齐

| 结论 | 产品落地 |
|------|----------|
| 板块无稳定互斥 | UI 明确写「相对强弱，非互斥」 |
| 软硬件只是轮动一种 | 10 子线覆盖算力/光/电/云/应用/安全等 |
| 可判断当前主线 | `primary` + `confirmed` 连续天数 |
| 落在合作快讯页 | `/deals` 新 Tab，不新建站点 |

---

## 13. 版本记录

| 版本 | 日期 | 变更 |
|------|------|------|
| v1.0 | 2026-08-29 | 首版：10 条子线、相对基准主线算法、复用 heatmap、/deals Tab、可选低频推送 |

---

*本文档为产品/工程设计，不构成投资建议。*
