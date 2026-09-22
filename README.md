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

### 协议登录（不打开账号登录浏览器）

```bash
# 未配置 YESCAPTCHA_KEY 时，先在另一个终端启动现有 Solver
python api_solver.py --browser_type camoufox --thread 5 --debug

# 使用已有密码，通过 CreateSession gRPC-Web 接口重新登录
python3 grok_login_protocol.py -c 8 -o keys/protocol_relogin.json

# 重新运行：指定同一个结果文件，跳过成功账号，重试失败和未处理的账号
python3 grok_login_protocol.py -c 8 -o keys/protocol_relogin.json --resume
```

`--resume` 只跳过指定结果 JSON 中 `success=true` 且 `sso` 非空的账号，不会重新验证
这些 SSO 是否仍有效，也不依据旧的 `email_sso.json` 重置记录跳过账号。
使用 `--resume` 时，必须用 `-o` 指定已经存在的结果文件。
不加 `--resume` 时，如果指定的 JSON 或同名 TXT 已存在，脚本会报错退出，避免覆盖。
不指定 `-o` 则每次创建带时间戳的新文件，不会跳过之前登录成功的账号。

邮箱和密码来源与浏览器版相同；支持 `-e`、`--credentials`、`-n 1`、`--timeout 30`
和 `--solver-url`。默认 8 个账号并发，用 `-c/--concurrency` 调整，`-c 1` 为串行。
JSON 结果和同名 TXT 中的 SSO 由主线程逐个保存；连续处理到 3 个失败结果或按 Ctrl+C 时，
停止派发新账号，等待在途账号完成并保存。Solver 的 `--thread` 是独立的验证码并发数。
登录请求使用 `curl_cffi`，验证码沿用本地 Solver 或 YesCaptcha；本地 Solver 自身仍使用浏览器。

协议字段来自登录页公开的 protobuf 定义；2026-09-22 已通过本地 Solver 实测一个账号登录成功，
取得已确认会话的 SSO，尚未验证整批账号。
若服务端要求 Castle，需在 `--credentials` 指定的文件里为对应邮箱提供有效的
`castle_request_token`；脚本不会生成该令牌。返回未确认会话（如邮箱验证或 MFA）会记为失败，
需要用浏览器完成登录。该流程不调用重置密码接口。

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
