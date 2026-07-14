# Grok 批量注册工具

批量注册 Grok 账号并自动开启 NSFW 功能。

## 功能

- 自动创建临时邮箱
- 自动获取验证码
- 自动完成注册流程
- 自动开启 NSFW/Unhinged 模式
- 注册完成后自动清理临时邮箱
- 支持多线程并发注册

## 文件说明

| 文件 | 说明 |
|------|------|
| `grok.py` | 主程序，批量注册入口 |
| `TurnstileSolver.bat` | Turnstile Solver 启动脚本 |
| `api_solver.py` | Turnstile 验证码解决器 |
| `browser_configs.py` | 浏览器指纹配置 |
| `db_results.py` | 验证结果存储 |
| `g/email_service.py` | 临时邮箱服务（cloudflare_temp_email） |
| `g/turnstile_service.py` | Turnstile 验证服务 |
| `g/user_agreement_service.py` | 用户协议同意服务 |
| `g/nsfw_service.py` | NSFW 设置服务 |
| `.env.example` | 环境变量模板 |
| `requirements.txt` | Python 依赖列表 |

## 依赖

- [cloudflare_temp_email](https://github.com/dreamhunter2333/cloudflare_temp_email) - 临时邮箱服务（通过 `MAIL_BASE_URL` 配置）
- Turnstile Solver - 内置验证码解决方案

## 安装

```bash
pip install -r requirements.txt
```

## 配置

复制 `.env.example` 为 `.env` 并填写配置：

```bash
cp .env.example .env
```

配置项说明：

| 配置项 | 说明 |
|--------|------|
| MAIL_BASE_URL | 临时邮箱服务地址（必填，如 `https://mail.example.com`） |
| MAIL_ADMIN_PASSWORD | Admin 密码（对应 worker 的 `ADMIN_PASSWORDS` / 请求头 `x-admin-auth`） |
| MAIL_DOMAIN | 邮箱域名（必填，如 `example.com`） |
| MAIL_SITE_PASSWORD | 站点密码（可选，启用 `x-custom-auth` 时填写） |
| YESCAPTCHA_KEY | YesCaptcha API Key（可选，不填使用本地 Solver） |

## 使用

### 1. 启动 Turnstile Solver

双击运行 `TurnstileSolver.bat` 或执行：

```bash
python api_solver.py --browser_type camoufox --thread 5 --debug
```

等待 Solver 启动完成（监听 `http://127.0.0.1:5072`）

### 2. 运行注册程序

新开一个终端，运行：

```bash
python grok.py
```

按提示输入：
- 并发数（默认 8）
- 注册数量（默认 100）

注册成功的 SSO Token 保存在 `keys/grok_时间戳_数量.txt`

## 输出示例

```
============================================================
Grok 注册机
============================================================
[*] 正在初始化...
[+] Action ID: 7f67aa61adfb0655899002808e1d443935b057c25b
[*] 启动 8 个线程，目标 10 个
[*] 输出: keys/grok_20260204_190000_10.txt
[*] 开始注册: abc123@example.com
[+] 1/10 abc123@example.com | 5.2s/个
[+] 2/10 def456@example.com | 4.8s/个
...
[*] 开始二次验证 NSFW...
[*] 二次验证完成: 10/10
```

## 注意事项

- 需可用的 cloudflare_temp_email 服务，并在 `.env` 中配置 `MAIL_BASE_URL` / `MAIL_DOMAIN`
- 运行前必须先启动 Turnstile Solver
- 仅供学习研究使用
