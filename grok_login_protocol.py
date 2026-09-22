#!/usr/bin/env python3
"""通过 CreateSession gRPC-Web 接口，用已有密码重新登录 email.json。"""

import argparse
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote

from curl_cffi import requests

from g.turnstile_service import TurnstileService
from grok_login import LOGIN_URL, account_password, read_records, save_results
from grok_reset_pwd import (
    DEFAULT_IMPERSONATE,
    PROXIES,
    SITE_URL,
    _enc_varint,
    enc_str,
    grpc_web_frame,
    load_emails,
    parse_grpc_web_response,
)


class LoginError(ValueError):
    """可安全输出的协议错误，不包含请求密码或令牌。"""


def enc_message(field, payload):
    return _enc_varint((field << 3) | 2) + _enc_varint(len(payload)) + payload


def login_payload(email, password, turnstile_token, castle_token=""):
    # accounts.x.ai 登录页公开的 auth_mgmt.proto：
    # request.1 -> credentials.1 -> email/password；request.4 -> anti_abuse_token.1。
    credentials = enc_str(1, email) + enc_str(2, password)
    payload = enc_message(1, enc_message(1, credentials))
    payload += enc_message(4, enc_str(1, turnstile_token))
    if castle_token:
        payload += enc_str(10, castle_token)
    return grpc_web_frame(payload)


def protobuf_fields(data):
    """读取响应的顶层字段；不在整段响应里猜测哪个 JWT 是 SSO。"""
    offset = 0

    def varint():
        nonlocal offset
        value = 0
        for shift in range(0, 70, 7):
            if offset >= len(data):
                raise LoginError("protobuf varint 截断")
            byte = data[offset]
            offset += 1
            if shift == 63 and byte > 1:
                raise LoginError("protobuf varint 溢出")
            value |= (byte & 127) << shift
            if not byte & 128:
                return value
        raise LoginError("protobuf varint 溢出")

    fields = {}
    while offset < len(data):
        tag = varint()
        field, wire = tag >> 3, tag & 7
        if not field:
            raise LoginError("protobuf 字段编号为零")
        if wire == 0:
            value = varint()
        elif wire in (1, 2, 5):
            size = varint() if wire == 2 else (8 if wire == 1 else 4)
            if offset + size > len(data):
                raise LoginError("protobuf 字段截断")
            value = data[offset:offset + size]
            offset += size
        else:
            raise LoginError("不支持的 protobuf wire type")
        fields[field] = value
    return fields


def session_cookie(body):
    fields = protobuf_fields(body)
    session = fields.get(1)
    cookie = fields.get(2)
    if not isinstance(session, bytes) or not isinstance(cookie, bytes):
        raise LoginError("CreateSession 响应缺少 session 或 session_cookie")
    # prod_auth.Session.status：1=PENDING，2=CONFIRMED。
    if protobuf_fields(session).get(5) != 2:
        raise LoginError("会话尚未确认，可能需要邮箱验证或 MFA；请使用浏览器完成登录")
    sso = cookie.decode("utf-8")
    if not sso or any(character.isspace() for character in sso):
        raise LoginError("session_cookie 为空或格式错误")
    return sso


