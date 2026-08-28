# 黄仁勋 / 英伟达 A 档产业动作监控 — 设计文档

> **用途**：在 Athena 网站项目中实现「英伟达实质性产业动作 → 解析受益标的 → 买卖窗口提示 → 推送」功能。  
> **版本**：v1.1（2026-08-28）  
> **原则**：监控 **A 档**（投资 / 合作 / 产能锁定）与 **A_PLUS_B 档**（先 A 后口头催化）；**不监控**纯 B/C 档。  
> **默认策略**：**尽早买 + 次日收盘卖**（`EARLIEST_BUY_T1_CLOSE`）。  
> **关联文档**：[AI合作快讯监控-设计文档.md](./AI合作快讯监控-设计文档.md)（市值分档 T0/T1/T2、推送架构可复用）

---

## 0. 给开发者的核心约束

1. **入库并推送 `signal_tier ∈ {A, A_PLUS_B}`**；纯 B/C 档只记日志、不推送。
2. **锚点固定为 NVDA**；受益方为被投资 / 被合作 / 被锁产能 / 被口头站台的美股 ticker。
3. **推送必须带买卖窗口**：`buy_window`、`sell_window`、`strategy`（默认 `EARLIEST_BUY_T1_CLOSE`）。
4. **A 档**：同一受益 ticker + 同一 `action_type`，**7 天内去重**。
5. **A_PLUS_B 档**：同一 ticker **14 天内去重**（口头催化频率低但易重复炒作）。
6. v1 **仅美股**；韩股 ADR 默认关闭。
7. **不做自动下单**；页面与推送必须含免责声明。

---

## 1. 项目目标

### 1.1 要解决什么问题

历史统计（§7）：

| 档位 | 次日收涨概率 | 默认策略下预期收益 | 持有 20 日胜率 |
|------|-------------|-------------------|----------------|
| **A** | ~85%–90% | **+8%～+15%**（尽早买） | ~65% |
| **A_PLUS_B** | ~70%–80% | **+5%～+12%**（情绪脉冲） | ~45%–50% |
| **纯 B** | ~50% | 不监控 | 不推荐 |

系统目标：

1. **尽早发现** NVDA 官网 / SEC / 新闻稿中的 A 档动作
2. **识别 A_PLUS_B**：90 天内有过 A 档的标的，再出现口头站台类催化
3. **输出默认买卖窗口**：尽早买 → **次日收盘卖**
4. 去重推送 + `/nvda-signals` 页面 + API

### 1.2 信号分档总览

| 档位 | 定义 | 是否推送 |
|------|------|----------|
| **A** | 投资 / 采购承诺 / 产能锁定 / 战略合作协议（有合同或金额） | ✅ 主推 |
| **A_PLUS_B** | **90 天内已有 confirmed A** + 本次为口头站台（无新合同/金额） | ✅ 二次催化，规则更严 |
| **B** | 纯口头，**90 天内无 A** | ❌ 仅日志 |
| **C** | 饭局、行程、炸鸡股等 | ❌ 仅日志 |

---

## 2. A 档信号定义

### 2.1 动作类型（`action_type` 枚举，仅 A 档）

| 代码 | 名称 | 必须满足的证据 | 材料性底线 |
|------|------|----------------|------------|
| `NVDA_INVEST` | 股权投资 | NVDA 官宣投资金额或持股；8-K / 新闻稿 | **≥ 70** |
| `NVDA_PURCHASE_COMMIT` | 多年采购承诺 | 明确多年 purchase commitment / offtake / 金额或 GW | **≥ 75** |
| `NVDA_CAPACITY_LOCK` | 产能锁定 | capacity rights / slot reservation / 建厂资助 | **≥ 75** |
| `NVDA_STRATEGIC_PARTNER` | 战略合作协议 | 多年期 strategic partnership + **具体业务** | **≥ 65** |
| `NVDA_SUPPLY_LT` | 长期供应协议 | long-term supply agreement，≥2 年 | **≥ 70** |

**A 档入库条件（同时满足）**：

- `signal_tier == 'A'`
- `materiality_score >= action_type` 对应底线
- 受益方可映射到美股 ticker
- 信息源：NVDA 官方、SEC、双方 PR、Reuters/Bloomberg 双方确认
- 非传闻（无官方跟进 → `status=rumor`，不推送）

