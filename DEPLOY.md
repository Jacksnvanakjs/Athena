# 云端部署指南

本地电脑关机后，需要把服务部署到 **24 小时在线的云端服务器**，定时抓取和 Bark/微信通知才能持续工作。

## 平台推荐（免备案 + 尽量免实名）

| 平台 | 免费 | 需备案 | 需实名 | 24小时运行 | 推荐度 |
|------|------|--------|--------|------------|--------|
| **Fly.io** | 有免费额度 | 否（`*.fly.dev`） | 仅需 GitHub 登录，无需国内实名 | ✅ 是 | ⭐⭐⭐ 首选 |
| **Render** | 免费版 | 否（`*.onrender.com`） | 仅需 GitHub 登录 | ❌ 会休眠，需外部 cron | ⭐⭐ 备选 |
| **Oracle Cloud** | 永久免费 VM | 否 | 需信用卡验证 | ✅ 是 | ⭐⭐ 适合懂 Linux |
| 国内云（阿里云/腾讯云） | 有试用 | **需要备案** | **需要实名** | ✅ | ❌ 不推荐 |

> **备案**：只有用国内服务器 + 自己的域名时才需要。使用 Fly.io / Render 提供的 `xxx.fly.dev` 或 `xxx.onrender.com` 子域名，**不需要备案**。
>
> **实名**：Fly.io 和 Render 用 GitHub 账号注册即可，**不需要中国身份证实名**。

---

## 方案一：Fly.io 部署（推荐）

Fly.io 在香港有节点，国内访问较快，免费额度可跑一个小服务 24 小时。

### 1. 准备工作

```bash
# 安装 flyctl（macOS）
brew install flyctl

# 登录（浏览器用 GitHub 授权）
fly auth login
```

### 2. 初始化并部署

```bash
cd /Users/admin/USA/Athena

# 首次部署（会提示创建 app、选区域选 hkg 香港）
fly launch --no-deploy

# 创建持久化存储卷（保存 SQLite 数据库）
fly volumes create athena_data --region hkg --size 1

# 设置环境变量（替换成你的真实值）
fly secrets set \
  SERVERCHAN_SENDKEY=你的SendKey \
  SCRAPE_SECRET=随机字符串比如abc123xyz \
  TIMEZONE=Asia/Shanghai \
  ENABLE_SCHEDULER=true

# 部署
fly deploy
```

### 3. 访问

部署完成后会获得地址，例如：

```
https://athena-fund.fly.dev
```

把链接发给朋友即可访问。电脑关机也不影响定时抓取和 Bark 推送。

### 4. 常用命令

```bash
fly logs          # 查看日志
fly status        # 查看运行状态
fly ssh console   # 进入服务器
fly secrets list  # 查看已设的环境变量名
```

---

## 方案二：Render 部署（免费但会休眠）

Render 免费版 15 分钟无访问会休眠，**内置定时任务不可靠**，需要配合外部 cron。

### 1. 推送代码到 GitHub

```bash
git init
git add .
git commit -m "init"
# 在 GitHub 创建仓库后
git remote add origin https://github.com/你的用户名/athena.git
git push -u origin main
```

### 2. 在 Render 创建服务

1. 打开 https://render.com ，用 GitHub 登录
2. New → Blueprint → 连接仓库（会自动读取 `render.yaml`）
3. 在 Environment 中手动填入：
   - `SERVERCHAN_SENDKEY`
   - `SCRAPE_SECRET`（随机字符串）

### 3. 配置外部定时任务

因为 Render 免费版会休眠，用 [cron-job.org](https://cron-job.org)（免费）代替内置调度器：

| 时间 | Cron 表达式 | URL |
|------|-------------|-----|
| 每天 9:00（北京时间） | `0 1 * * *`（UTC） | `https://你的app.onrender.com/api/cron/scrape?secret=你的SCRAPE_SECRET` |
| 每天 18:00（北京时间） | `0 10 * * *`（UTC） | 同上 |

> cron-job.org 注册后，创建两个定时任务，时区选 UTC，填入上表中的表达式和 URL。

---

## 环境变量说明

| 变量 | 必填 | 说明 |
|------|------|------|
| `SERVERCHAN_SENDKEY` | 是 | 你的 Server酱 SendKey |
| `SCRAPE_SECRET` | 公网部署必填 | 防止他人滥用抓取接口 |
| `TIMEZONE` | 是 | 固定填 `Asia/Shanghai` |
| `ENABLE_SCHEDULER` | - | Fly.io 填 `true`；Render 填 `false` |
| `PUSHPLUS_TOKEN` | 否 | 如用 PushPlus 则填写 |

---

## 安全提醒

1. **不要把 `.env` 提交到 GitHub**，密钥通过平台的环境变量 / secrets 配置
2. 公网部署后务必设置 `SCRAPE_SECRET`
3. 如果网站完全公开，建议后续加登录功能，避免他人滥用「立即抓取」

---

## 费用预估

- **Fly.io 免费额度**：足够跑本项目的单实例（256MB 内存）
- **Render 免费版**：$0，但会休眠
- **cron-job.org**：$0
- **Server酱**：你当前的免费版每天 5 条，够用（每天最多抓 2 次 + 偶尔变化通知）

如果推送量增大，可考虑 Server酱 订阅（约 8 元/月，1000 条/天）。
