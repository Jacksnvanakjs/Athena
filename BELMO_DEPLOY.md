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

## 第三步：配置环境变量

在服务的 **Environment variables** 中添加：

| 变量名 | 值 | 说明 |
|--------|-----|------|
| `SERVERCHAN_SENDKEY` | 你的 SendKey | Server酱 推送密钥（必填） |
| `SCRAPE_SECRET` | 随机字符串 | 防止他人滥用抓取接口 |
| `TIMEZONE` | `Asia/Shanghai` | 北京时间 |
| `ENABLE_SCHEDULER` | `true` | 启用内置 9:00 / 18:00 定时抓取 |
| `DATABASE_URL` | `sqlite:////app/data/funds.db` | 数据库路径 |
| `FUNDS_SOURCE_FILE` | `额度数据来源.txt` | 基金列表文件 |

> `SCRAPE_SECRET` 可用终端生成：`openssl rand -hex 16`

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

1. **数据持久化**：免费版容器重启或重新部署后，SQLite 历史数据可能丢失；定时抓取会自动恢复最新额度，图表历史会重新开始累积。
2. **不要用 Dockerfile 部署**：免费版用 Python 自动检测即可（项目已含 `requirements.txt` 和 `.python-version`）。
3. **密钥安全**：不要把 `.env` 提交到 GitHub，只在 Belmo 控制台填环境变量。

## 部署后验证

1. 打开网站首页，应能看到基金额度卡片
2. 访问 `/api/status`，确认 `is_trading_day` 和 `timezone` 正确
3. 点击「立即抓取」测试（若设置了 `SCRAPE_SECRET`，需在请求头加 `X-Scrape-Secret`）
4. 检查 Server酱 / Bark 是否收到测试通知

## 常见问题

**Q: 日志报 `/bin/bash: -c: option requires an argument`？**  
说明启动命令为空，或误识别为 Node.js。在 Belmo 控制台将运行时改为 **Python**，Start command 填 `python run.py`，然后重新部署。

**Q: 部署失败怎么办？**  
查看 Belmo 控制台的 Build logs，确认 Python 3.13 和依赖安装成功。

**Q: 定时任务没跑？**  
确认 `ENABLE_SCHEDULER=true`，且 `TIMEZONE=Asia/Shanghai`。

**Q: 推送没收到？**  
检查 `SERVERCHAN_SENDKEY` 是否正确，Server酱 免费版每天 5 条额度是否用完。