### 2.2 A 档关键词（中英）

**正向**（需同时出现 NVDA/NVIDIA）：

```
invest, investment, strategic partnership, multi-year, purchase commitment,
offtake, capacity, pre-pay, prepayment, supply agreement, long-term agreement,
equity stake, warrants, collaboration agreement, co-develop, joint development,
fabrication facility, manufacturing, allocation, reserved capacity, slot reservation,
$ billion, billion-dollar, 亿美元, 战略合作, 长期协议, 产能, 投资
```

**负向**（无 90 天内 A 记录 → 降级 B；有 A 记录 → 见 §3 A_PLUS_B）：

```
dinner, lunch, meal, restaurant, 饭局, 炸鸡, 烤五花肉,
"buy the stock", "buy their stock", 打折买入, trillion-dollar company, 万亿,
according to people familiar, reportedly in talks, 据悉, 传闻, 或将, 拟
```

### 2.3 信息源优先级

| 优先级 | 来源 | 权重 |
|--------|------|------|
| P0 | `nvidia.com/newsroom`、SEC EDGAR 8-K | 1.0 |
| P1 | PR Newswire / GlobeNewswire 联合稿 | 0.95 |
| P2 | Reuters / Bloomberg（双方确认） | 0.85 |
| P3 | 合作方 IR | 0.80 |
| **不单独触发** | 富途/老虎/自媒体转载 | — |

---

## 3. A_PLUS_B 档完整规则

### 3.1 定义

**A_PLUS_B** = 同一 `beneficiary_ticker` 在 **过去 90 个自然日内** 存在一条 `status=confirmed` 且 `signal_tier=A` 的记录，且本次新事件满足：

| 条件 | 要求 |
|------|------|
| 新事件类型 | 口头站台 / 预测 / 喊单类（原 B 档语义） |
| 无新硬条款 | **无** 新投资金额、无新多年采购承诺、无新产能锁定合同 |
| 发言人 | Jensen Huang 以 NVDA CEO 身份，或 NVDA 官方场合（GTC、Computex、财报会、联合发布会） |
| 受益方可交易 | 美股 ticker，非 T0 |

**典型正例**：MRVL — 2026/3 NVDA 投资 $2B（**A**）→ 2026/6 Computex「下一个万亿公司」（**A_PLUS_B**）

**典型反例（不升格）**：

- 90 天内无 A 记录的「万亿市值」喊话 → **纯 B，不推送**
- 饭局、炸鸡、行程猜测 → **C，不推送**
- 口头话 + **同时宣布新投资** → 按 **新 A 档** 处理，非 A_PLUS_B

### 3.2 A_PLUS_B 动作类型（`action_type` 枚举）

| 代码 | 名称 | 触发语义示例 |
|------|------|-------------|
| `NVDA_VERBAL_BULLISH` | 口头看好 | 「下一个万亿公司」「essential」「doing so well」 |
| `NVDA_VERBAL_BUY` | 口头荐股 | 「buy their stock」「买他们的股票」 |
| `NVDA_VERBAL_DEMAND` | 口头需求 | 「please make more」「多生产」（无新供应合同） |

### 3.3 A_PLUS_B 入库条件

- `signal_tier == 'A_PLUS_B'`
- `prior_a_event_id` 非空（关联 90 天内最近一条 A 记录）
- `prior_a_days_ago <= 90`
- `materiality_score >= 55`（低于 A 档，因无硬合同）
- `beneficiary_role` 必须为 **direct**（口头话几乎只点名校名，不推间接）
- 非 C 档场景（饭局/行程/memes）

### 3.4 A_PLUS_B 材料性评分（满分 100，底线 55）

| 维度 | 权重 | 加分 |
|------|------|------|
| **前期 A 档强度** | 30 | 前次 A 材料性 ≥85 = 30；70–84 = 22；<70 = 15 |
| **前期 A 时效** | 20 | ≤30 天 = 20；31–60 天 = 15；61–90 天 = 10 |
| **口头明确度** | 25 | 直接点名 ticker/CEO 同台 = 25；间接提及赛道 = 12 |
| **市场起点** | 15 | 30 日涨幅 <10% = 15；10%–20% = 8；>20% = 0 |
| **来源权重** | 10 | P0 场合 = 10；P2 媒体报道 = 6 |

**推送门槛**：

