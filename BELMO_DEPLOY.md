# Belmo 部署指南

Belmo 免费版：**不需绑卡、不需国内实名、24 小时不休眠**，比 Render 更适合本项目（内置定时任务可直接用，无需外部 cron）。

## 第一步：注册 Belmo

1. 打开 https://belmo.io
2. 点击 **Start free**，用 **GitHub** 登录（账号：`Jacksnvanakjs`）
3. **不需要信用卡**

## 第二步：连接 GitHub 仓库

1. 在 Belmo 控制台点击 **New service** → **API**
2. 安装 **Belmo GitHub App**，授权访问仓库
3. 选择仓库：**Jacksnvanakjs/Athena**
4. 分支：`main`

## 第三步：配置 Turso 数据库（推荐）

Belmo 免费版容器重启或重新部署后，本地 `/tmp` 里的 SQLite 会丢失历史数据。接入 **Turso** 可免费持久化，且**不需绑卡、不需国内实名**。

### 3.1 创建 Turso 数据库

1. 打开 https://turso.tech ，用 **GitHub** 注册/登录
2. 控制台创建数据库（或用 CLI 建 group + import）。当前推荐主库在 **孟买** `aws-ap-south-1`（东京节点从国内常超时；官方 AWS 暂不支持 sin/hkg 副本）
3. 复制 **Database URL**（形如 `libsql://athena-apac-xxx.aws-ap-south-1.turso.io`）
4. **Create Token**，复制 **Auth Token**

也可用 Turso CLI（可选）：

```bash
brew install tursodatabase/tap/turso
turso auth login
# 示例：孟买 group + 库（若已有东京库，先 export 再 import，勿两套库同时当主库写）
turso group create asia --location aws-ap-south-1 -w
turso db show athena-apac --url
turso db tokens create athena-apac
```

### 3.2 在 Belmo 填写环境变量

在服务的 **Environment variables** 中添加：

| 变量名 | 必填？ | 值 / 说明 |
|--------|--------|-----------|
| `TURSO_DATABASE_URL` | **必改** | 与本地同一主库 URL（现为孟买 `…aws-ap-south-1.turso.io`）。若仍是旧东京 URL，请改成新库并换 token |
| `TURSO_AUTH_TOKEN` | **必改** | 对应上述库的 token（换库必须换 token） |
| `TURSO_EMBEDDED_REPLICA` | 建议 | 默认 `true`：云端也读本地副本，页面更快；首次部署 sync 可能 1–3 分钟 |
| `TURSO_SYNC_INTERVAL_SEC` | 可选 | 默认 `30`；可调 `120`/`300` 更省 Sync 流量 |
| `TURSO_CONNECT_TIMEOUT_SEC` | 可选 | 默认 `45`；启动超时先起 HTTP，后台重连 Turso |
| `SELF_HEAL_ENABLED` | 建议 | 默认 `true`：定时补全财报涨跌/首日回测等缺失字段 |
| `SELF_HEAL_INTERVAL_MIN` | 可选 | 默认 `20` |
| `BARK_DEVICE_KEY` | **推荐** | iOS Bark App 内复制的 key（无需国内实名；官方 `api.day.app`） |
| `BARK_SERVER_URL` | 可选 | 默认 `https://api.day.app` |
| `BARK_GROUP` | 可选 | 默认 `Athena` |
| `SCRAPE_SECRET` | 建议 | 保护 cron 接口 |
| `TIMEZONE` | 必填 | `Asia/Shanghai` |
| `ENABLE_SCHEDULER` | **必填** | `true`（合作快讯 7×24） |
| `DEAL_POLL_INTERVAL_MIN` | 可选 | 默认 `2` |
| `GEMINI_API_KEY` | 必填 | 合作快讯 LLM |
| `SEC_USER_AGENT` | 建议 | SEC 联系邮箱 UA |
| `FINNHUB_API_KEY` | 建议 | 日历 / ticker |

> **不要**再设 `DATABASE_URL` 当主库；配置了 Turso 后只走 Turso（含 Embedded Replica），不再回退 SQLite。  
> **不要**把另一份独立库填进 `TURSO_DATABASE_URL_FALLBACKS`（会双写分裂）。官方 Edge Replicas 已弃用。