def login_one(email, password, solver, timeout, castle_token=""):
    if not isinstance(castle_token, str):
        raise LoginError("castle_request_token 必须是字符串")
    with requests.Session(impersonate=DEFAULT_IMPERSONATE, proxies=PROXIES) as session:
        page = session.get(LOGIN_URL, timeout=timeout, allow_redirects=False)
        if page.status_code != 200:
            raise LoginError(f"登录页 HTTP {page.status_code}，未提交密码")
        sitekey = re.search(r'0x4[A-Za-z0-9_-]+', page.text)
        if not sitekey:
            raise LoginError("登录页没有 Turnstile sitekey，页面结构可能已变化")
        task = solver.create_task(LOGIN_URL, sitekey.group())
        turnstile_token = solver.get_response(task)
        if not turnstile_token:
            raise LoginError("未取得 Turnstile token，请检查 Solver / YesCaptcha")
        response = session.post(
            f"{SITE_URL}/auth_mgmt.AuthManagement/CreateSession",
            data=login_payload(email, password, turnstile_token, castle_token),
            headers={
                "content-type": "application/grpc-web+proto",
                "x-grpc-web": "1",
                "x-user-agent": "connect-es/2.1.1",
                "origin": SITE_URL,
                "referer": LOGIN_URL,
                "accept": "*/*",
            },
            timeout=timeout,
            allow_redirects=False,
        )
        if response.status_code != 200:
            raise LoginError(f"CreateSession HTTP {response.status_code}")
        body, status, message = parse_grpc_web_response(response.content)
        if status is None:
            status = response.headers.get("grpc-status")
            message = unquote(response.headers.get("grpc-message", ""))
        if status != "0":
            detail = message or "未返回成功的 grpc-status"
            for secret in (password, turnstile_token, castle_token):
                if secret:
                    detail = detail.replace(secret, "[已隐藏]")
            detail = re.sub(r'eyJ[\w=-]+\.[\w=-]+\.[\w=-]+', '[已隐藏]', detail)
            raise LoginError(f"CreateSession gRPC {status}: {' '.join(detail.split())[:300]}")
        if body is None:
            raise LoginError("CreateSession 返回空消息")
        return session_cookie(body)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-e", "--emails", default="email.json")
    parser.add_argument("--credentials", default="email_sso.json", help="邮箱对应的已有密码记录")
    parser.add_argument("-n", "--limit", type=int, default=0, help="只处理前 N 个待登录账号，0=全部")
    parser.add_argument("-o", "--output", type=Path, help="新结果 JSON 路径")
    parser.add_argument("--resume", action="store_true", help="配合 -o 跳过已登录成功的账号")
    parser.add_argument("--timeout", type=int, default=30, help="登录 HTTP 请求超时秒数")
    parser.add_argument("--solver-url", default="http://127.0.0.1:5072")
    args = parser.parse_args()
    if args.limit < 0 or args.timeout <= 0:
        parser.error("limit 必须 >= 0，timeout 必须 > 0")
    if args.resume and (not args.output or not args.output.is_file()):
        parser.error("--resume 需要用 -o 指定已有协议登录结果 JSON")
    output = args.output or Path(f"keys/protocol_login_{datetime.now():%Y%m%d_%H%M%S_%f}.json")
    if output.suffix.lower() != ".json":
        parser.error("输出文件必须以 .json 结尾")
    protected = {Path(p).resolve() for p in (args.emails, args.credentials, "email.json", "email_sso.json", "keys/sso.txt", ".env")}
    if output.resolve() in protected or output.with_suffix(".txt").resolve() in protected:
        parser.error("输出不能覆盖邮箱列表、旧密码记录或配置文件")
    if not args.resume and (output.exists() or output.with_suffix(".txt").exists()):
        parser.error("输出已存在，请换路径；继续上次登录请加 --resume")
    records = read_records(args.credentials)
    results = read_records(output) if args.resume else {}
    emails = [email for email in load_emails(args.emails) if not (
        results.get(email.lower(), {}).get("success") and results.get(email.lower(), {}).get("sso")
    )]
    if args.limit:
        emails = emails[:args.limit]
    accounts = [(email, account_password(email, records)) for email in emails]
    save_results(output, results)
    solver = TurnstileService(solver_url=args.solver_url)
    print(f"协议登录 {len(accounts)} 个账号 | 结果: {output} | SSO: {output.with_suffix('.txt')}", flush=True)
    succeeded = failed = consecutive_failures = 0
    for index, (email, password) in enumerate(accounts, 1):
        print(f"[{index}/{len(accounts)}] {email}", flush=True)
        record = {"email": email, "success": False, "sso": "", "error": ""}
        try:
            castle = records.get(email.lower(), {}).get("castle_request_token", "")
            record["sso"] = login_one(email, password, solver, args.timeout, castle)
            record["success"] = True
            succeeded += 1
            consecutive_failures = 0
            print("    登录成功", flush=True)
        except Exception as error:
            # 第三方异常可能带请求上下文，只输出我们自己的安全错误详情。
            record["error"] = str(error) if isinstance(error, LoginError) else f"请求失败: {type(error).__name__}，请检查网络和 Solver"
            failed += 1
            consecutive_failures += 1
            print(f"    失败: {record['error']}", flush=True)
        record["updated_at"] = datetime.now(timezone.utc).isoformat()
        results[email.lower()] = record
        save_results(output, results)
        if consecutive_failures >= 3:
            print("连续 3 个账号失败，已停止；请检查错误信息。", flush=True)
            break
    print(f"完成: 本次成功 {succeeded} / 失败 {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n已停止，已完成结果已保存，可用 --resume 继续。")
        raise SystemExit(130)
    except (OSError, ValueError) as error:
        print(f"输入或文件写入失败: {error}")
        raise SystemExit(1)