- `materiality >= 55` 且 `confidence >= 65` → 网站列表
- `confidence >= 72` → 微信推送（低于 A 档的 80）

### 3.5 A_PLUS_B 与纯 B 的判定流程（`classifier.py`）

```
新消息进入
    ↓
是否含 NVDA + 受益 ticker？
    ↓ 否 → 丢弃
是否含 A 档硬条款（金额/多年合同/产能）？
    ↓ 是 → signal_tier = A（§2）
是否含 B 档口头关键词（§2.2 负向词中的站台类）？
    ↓ 否 → 丢弃或 C 档日志
查 nvda_signal_events：该 ticker 90 天内是否有 confirmed A？
    ↓ 否 → signal_tier = B，仅日志，不推送
    ↓ 是 → signal_tier = A_PLUS_B（§3）
是否含 C 档词（饭局/炸鸡/行程）？
    ↓ 是 → signal_tier = C，仅日志
```

---

## 4. 买卖窗口策略（默认：尽早买 + 次日收盘卖）

> 策略代码：`EARLIEST_BUY_T1_CLOSE`  
> 含义：在**可交易最早时点**买入，在**买入日后的下一个交易日收盘**全部卖出。  
> 此为 v1.1 **唯一默认**；旧版分批止盈（TP1/TP2/TP3）移至 §4.4 可选。

### 4.1 策略说明（A 档）

| 消息发布时间 | 买入窗口（尽早） | 卖出窗口 |
|--------------|------------------|----------|
| **盘后 / 盘前**（最常见） | ① 消息当日 **盘后**；或 ② **次日盘前**；或 ③ 次日 **09:35–10:00 ET** 开盘段（三者取你实际能成交的最早时刻） | 买入日之后的 **第一个完整交易日 16:00 ET 收盘** 清仓 |
| **盘中** | 消息确认后 **30 分钟内**（若当日已涨 <8%） | **次日收盘** 清仓 |

**示例（盘后消息，周二 16:30 发布）**：

```
周二 16:30  消息发布
周二 17:00  盘后买入（首选）          ← 「消息当天买」
周三 16:00  收盘卖出                  ← 「次日收盘卖」
```

**示例（无法盘后，周三开盘买）**：

```
周三 09:35  开盘买入
周四 16:00  收盘卖出                  ← 持有约 1.5 个交易日
```

**统计口径对齐**：

- 文档历史涨幅 **+11%～+22%** = 消息日收盘 → 次日收盘（理论满额，需消息日前已持仓）
- 散户「尽早买 + 次日收盘卖」实盘预期：**+8%～+15%**（直接受益）

### 4.2 买入过滤（A 档）

| 条件 | 动作 |
|------|------|
| 预估跳空 **≥ 15%**（相对消息日前收盘） | `buy_ok=false`，`chase_risk=high` |
| 信号日前 30 日涨幅 **> 25%** | `buy_ok=false` |
| 信号日前 10 日涨幅 **> 15%** | `buy_ok=caution`，建议仓位 **50%** |
| 日均成交额 < $20M（T2: $10M） | 不推 |
| 盘中消息且当日已涨 **≥ 8%** | 不追，仅观察 |

### 4.3 策略说明（A_PLUS_B 档）— 更短、更严

| 维度 | A 档 | A_PLUS_B 档 |
|------|------|-------------|
| **策略代码** | `EARLIEST_BUY_T1_CLOSE` | `INTRADAY_FAST_EXIT` |
| **买入** | 盘后/盘前/次日早盘 | **仅盘中**；消息发布后 30 分钟内；当日涨幅 **< 10%** |
| **卖出** | 买入后 **次日收盘** | **优先当日收盘**；若来不及则 **次日开盘 30 分钟内** 清仓 |
| **跳空阈值** | 15% 不追 | **10%** 不追 |
| **建议仓位** | 100% | **≤ 50%** |
| **止损** | 可选 -8%（盘中跌破） | **-5%** 硬止损 |
| **预期收益** | +8%～+15% | +5%～+12%（情绪脉冲，快进快出） |

**MRVL 校准（A_PLUS_B）**：

- 2026/6/2 口头「万亿」：若按 A_PLUS_B → 当日或次日早盘卖出，可避开 7 月 **-50%** 回撤
- 若误按 A 档持有到 T+20 → 大幅回吐

### 4.4 预期收益与胜率（写入推送）

