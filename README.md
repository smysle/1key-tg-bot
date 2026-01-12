# 1Key Google 学生认证 Telegram Bot

基于 1Key 批量认证 API 的 Telegram Bot，支持批量验证 Google 学生认证。

## 功能特性

- ✅ 批量验证（最多5个/批）
- ✅ 自动提取验证ID（支持链接和纯ID）
- ✅ 实时状态更新
- ✅ 自动轮询等待结果
- ✅ CSRF Token 自动管理
- ✅ 取消验证支持
- ✅ 用户统计（支持 Redis 持久化）
- ✅ 管理员功能

## 快速开始

### 1. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env 文件，填入你的配置
```

需要配置：
- `TG_BOT_TOKEN`: 从 [@BotFather](https://t.me/BotFather) 获取
- `ONEKEY_API_KEY`: 你的 1Key API Key
- `REDIS_URL`: (可选) Redis 连接地址，用于持久化统计

### 2. 使用 Docker 部署（推荐）

```bash
docker-compose up -d
```

### 3. 或本地运行

```bash
pip install -r requirements.txt
python bot.py
```

## 使用方法

### 用户命令

| 命令 | 说明 |
|------|------|
| `/start` | 开始使用 |
| `/help` | 查看帮助 |
| `/verify <链接或ID>` | 提交验证 |
| `/batch <链接1> <链接2> ...` | 批量验证（最多5个） |
| `/status <check_token>` | 查询验证状态 |
| `/cancel <ID>` | 取消验证 |
| `/mystats` | 查看个人统计 |

### 管理员命令

| 命令 | 说明 |
|------|------|
| `/stats` | 查看全局统计（总提交数、用户数、Top 10） |
| `/stats24` | 查看24小时统计 |
| `/user <user_id>` | 查看指定用户统计 |

默认管理员ID: `6997010290`

### 自动识别

直接发送验证链接或ID，Bot 会自动识别并开始验证：

```
https://one.google.com/verify?id=6931007a35dfed1a6931adac
```

或直接发送ID：

```
6931007a35dfed1a6931adac
```

### 批量验证

支持多行输入：

```
6931007a35dfed1a6931adac
6931007a35dfed1a6931adad
6931007a35dfed1a6931adae
```

## 项目结构

```
1key-tg-bot/
├── bot.py              # Telegram Bot 主程序
├── config.py           # 配置管理
├── csrf_manager.py     # CSRF Token 管理
├── onekey_client.py    # 1Key API 客户端
├── models.py           # 数据模型
├── stats_storage.py    # 统计存储（Redis/内存）
├── requirements.txt    # Python 依赖
├── Dockerfile          # Docker 构建
├── docker-compose.yml  # Docker Compose
├── .env.example        # 环境变量示例
└── README.md           # 说明文档
```

## 统计功能

### 内存模式（默认）
- 不配置 `REDIS_URL` 时使用
- 重启后数据丢失
- 适合测试环境

### Redis 模式（推荐）
- 配置 `REDIS_URL` 后启用
- 数据持久化
- 支持 24 小时滑动窗口统计

## API 说明

### CSRF Token

Bot 会自动从 `https://batch.1key.me/` 页面获取 CSRF Token，并在过期时自动刷新。

### 批量验证流程

```
1. POST /api/batch (SSE) -> 获取初始结果和 checkToken
2. POST /api/check-status -> 轮询状态直到完成
3. 返回最终结果
```

## 注意事项

⚠️ **重要限制**:
- 每个 IPv4(/32) / IPv6(/64) 只能使用一次
- 每批最多 5 个验证ID
- 代理池可能不稳定，出错请稍后重试

## License

MIT
