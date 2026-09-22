#!/usr/bin/env python3
"""用已有密码重新登录 email.json；不会调用注册或重置密码接口。"""

import argparse
import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from patchright.sync_api import Error as BrowserError, TimeoutError as BrowserTimeout, sync_playwright

from grok_reset_pwd import load_emails


LOGIN_URL = "https://accounts.x.ai/sign-in?email=true&redirect=grok-com&return_to=%2F"


def read_records(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or any(not isinstance(v, dict) for v in data.values()):
        raise ValueError(f"{path} 必须是以邮箱为键、记录为值的 JSON 对象")
    return {email.lower(): record for email, record in data.items()}


def account_password(email, records):
    password = records.get(email.lower(), {}).get("password") or os.getenv("ACCOUNT_PASSWORD")
    if not isinstance(password, str) or not password:
        raise ValueError("缺少已保存密码，也未配置 ACCOUNT_PASSWORD")
    return password


def atomic_write(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # 临时文件默认仅当前用户可读；替换前中断也不会损坏原文件。
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def save_results(path, results):
    atomic_write(path, json.dumps(results, ensure_ascii=False, indent=2) + "\n")
    tokens = [record["sso"] for record in results.values() if record.get("success") and record.get("sso")]
    atomic_write(path.with_suffix(".txt"), "".join(token + "\n" for token in tokens))


def login_one(browser, email, password, timeout):
    # 每个账号使用独立上下文，避免把上一个账号的 Cookie 记到下一个账号。
    context = browser.new_context(locale="en-US")
    stage = "打开登录页"
    try:
        page = context.new_page()
        page.set_default_timeout(timeout * 1000)
        page.goto(LOGIN_URL, wait_until="domcontentloaded")
        stage = "填写邮箱"
        page.locator('input[name="email"]').fill(email)
        page.get_by_test_id("sign-in-submit").click()
        stage = "填写密码"
        page.locator('input[name="password"]').fill(password)
        stage = "等待登录页验证"
        print("    等待页面验证；如浏览器要求人机验证，请手动完成。", flush=True)
        page.wait_for_function(
            "() => !!document.querySelector('input[name=\"cf-turnstile-response\"]')?.value"
        )
        stage = "提交登录"
        page.get_by_test_id("sign-in-submit").click()
        stage = "等待 Grok SSO（检查页面是否需要验证码、二次验证或提示密码错误）"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            cookies = {cookie["name"]: cookie["value"] for cookie in context.cookies("https://grok.com/")}
            if cookies.get("sso"):
                return cookies["sso"]
            page.wait_for_timeout(500)
        raise RuntimeError(f"{stage}超时")
    except BrowserTimeout:
        raise RuntimeError(f"{stage}超时") from None
    except BrowserError:
        # Playwright 异常的调用日志可能含 fill(password)，不要输出或落盘。
        raise RuntimeError(f"{stage}失败，浏览器关闭或页面操作异常") from None
    finally:
        try:
            context.close()
        except BrowserError:
            pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-e", "--emails", default="email.json", help="邮箱列表 JSON")
    parser.add_argument("--credentials", default="email_sso.json", help="已有密码记录")
    parser.add_argument("-n", "--limit", type=int, default=0, help="只处理前 N 个待登录账号，0=全部")
    parser.add_argument("-o", "--output", type=Path, help="结果 JSON 路径，默认 keys/login_时间戳.json")
    parser.add_argument("--resume", action="store_true", help="配合 -o 跳过该登录结果中已成功的账号")
    parser.add_argument("--timeout", type=int, default=120, help="每个登录阶段的等待秒数，默认 120")
    args = parser.parse_args()
    if args.limit < 0 or args.timeout <= 0:
        parser.error("limit 必须 >= 0，timeout 必须 > 0")
    if args.resume and (not args.output or not args.output.is_file()):
        parser.error("--resume 需要用 -o 指定已有登录结果 JSON")
    output = args.output or Path(f"keys/login_{datetime.now():%Y%m%d_%H%M%S_%f}.json")
    if output.suffix.lower() != ".json":
        parser.error("输出文件必须以 .json 结尾")
    protected = {Path(p).resolve() for p in (args.emails, args.credentials, "email.json", "email_sso.json", "keys/sso.txt", ".env")}
    if output.resolve() in protected or output.with_suffix(".txt").resolve() in protected:
        parser.error("输出不能覆盖邮箱列表、旧密码记录或配置文件")
    if not args.resume and (output.exists() or output.with_suffix(".txt").exists()):
        parser.error("输出已存在，请换路径；继续上次登录请加 --resume")

    records = read_records(args.credentials)
    results = read_records(output) if args.resume else {}
    emails = load_emails(args.emails)
    emails = [email for email in emails if not (
        results.get(email.lower(), {}).get("success") and results.get(email.lower(), {}).get("sso")
    )]
    if args.limit:
        emails = emails[:args.limit]
    if not emails:
        if args.resume:
            save_results(output, results)
        print("没有待登录账号")
        return 0
    # 开浏览器前检查全部待处理账号的密码，避免跑到一半才发现输入缺失。
    accounts = [(email, account_password(email, records)) for email in emails]
    save_results(output, results)
    print(f"待登录 {len(accounts)} 个账号 | 结果: {output} | SSO: {output.with_suffix('.txt')}", flush=True)
    print("使用独立 Chrome 窗口依次登录；Ctrl+C 停止，已完成结果会保留。", flush=True)
    succeeded = failed = consecutive_failures = 0
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=False)
        try:
            for index, (email, password) in enumerate(accounts, 1):
                print(f"[{index}/{len(accounts)}] {email}", flush=True)
                record = {"email": email, "success": False, "sso": "", "error": ""}
                try:
                    record["sso"] = login_one(browser, email, password, args.timeout)
                    record["success"] = True
                    succeeded += 1
                    consecutive_failures = 0
                    print("    登录成功", flush=True)
                except RuntimeError as error:
                    record["error"] = str(error)
                    failed += 1
                    consecutive_failures += 1
                    print(f"    失败: {error}", flush=True)
                record["updated_at"] = datetime.now(timezone.utc).isoformat()
                results[email.lower()] = record
                save_results(output, results)
                if consecutive_failures >= 3:
                    print("连续 3 个账号失败，已停止；请先检查浏览器提示。", flush=True)
                    break
        finally:
            try:
                browser.close()
            except BrowserError:
                pass
    print(f"完成: 本次成功 {succeeded} / 失败 {failed} | 结果: {output}")
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n已停止，已完成的账号结果已保存。")
        raise SystemExit(130)
    except BrowserError:
        print("浏览器启动或连接失败，请确认已安装 Google Chrome；已保存结果可用 --resume 继续。")
        raise SystemExit(1)
    except (OSError, ValueError) as error:
        print(f"输入或文件写入失败: {error}")
        raise SystemExit(1)