| 档位 | 策略 | 胜率（默认策略下） | 预期收益 | 备注 |
|------|------|-------------------|----------|------|
| **A** | 尽早买 + 次日收盘卖 | **~85%–90%** | **+8%～+15%** | 主策略 |
| **A_PLUS_B** | 盘中买 + 当日/次日早盘卖 | **~70%–80%** | **+5%～+12%** | 仓位减半 |
| **追高（跳空≥15%）** | — | **~45%** | 均值转负 | 不推买入 |

**推送免责声明（固定文案）**：

```
历史统计：A档 n=6，尽早买+次日收盘卖胜率约85%；A_PLUS_B为情绪催化，快进快出。
非投资建议。跳空过大不追。
```

### 4.5 可选进阶策略（默认关闭）

环境变量 `NVDA_SIGNAL_STRATEGY=LEGACY_BATCH` 时启用旧版分批：

| 阶段 | T+5 卖 50% | T+10 卖 25% | T+20 清仓 | 止损 -8% |

**默认不启用**；UI 上可标注「进阶模式」。

---

## 5. 硬性过滤

### 5.1 A 档

| # | 条件 | 结果 |
|---|------|------|
| F1 | `signal_tier != A` | 不走 A 流程 |
| F2 | 材料性低于 §2.1 底线 | 不推送 |
| F3 | `status == rumor` | 不推送 |
| F4 | T0 受益方 | 不推送 |
| F5 | 30 日涨幅 > 25% | `buy_ok=false` |
| F6 | 跳空 ≥ 15% | `buy_ok=false` |
| F7 | 流动性不足 | 不推送 |
| F8 | 7 日内同 ticker + action_type 重复 | 去重 |

### 5.2 A_PLUS_B 档

| # | 条件 | 结果 |
|---|------|------|
| P1 | 90 天内无 confirmed A | 降级纯 B，不推送 |
| P2 | 材料性 < 55 | 不推送 |
| P3 | `beneficiary_role != direct` | 不推送 |
| P4 | 含 C 档词（饭局等） | 不推送 |
| P5 | 当日已涨 ≥ 10% | `buy_ok=false` |
| P6 | 跳空 ≥ 10% | `buy_ok=false` |
| P7 | 14 日内同 ticker 已有 A_PLUS_B | 去重 |
| P8 | 前次 A 为 `rumor` 或已 `expired` | 不升格 A_PLUS_B |

---

## 6. 受益标的识别（复用 v1.0）

间接受益、市值分档规则不变，见 v1.0 §3。**A_PLUS_B 仅推 direct，不推 indirect。**

---

## 7. 历史校准案例

| # | 日期 | 档位 | 标的 | 统计涨幅（消息日→次日） | 默认策略实盘预期 | 若持有更久 |
|---|------|------|------|------------------------|------------------|------------|
| 1 | 2026-03-02 | **A** | LITE | +11.8% | 尽早买 + 次日卖 **~+10%** | 年内极高（不作短线预期） |
| 2 | 2026-03-02 | **A** | COHR | +15.4% | **~+12%** | 同上 |
| 3 | 2026-03-02 | **A** 间接 | AAOI | +22.4% | **~+15%** | 波动大 |
| 4 | 2026-03 | **A** | MRVL | +13% | **~+10%** | — |
| 5 | 2026-06-02 | **A_PLUS_B** | MRVL | +32.5% | 盘中追风险高；**当日/次日早盘卖**可锁利润 | 7 月 **-50%** |
| 6 | 2026-06-08 | **A** | SK 海力士 | — | ADR 默认关闭 | — |

**默认 config**：

```python
NVDA_SIGNAL_DEFAULTS = {
    "strategy": "EARLIEST_BUY_T1_CLOSE",   # A 档默认
    "strategy_a_plus_b": "INTRADAY_FAST_EXIT",
    "a_next_day_win_rate": 0.88,
    "a_expected_gain_low": 0.08,
    "a_expected_gain_high": 0.15,
    "a_plus_b_win_rate": 0.75,
    "a_plus_b_expected_gain_low": 0.05,
    "a_plus_b_expected_gain_high": 0.12,
    "a_chase_gap_threshold": 0.15,
    "a_plus_b_chase_gap_threshold": 0.10,
    "a_plus_b_intraday_max_gain": 0.10,
    "a_prior_a_lookback_days": 90,
    "a_plus_b_position_pct": 0.50,
    "a_pre_signal_30d_max": 0.25,
    "a_plus_b_stop_loss": -0.05,
}
```