在 Belmo **Settings** 里把 **Health Check Path** 设为：

```
/health
```

> 降级时 `/health` 可能返回 `degraded`（库未就绪）但仍应尽快恢复；HTML 页可开，部分 `/api/*` 会 503。  
> `SCRAPE_SECRET` 可用：`openssl rand -hex 16`

## 第四步：启动命令（重要）

Belmo 必须识别为 **Python**，不能是 Node.js。若显示 Node.js，请在控制台改为 Python，并设置：

- **Start command**：
  ```
  python run.py
  ```

项目根目录已包含 `Procfile`、`belmo.yml`、`runtime.txt`，推送代码后 Belmo 通常会自动识别为 Python/FastAPI。

## 第五步：点击 Deploy

等待 2–4 分钟，部署完成后会获得地址，例如：

```
https://athena-fund.app.belmo.io
```

## 与 Render / Fly 对比

| 特性 | Belmo 免费版 | Render 免费版 | Fly.io |
|------|-------------|---------------|--------|
| 绑卡 | ❌ 不需要 | ❌ 通常不需要 | ✅ 需要 |
| 休眠 | ❌ 不休眠 | ✅ 15 分钟休眠 | ❌ 不休眠 |
| 内置定时任务 | ✅ 直接可用 | ❌ 需外部 cron | ✅ 直接可用 |
| 首次打开速度 | 快 | 可能 30–60 秒 | 快 |

## 注意事项

1. **合作快讯 7×24 扫描**：务必保持 `ENABLE_SCHEDULER=true`，且**不要频繁手动重启/停服**（本地开发可设 `ENABLE_SCHEDULER=false`）。部署后访问 `/health`，应返回 `scheduler.running: true`；若为 `false` 会返回 503，Belmo 可能自动重启容器。
2. **IR RSS 直连**：`app/deal_monitor/company_ir_feeds.json` 已配置 Adobe（Google News 补抓官网）及 60+ 公司 IR RSS，优先于 Finnhub 索引。
3. **数据持久化**：推荐配置 **Turso**（见上文第三步），重新部署后图表历史不丢失。未配置时回退到 `/tmp` 本地库，Redeploy 会清空历史。
4. **不要用 Dockerfile 部署**：免费版用 Python 自动检测即可（项目已含 `requirements.txt` 和 `.python-version`）。
5. **密钥安全**：不要把 `.env` 提交到 GitHub，只在 Belmo 控制台填环境变量。

## 部署后验证

1. 打开网站首页，应能看到基金额度卡片
2. 访问 `/api/status`，确认 `is_trading_day` 和 `timezone` 正确
3. 点击「立即抓取」测试（若设置了 `SCRAPE_SECRET`，需在请求头加 `X-Scrape-Secret`）
4. 访问 `/health`，确认 `scheduler.enabled` 与 `scheduler.running` 均为 `true`
5. 打开 `/deals` 快讯页，表头应显示「发稿 / 抓取 / 推送」三行时间

## 常见问题

**Q: 日志报 `/bin/bash: -c: option requires an argument`？**  
说明启动命令为空，或误识别为 Node.js。在 Belmo 控制台将运行时改为 **Python**，Start command 填 `python run.py`，然后重新部署。

**Q: 部署失败怎么办？**  
查看 Belmo 控制台的 Build logs，确认 Python 3.13 和依赖安装成功。

**Q: 定时任务没跑？**  
确认 `ENABLE_SCHEDULER=true`，且 `TIMEZONE=Asia/Shanghai`。

**Q: 推送没收到？**  
检查 `BARK_DEVICE_KEY` 是否正确；可用 `scripts/send_bark_test.py` 测一条。

**Q: 如何确认 Turso 已生效？**  
访问 `/api/status`，`database` 字段应为 `"turso"`。若为 `"sqlite"`，检查 Belmo 环境变量 `TURSO_DATABASE_URL` 和 `TURSO_AUTH_TOKEN` 是否都已填写并重新部署。
