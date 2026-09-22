# Grok 批量注册工具

批量注册 Grok 账号并自动开启 NSFW 功能。

## 功能

- 自动创建临时邮箱并获取验证码
- 自动完成注册流程
- 自动开启 NSFW / Unhinged 模式
- 注册完成后自动清理临时邮箱
- 支持多线程并发注册
- 支持本地 Turnstile Solver 或 YesCaptcha

## 项目结构

```
.
├── grok.py                    # 主程序，批量注册入口
├── api_solver.py              # 本地 Turnstile 验证码解决器
├── browser_configs.py         # 浏览器指纹配置
├── db_results.py              # 验证结果存储
├── TurnstileSolver.bat        # Windows 下一键启动 Solver
├── .env.example               # 环境变量模板
├── requirements.txt           # Python 依赖
└── g/
    ├── email_service.py       # 临时邮箱服务（cloudflare_temp_email）
    ├── turnstile_service.py   # Turnstile 验证服务
    ├── user_agreement_service.py
    └── nsfw_service.py        # NSFW 设置服务
```

## 依赖

- [cloudflare_temp_email](https://github.com/dreamhunter2333/cloudflare_temp_email) — 临时邮箱服务，通过环境变量配置接入地址
- 本地 Turnstile Solver（内置 `api_solver.py`）或 [YesCaptcha](https://yescaptcha.com/)

## 安装

```bash
pip install -r requirements.txt
```

## 配置

复制环境变量模板并填写：

```bash
cp .env.example .env
```

### 环境变量

| 配置项 | 必填 | 说明 |
|--------|------|------|
| `MAIL_BASE_URL` | 是 | 临时邮箱服务地址，例如 `https://mail.example.com` |
| `MAIL_ADMIN_PASSWORD` | 是 | Admin 密码（对应 worker 的 `ADMIN_PASSWORDS`，请求头 `x-admin-auth`） |
| `MAIL_DOMAIN` | 是 | 邮箱域名，例如 `example.com` |
| `MAIL_SITE_PASSWORD` | 否 | 站点密码（启用 `x-custom-auth` 时填写） |
| `YESCAPTCHA_KEY` | 否 | YesCaptcha API Key；不填则使用本地 Solver（`http://127.0.0.1:5072`） |

兼容别名（可选）：

| 别名 | 等价于 |
|------|--------|
| `WORKER_DOMAIN` | `MAIL_BASE_URL` |
| `ADMIN_PASSWORD` / `FREEMAIL_TOKEN` | `MAIL_ADMIN_PASSWORD` |

`.env` 示例：

```env
MAIL_BASE_URL=https://mail.example.com
MAIL_ADMIN_PASSWORD=your-admin-password
MAIL_DOMAIN=example.com
MAIL_SITE_PASSWORD=
YESCAPTCHA_KEY=
```

> `.env` 含敏感信息，已加入 `.gitignore`，请勿提交到仓库。

## 使用

### 1. 启动 Turnstile Solver（未配置 YesCaptcha 时）

Windows 可双击 `TurnstileSolver.bat`，或手动执行：

```bash
python api_solver.py --browser_type camoufox --thread 5 --debug
```

等待 Solver 就绪（默认监听 `http://127.0.0.1:5072`）。

若已配置 `YESCAPTCHA_KEY`，可跳过本步骤。

### 2. 运行注册程序

新开一个终端：

```bash
python grok.py
```

按提示输入：

- 并发数（默认 `8`）
- 注册数量（默认 `100`）

### 3. 输出

成功注册的 SSO Token 保存在：

```text
keys/grok_<时间戳>_<数量>.txt
```

## 已有账号重新登录

使用 `email.json` 的邮箱和 `email_sso.json` 保存的密码重新登录；缺少密码时使用
`.env` 的 `ACCOUNT_PASSWORD`。不会重置密码。需要安装 Google Chrome，以及项目已有的
`patchright` 依赖（`pip install -r requirements.txt`）。

```bash
python3 grok_login.py
```

脚本打开独立 Chrome 窗口，逐个账号登录。遇到人机验证或二次验证时，在窗口中手动完成。
每个阶段默认等待 120 秒，可用 `--timeout 300` 延长；连续 3 个账号失败会停止。
新结果保存至 `keys/login_<时间戳>.json`，新 SSO 同时导出到同名 `.txt`，旧密码记录不变。

```bash
# 先登录一个账号
python3 grok_login.py -n 1

# 指定结果文件；中断后使用同一文件继续，跳过已登录成功的账号
python3 grok_login.py -o keys/relogin.json
python3 grok_login.py -o keys/relogin.json --resume
```

## 注册输出示例

```text
============================================================
Grok 注册机
============================================================
[*] 正在初始化...
[+] 注册页可访问 (impersonate=chrome136)
[+] Action ID: 7f67aa61adfb0655899002808e1d443935b057c25b

并发数 (默认8): 8
注册数量 (默认100): 10
[*] 启动 8 个线程，目标 10 个
[*] 输出: keys/grok_20260714_190000_10.txt
[*] 开始注册: abc123@example.com
[✓] 注册成功: 1/10 | abc123@example.com | SSO: sso_xxx... | 平均: 5.2s | NSFW: ok
...
```

## 注意事项

1. 必须先配置可用的 cloudflare_temp_email 服务，并在 `.env` 中填写 `MAIL_BASE_URL`、`MAIL_DOMAIN`、`MAIL_ADMIN_PASSWORD`
2. 未配置 `YESCAPTCHA_KEY` 时，运行前必须先启动本地 Turnstile Solver
3. 仅供学习研究使用