---

## 8. 系统架构

### 8.1 模块路径

```
app/nvda_signal/
  classifier.py       # A / A_PLUS_B / B / C 四档判定（§3.5）
  prior_a_lookup.py   # 90 天内 A 记录查询
  trade_window.py     # EARLIEST_BUY_T1_CLOSE / INTRADAY_FAST_EXIT
  ...
```

### 8.2 数据流

```
NVDA newsroom / SEC / PR
        ↓
  classifier: A | A_PLUS_B | B | C
        ↓ B/C → 日志
  prior_a_lookup（仅 B 类口头词时）
        ↓
  materiality + §5 过滤
        ↓
  trade_window（按档位选策略）
        ↓
  去重 → 入库 → 推送
```

---

## 9. 数据库 Schema

### 9.1 表：`nvda_signal_events`（v1.1 增补字段）

| 字段 | 类型 | 说明 |
|------|------|------|
| `signal_tier` | varchar | **`A` / `A_PLUS_B`**（推送）；B/C 不入此表或入 `nvda_signal_log` |
| `strategy` | varchar | `EARLIEST_BUY_T1_CLOSE` / `INTRADAY_FAST_EXIT` |
| `prior_a_event_id` | UUID | 可空；A_PLUS_B 必填 |
| `prior_a_days_ago` | int | 可空 |
| `position_pct` | float | 建议仓位；A=1.0，A_PLUS_B=0.5 |
| `sell_window` | text | 如 `next_session_close` / `same_day_close` |
| ... | | 其余字段同 v1.0 |

### 9.2 `sell_plan_json` 示例（A 档默认）

```json
{
  "strategy": "EARLIEST_BUY_T1_CLOSE",
  "buy_windows": ["after_hours", "pre_market", "next_open_0935_1000"],
  "sell_at": "next_trading_day_close",
  "sell_label": "次日收盘全部卖出",
  "no_chase_if_gap_pct": 15,
  "optional_stop_loss_pct": -8,
  "position_pct": 1.0
}
```

### 9.3 `sell_plan_json` 示例（A_PLUS_B）

```json
{
  "strategy": "INTRADAY_FAST_EXIT",
  "buy_windows": ["intraday_within_30min"],
  "buy_conditions": {"max_intraday_gain_pct": 10},
  "sell_at": "same_day_close_preferred",
  "sell_fallback": "next_open_first_30min",
  "sell_label": "当日收盘优先，否则次日早盘清仓",
  "no_chase_if_gap_pct": 10,
  "stop_loss_pct": -5,
  "position_pct": 0.5
}
```

---

## 10. API 设计

### 10.1 `GET /api/nvda-signals`

新增查询参数：

| 参数 | 说明 |
|------|------|
| `signal_tier` | `A` / `A_PLUS_B` / 空=全部 |

响应增补：

```json
{
  "signal_tier": "A",
  "strategy": "EARLIEST_BUY_T1_CLOSE",
  "buy_window": "after_hours | pre_market | 2026-03-03 09:35-10:00 ET",
  "sell_window": "2026-03-04 16:00 ET (next close)",
  "position_pct": 1.0,
  "prior_a_event_id": null
}
```

```json
{
  "signal_tier": "A_PLUS_B",
  "strategy": "INTRADAY_FAST_EXIT",
  "buy_window": "intraday within 30min, day_gain<10%",
  "sell_window": "same_day_close | next_open_30min",
  "position_pct": 0.5,
  "prior_a_event_id": "uuid-of-mrvl-march-a",
  "prior_a_days_ago": 92
}
```

### 10.2 页面 `/nvda-signals`

- **A 档**：绿色边框，标签 `A · 尽早买/次日收盘卖`
- **A_PLUS_B**：橙色边框，标签 `A+B · 二次催化 · 半仓快进快出`
- **chase_risk=high**：灰色「仅观察」

---

## 11. 推送模板

### 11.1 A 档（`confidence >= 80`）

```
【NVDA A档】LITE 直接受益
类型：投资+采购承诺 | 材料性 92
策略：尽早买 → 次日收盘卖
买入：盘后/盘前/明日09:35-10:00（跳空<15%）
卖出：买入后第1个交易日收盘清仓
预期：+8%～+15% | 胜率约88%
⚠️ 非投资建议
{url}
```

### 11.2 A_PLUS_B 档（`confidence >= 72`）

```
【NVDA A+B】MRVL 二次催化
前次A：92天前 投资$2B | 本次：口头看好
策略：盘中买 → 当日收盘或次日早盘卖 | 仓位50%
条件：当日涨幅<10%，跳空<10%
预期：+5%～+12% | 快进快出，不隔夜博趋势
⚠️ 非投资建议
{url}
```

### 11.3 仅观察

```
【NVDA A档·观察】已跳空18%，不建议追
【NVDA A+B·观察】当日已涨12%，错过入场窗口
```

---

## 12. 定时任务

| 任务 | 频率 | 说明 |
|------|------|------|
| `poll_nvidia_newsroom` | 3 分钟 | P0 |
| `poll_sec_nvda_8k` | 5 分钟 | P0 |
| `poll_pr_wire_nvda` | 10 分钟 | P1 |
| `refresh_beneficiary_metrics` | 1 小时 | 涨幅、成交额、gap |
| `recompute_buy_ok` | 08:00 ET | 盘前更新 buy_ok |
| `expire_old_signals` | 00:00 ET | >30 天 → expired |
| **`prune_prior_a_index`** | 每天 | 清理 >90 天 A 索引（A_PLUS_B 查表用） |

---

## 13. 配置项

```python
NVDA_SIGNAL_ENABLED = True
NVDA_SIGNAL_STRATEGY = "EARLIEST_BUY_T1_CLOSE"   # 或 LEGACY_BATCH
NVDA_SIGNAL_A_PLUS_B_ENABLED = True
NVDA_SIGNAL_PRIOR_A_LOOKBACK_DAYS = 90
NVDA_SIGNAL_A_DEDUP_DAYS = 7
NVDA_SIGNAL_A_PLUS_B_DEDUP_DAYS = 14
NVDA_SIGNAL_MIN_MATERIALITY_A = 70
NVDA_SIGNAL_MIN_MATERIALITY_A_PLUS_B = 55
NVDA_SIGNAL_PUSH_MIN_CONFIDENCE_A = 80
NVDA_SIGNAL_PUSH_MIN_CONFIDENCE_A_PLUS_B = 72
NVDA_SIGNAL_CHASE_GAP_THRESHOLD_A = 0.15
NVDA_SIGNAL_CHASE_GAP_THRESHOLD_A_PLUS_B = 0.10
NVDA_SIGNAL_A_PLUS_B_POSITION_PCT = 0.50
NVDA_SIGNAL_A_PLUS_B_INTRADAY_MAX_GAIN = 0.10
NVDA_SIGNAL_A_PLUS_B_STOP_LOSS = -0.05
```

---

## 14. 测试用例

| 用例 | 输入 | 期望 |
|------|------|------|
| T1 | NVDA $2B 投资 LITE | `tier=A`, `strategy=EARLIEST_BUY_T1_CLOSE`, 推送 |
| T2 | MRVL「万亿」且 90 天内无 A | `tier=B`, **不推送** |
| T3 | MRVL「万亿」且 3 月有 A | `tier=A_PLUS_B`, `position_pct=0.5`, 推送 |
| T4 | 首尔饭局 | `tier=C`, 不推送 |
| T5 | A 档盘后，跳空 18% | `buy_ok=false` |
| T6 | A_PLUS_B，当日已涨 12% | `buy_ok=false` |
| T7 | A_PLUS_B 间接标的 AAOI | **不推送** |
| T8 | 口头话 + 同时 $500M 新投资 | `tier=A`（新 A，非 A_PLUS_B） |
| T9 | sell_plan | A → `next_trading_day_close` |
| T10 | sell_plan | A_PLUS_B → `same_day_close` preferred |

---

## 15. 版本记录

| 版本 | 日期 | 变更 |
|------|------|------|
| v1.0 | 2026-08-28 | 首版：仅 A 档；分批卖出 |
| v1.1 | 2026-08-28 | **默认改为尽早买+次日收盘卖**；新增 **A_PLUS_B** 完整规则；MRVL 校准 |

---

*本文档为 Athena 实现规格，不构成投资建议。历史胜率基于小样本，实盘需持续回测更新 §7 参数。*
