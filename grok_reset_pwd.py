#!/usr/bin/env python3
"""Grok / xAI 协议批量重置密码。

流程:
  1. GET /reset-password?email=...&_rsc=...  — 触发重置（本身就会发 1 封验证码）
  2. 从临时邮箱拉取验证码
  3. VerifyEmailValidationCode              — 校验验证码
  4. ResetPasswordByEmailValidationCode     — 设置新密码，响应里直接取 sso JWT

注意:
  - 不要再额外调 CreateEmailValidationCode，也不要重复 GET reset-password，
    否则会触发多封验证码邮件 / 限流。
  - 不再调用 createSessionRedirectUrl / redeem_session：该步在 xAI 当前部署
    上会再次补发一封验证码邮件，而 sso JWT 已可直接从设密响应中提取。

 邮箱列表: email.json
 新密码:   .env 中的 ACCOUNT_PASSWORD

默认行为:
  - email_sso.json 中已有 success=true 记录的邮箱 → 跳过，不再重置
  - 无记录或 success=false（失败）的邮箱 → 继续重置
  - 可用 --no-skip-success 关闭跳过
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import random
import re
import struct
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import quote, unquote

from curl_cffi import requests as creq
from dotenv import load_dotenv

from g.email_service import extract_verification_code

load_dotenv()

SITE_URL = "https://accounts.x.ai"
DEFAULT_IMPERSONATE = "chrome136"
IMPERSONATE_CANDIDATES = [
    "chrome136",
    "chrome133a",
    "chrome131",
    "chrome124",
]

# ResetPassword 页上的 Next.js server action（部署变更时会自动重新扫描）
ACTION_GET_NUM_ONE_TIME_LINKS = "00cdb835246d03ce4702a85ced485c82e4eaf3381c"

# ResetPasswordByEmailValidationCodeRequest:
#   1 email_validation_code
#   2 clear_text_password
#   3 email
#   4 num_one_time_links
#   5 castle_request_token (optional)

PROXIES = {
    # "http": "http://127.0.0.1:10808",
    # "https": "http://127.0.0.1:10808",
}

file_lock = threading.Lock()
print_lock = threading.Lock()
success_count = 0
fail_count = 0
start_time = time.time()
stop_event = threading.Event()

# 结果落盘：email_sso.json  ({邮箱: {success, sso, password, error, updated_at}})
RESULT_JSON = "email_sso.json"
_results: dict[str, dict] = {}


def log(msg: str) -> None:
    with print_lock:
        print(msg, flush=True)


# --------------- protobuf helpers ---------------

def _enc_varint(value: int) -> bytes:
    out = bytearray()
    v = int(value)
    while True:
        if v > 0x7F:
            out.append((v & 0x7F) | 0x80)
            v >>= 7
        else:
            out.append(v)
            break
    return bytes(out)


def enc_str(field_id: int, value: str) -> bytes:
    raw = value.encode("utf-8")
    return bytes([(field_id << 3) | 2]) + _enc_varint(len(raw)) + raw


def enc_varint_field(field_id: int, value: int) -> bytes:
    return bytes([(field_id << 3) | 0]) + _enc_varint(value)


def grpc_web_frame(payload: bytes) -> bytes:
    return b"\x00" + struct.pack(">I", len(payload)) + payload


def parse_grpc_web_response(content: bytes) -> tuple[Optional[bytes], Optional[str], Optional[str]]:
    """返回 (message_bytes, grpc_status, grpc_message)。"""
    status = None
    message = None
    msg = None
    offset = 0
    while offset + 5 <= len(content):
        flags = content[offset]
        length = struct.unpack(">I", content[offset + 1 : offset + 5])[0]
        offset += 5
        if offset + length > len(content):
            break
        frame = content[offset : offset + length]
        offset += length
        if flags & 0x80:  # trailer
            try:
                trailer = frame.decode("utf-8", errors="replace")
            except Exception:
                trailer = ""
            for line in trailer.split("\r\n"):
                if ":" not in line:
                    continue
                k, v = line.split(":", 1)
                k, v = k.strip().lower(), v.strip()
                if k == "grpc-status":
                    status = v
                elif k == "grpc-message":
                    message = unquote(v)
        else:
            msg = frame
    return msg, status, message


def extract_jwt_strings(data: bytes) -> list[str]:
    return re.findall(
        rb"eyJ[A-Za-z0-9_\-]+=*\.eyJ[A-Za-z0-9_\-]+[=]*\.[A-Za-z0-9_\-]+",
        data,
    )


# --------------- mail admin helpers ---------------

class MailAdmin:
    """用 admin API 绑定已有邮箱并读信（email.json 里的地址已存在于 CF worker）。"""

    def __init__(self):
        self.base_url = (
            os.getenv("MAIL_BASE_URL") or os.getenv("WORKER_DOMAIN") or ""
        ).rstrip("/")
        if self.base_url and not self.base_url.startswith("http"):
            self.base_url = f"https://{self.base_url}"
        self.admin_password = (
            os.getenv("MAIL_ADMIN_PASSWORD")
            or os.getenv("ADMIN_PASSWORD")
            or os.getenv("FREEMAIL_TOKEN")
        )
        self.site_password = (os.getenv("MAIL_SITE_PASSWORD") or "").strip()
        if not self.base_url or not self.admin_password:
            raise ValueError("缺少 MAIL_BASE_URL / MAIL_ADMIN_PASSWORD")
        self.headers = {
            "x-admin-auth": self.admin_password,
            "Content-Type": "application/json",
            "x-lang": "zh",
        }
        if self.site_password:
            self.headers["x-custom-auth"] = self.site_password

    def ensure_address(self, email: str) -> bool:
        """确保邮箱在系统中存在。已存在则直接可用；不存在则创建。"""
        import requests as req

        try:
            r = req.get(
                f"{self.base_url}/admin/address",
                headers=self.headers,
                params={"limit": 5, "offset": 0, "query": email},
                timeout=15,
            )
            if r.status_code == 200:
                for row in (r.json() or {}).get("results") or []:
                    if row.get("name") == email:
                        return True
            local, _, domain = email.partition("@")
            if not local or not domain:
                return False
            r = req.post(
                f"{self.base_url}/admin/new_address",
                headers=self.headers,
                json={"name": local, "domain": domain, "enablePrefix": False},
                timeout=15,
            )
            if r.status_code == 200:
                return True
            # 并发创建可能冲突，再查一次
            r = req.get(
                f"{self.base_url}/admin/address",
                headers=self.headers,
                params={"limit": 5, "offset": 0, "query": email},
                timeout=15,
            )
            if r.status_code == 200:
                for row in (r.json() or {}).get("results") or []:
                    if row.get("name") == email:
                        return True
            log(f"[-] {email} 绑定邮箱失败: {r.status_code} {r.text[:120]}")
            return False
        except Exception as e:
            log(f"[-] {email} 绑定邮箱异常: {e}")
            return False

    def fetch_code(self, email: str, since_ts: float, max_attempts: int = 40) -> Optional[str]:
        """轮询 admin mails，取 since_ts 之后的新验证码。"""
        import requests as req

        interval = 2
        # 给一点时间容差
        since = since_ts - 5
        for attempt in range(max_attempts):
            if stop_event.is_set():
                return None
            try:
                r = req.get(
                    f"{self.base_url}/admin/mails",
                    headers=self.headers,
                    params={"limit": 10, "offset": 0, "address": email},
                    timeout=15,
                )
                if r.status_code == 429:
                    time.sleep(min(interval * 2, 10))
                    interval = min(interval * 2, 10)
                    continue
                if r.status_code == 200:
                    # 优先最新邮件；admin 列表一般按时间倒序
                    best_code = None
                    best_ts = -1.0
                    for mail in (r.json() or {}).get("results") or []:
                        created = mail.get("created_at") or ""
                        # created_at: "2026-07-30 13:50:14" 为 UTC（无时区后缀）
                        ct = None
                        try:
                            ct = (
                                datetime.strptime(created, "%Y-%m-%d %H:%M:%S")
                                .replace(tzinfo=timezone.utc)
                                .timestamp()
                            )
                            # 只要触发之后的信（放宽 60s 时钟误差）
                            if ct + 60 < since:
                                continue
                        except Exception:
                            pass
                        raw = mail.get("raw") or ""
                        subj_m = re.search(r"(?im)^Subject:\s*(.+)$", raw)
                        subject = subj_m.group(1).strip() if subj_m else ""
                        code = None
                        for text in (subject, raw):
                            code = extract_verification_code(text)
                            if code:
                                break
                        if not code:
                            continue
                        # 多封时取最新
                        ts = ct if ct is not None else 0.0
                        if ts >= best_ts:
                            best_ts = ts
                            best_code = code
                    if best_code:
                        return best_code
            except Exception:
                pass
            time.sleep(interval)
            if attempt > 0 and attempt % 5 == 0:
                interval = min(interval + 1, 5)
        return None


# --------------- xAI protocol ---------------

def grpc_headers(email: str) -> dict:
    return {
        "content-type": "application/grpc-web+proto",
        "x-grpc-web": "1",
        "x-user-agent": "connect-es/2.1.1",
        "origin": SITE_URL,
        "referer": f"{SITE_URL}/reset-password?email={quote(email)}",
        "accept": "*/*",
    }


def trigger_reset_password(session: creq.Session, email: str) -> bool:
    """按 grok-reset-pwd.md：GET /reset-password?email=... 触发重置并发验证码。

    只应调用一次。普通 HTML GET 与 RSC GET 都会各自发信，这里统一用文档里的 RSC 请求。
    """
    # 先轻量访问首页拿 cf cookie，不带 email，避免误触发发信
    try:
        session.get(SITE_URL, timeout=15)
    except Exception:
        pass

    headers = {
        "accept": "*/*",
        "accept-language": "en",
        "rsc": "1",
        "next-url": "/sign-in",
        "next-router-state-tree": (
            "%5B%22%22%2C%7B%22children%22%3A%5B%22(app)%22%2C%7B%22children%22%3A%5B%22(auth)%22%2C%7B%22children%22%3A%5B%22reset-password%22%2C%7B%22children%22%3A%5B%22__PAGE__%22%2C%7B%7D%2Cnull%2Cnull%2C0%5D%7D%2Cnull%2C%22refetch%22%2C0%5D%7D%2Cnull%2Cnull%2C0%5D%7D%2Cnull%2Cnull%2C0%5D%7D%2Cnull%2Cnull%2C16%5D"
        ),
        "referer": (
            f"{SITE_URL}/sign-in?redirect=grok-com"
            f"&return_to=/?q=%26reasoningMode=none%26voice=false&email=true"
        ),
        "priority": "u=1, i",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
    }
    try:
        res = session.get(
            f"{SITE_URL}/reset-password",
            params={"email": email, "_rsc": "1"},
            headers=headers,
            timeout=30,
        )
        if res.status_code == 200:
            return True
        log(f"[-] {email} 触发重置失败: HTTP {res.status_code}")
        return False
    except Exception as e:
        log(f"[-] {email} 触发重置异常: {e}")
        return False


def verify_code(session: creq.Session, email: str, code: str) -> bool:
    # VerifyEmailValidationCodeRequest: 1=email, 2=email_validation_code
    # 邮件主题常带连字符 (ABC-DEF)，接口两种都试
    candidates = []
    for c in (code, code.replace("-", ""), code.upper(), code.replace("-", "").upper()):
        if c and c not in candidates:
            candidates.append(c)

    url = f"{SITE_URL}/auth_mgmt.AuthManagement/VerifyEmailValidationCode"
    last_err = ""
    for c in candidates:
        payload = enc_str(1, email) + enc_str(2, c)
        try:
            res = session.post(
                url, data=grpc_web_frame(payload), headers=grpc_headers(email), timeout=20
            )
            _, status, gmsg = parse_grpc_web_response(res.content)
            if status is None:
                status = res.headers.get("grpc-status")
                gmsg = unquote(res.headers.get("grpc-message") or "") or gmsg
            if res.status_code == 200 and (status is None or status == "0"):
                return True
            last_err = gmsg or status or str(res.status_code)
        except Exception as e:
            last_err = str(e)
    log(f"[-] {email} 验证码校验失败: {last_err}")
    return False


def get_num_one_time_links(session: creq.Session, email: str, action_id: str) -> int:
    try:
        res = session.post(
            f"{SITE_URL}/reset-password",
            params={"email": email},
            headers={
                "accept": "text/x-component",
                "content-type": "text/plain;charset=UTF-8",
                "next-action": action_id,
                "origin": SITE_URL,
                "referer": f"{SITE_URL}/reset-password?email={quote(email)}",
            },
            data="[]",
            timeout=20,
        )
        # 响应类似: 0:{...}\n1:4\n  取最后一个纯数字
        nums = re.findall(r"(?m)^(?:\d+:)?(\d+)\s*$", res.text)
        if nums:
            return max(1, int(nums[-1]))
    except Exception:
        pass
    return 2


def reset_password(
    session: creq.Session,
    email: str,
    code: str,
    password: str,
    num_links: int = 2,
) -> Optional[str]:
    """设置新密码，返回响应中直接提取的 sso JWT。"""
    # 验证码保留原始格式优先
    code_variants = [code, code.replace("-", "")]
    url = f"{SITE_URL}/auth_mgmt.AuthManagement/ResetPasswordByEmailValidationCode"
    last_err = ""
    for c in code_variants:
        if not c:
            continue
        payload = (
            enc_str(1, c)
            + enc_str(2, password)
            + enc_str(3, email)
            + enc_varint_field(4, num_links)
        )
        try:
            res = session.post(
                url, data=grpc_web_frame(payload), headers=grpc_headers(email), timeout=30
            )
            body, status, gmsg = parse_grpc_web_response(res.content)
            if status is None:
                status = res.headers.get("grpc-status")
                gmsg = unquote(res.headers.get("grpc-message") or "") or gmsg
            if res.status_code != 200 or (status and status != "0"):
                last_err = gmsg or status or str(res.status_code)
                continue
            data = body or res.content
            jwts = [j.decode() for j in extract_jwt_strings(data)]
            if jwts:
                return jwts[0]
            last_err = "响应中无 sso JWT"
        except Exception as e:
            last_err = str(e)
    log(f"[-] {email} 重置密码失败: {last_err}")
    return None


def discover_get_num_action(session: creq.Session) -> str:
    """扫描 reset-password 页里的 getNumOneTimeLinks next-action id。

    不带 email 参数访问页面，避免额外触发一封重置验证码邮件。
    """
    action_id = ACTION_GET_NUM_ONE_TIME_LINKS
    try:
        res = session.get(f"{SITE_URL}/reset-password", timeout=30)
        if res.status_code != 200:
            return action_id
        from bs4 import BeautifulSoup
        from urllib.parse import urljoin

        soup = BeautifulSoup(res.text, "html.parser")
        js_urls = [
            urljoin(SITE_URL, sc["src"])
            for sc in soup.find_all("script", src=True)
            if "_next/static" in sc.get("src", "")
        ]
        for ju in js_urls:
            try:
                t = session.get(ju, timeout=20).text
            except Exception:
                continue
            for pat in (
                r'createServerReference\)\("([a-f0-9]{40,44})"[^)]*"([^"]*)"\)',
                r'createServerReference\("([a-f0-9]{40,44})"[^)]*"([^"]*)"\)',
            ):
                for aid, name in re.findall(pat, t):
                    if name == "getNumOneTimeLinks":
                        return aid
    except Exception as e:
        log(f"[!] action 扫描失败，使用内置默认值: {e}")
    return action_id


def open_session():
    last_err = None
    for imp in IMPERSONATE_CANDIDATES:
        try:
            s = creq.Session(impersonate=imp, proxies=PROXIES)
            r = s.get(SITE_URL, timeout=15)
            if r.status_code < 500:
                return s, imp
        except Exception as e:
            last_err = e
    raise RuntimeError(f"无法建立会话: {last_err}")


# --------------- result persistence ---------------

def load_results(path: str = RESULT_JSON) -> dict[str, dict]:
    p = Path(path)
    if not p.exists() or p.stat().st_size == 0:
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
    out: dict[str, dict] = {}
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and item.get("email"):
                out[str(item["email"]).lower()] = item
    elif isinstance(data, dict):
        # 兼容 {email: {...}} 或 {email: sso_string}
        for k, v in data.items():
            if isinstance(v, dict):
                item = dict(v)
                item.setdefault("email", k)
                out[str(item["email"]).lower()] = item
            elif isinstance(v, str):
                out[str(k).lower()] = {
                    "email": k,
                    "success": bool(v),
                    "sso": v,
                }
    return out


def save_results(path: str = RESULT_JSON) -> None:
    """以邮箱为 key 写 dict；未处理的不会出现。"""
    data = {
        item["email"]: item
        for item in sorted(_results.values(), key=lambda x: x.get("email") or "")
    }
    Path(path).write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def record_result(
    email: str,
    *,
    success: bool,
    sso: str = "",
    password: str = "",
    error: str = "",
    result_json: str = RESULT_JSON,
) -> None:
    global success_count, fail_count
    item = {
        "email": email,
        "success": bool(success),
        "sso": sso or "",
        "password": password or "",
        "error": error or "",
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    with file_lock:
        _results[email.lower()] = item
        if success:
            success_count += 1
            n = success_count
        else:
            fail_count += 1
            n = success_count
        try:
            save_results(result_json)
        except Exception as e:
            log(f"[-] 写 {result_json} 失败: {e}")
        if success:
            avg = (time.time() - start_time) / max(n, 1)
            log(
                f"[✓] {n} | {email} | SSO: {(sso or '')[:18]}... | 平均 {avg:.1f}s"
            )


# --------------- worker ---------------

def reset_one(
    email: str,
    password: str,
    mail: MailAdmin,
    action_get_num: str,
    result_json: str,
) -> bool:
    if stop_event.is_set():
        return False

    time.sleep(random.uniform(0, 1.5))

    def fail(err: str) -> bool:
        log(f"[-] {email} {err}")
        record_result(
            email,
            success=False,
            password=password,
            error=err,
            result_json=result_json,
        )
        return False

    if not mail.ensure_address(email):
        return fail("绑定邮箱失败")

    session = None
    try:
        session, _imp = open_session()
    except Exception as e:
        return fail(f"会话失败: {e}")

    try:
        # 1) 触发重置（只发 1 封验证码，禁止再调 CreateEmailValidationCode）
        sent_at = time.time()
        if not trigger_reset_password(session, email):
            return fail("触发重置失败")

        # 2) 收验证码（取 sent_at 之后最新一封）
        code = mail.fetch_code(email, since_ts=sent_at)
        if not code:
            return fail("未收到验证码")
        log(f"[*] {email} 验证码: {code}")

        # 3) 校验验证码
        if not verify_code(session, email, code):
            # reset 接口也会校验；这里失败仍尝试一次，但记日志
            log(f"[!] {email} VerifyEmail 未通过，继续尝试设密")

        # 4) 设新密码，响应里直接取 sso JWT（不再 redeem，避免多余验证码邮件）
        num_links = get_num_one_time_links(session, email, action_get_num)
        sso_jwt = reset_password(session, email, code, password, num_links)
        if not sso_jwt:
            return fail("重置密码失败")

        record_result(
            email,
            success=True,
            sso=sso_jwt,
            password=password,
            result_json=result_json,
        )
        return True
    except Exception as e:
        return fail(f"异常: {e}")
    finally:
        if session is not None:
            try:
                session.close()
            except Exception:
                pass


def load_emails(path: str) -> list[str]:
    raw = Path(path).read_text(encoding="utf-8")
    data = json.loads(raw)
    if isinstance(data, list):
        emails = [str(x).strip() for x in data if str(x).strip()]
    elif isinstance(data, dict):
        emails = []
        for k, v in data.items():
            if isinstance(v, str) and "@" in v:
                emails.append(v.strip())
            elif "@" in str(k):
                emails.append(str(k).strip())
    else:
        raise ValueError("email.json 格式不支持")
    # 去重保序
    seen = set()
    out = []
    for e in emails:
        el = e.lower()
        if el not in seen and "@" in e:
            seen.add(el)
            out.append(e)
    return out


def main():
    parser = argparse.ArgumentParser(description="Grok 协议批量重置密码")
    parser.add_argument("-e", "--emails", default="email.json", help="邮箱列表 JSON")
    parser.add_argument("-c", "--concurrency", type=int, default=8, help="并发数，默认 8")
    parser.add_argument("-n", "--limit", type=int, default=0, help="只处理前 N 个，0=全部")
    parser.add_argument("--password", default="", help="覆盖 .env 的 ACCOUNT_PASSWORD")
    parser.add_argument("--offset", type=int, default=0, help="从第 N 个邮箱开始")
    parser.add_argument(
        "-o",
        "--output",
        default=RESULT_JSON,
        help="结果 JSON 路径，默认 email_sso.json",
    )
    parser.add_argument(
        "--no-skip-success",
        action="store_true",
        help="不跳过结果文件中已成功的邮箱（默认自动跳过）",
    )
    args = parser.parse_args()

    password = args.password or os.getenv("ACCOUNT_PASSWORD") or ""
    if not password:
        print("[-] 请在 .env 设置 ACCOUNT_PASSWORD，或使用 --password")
        return
    if len(password) < 8:
        print("[-] 密码至少 8 位")
        return

    emails = load_emails(args.emails)
    if args.offset:
        emails = emails[args.offset :]
    if args.limit and args.limit > 0:
        emails = emails[: args.limit]
    if not emails:
        print("[-] 没有待处理邮箱")
        return

    result_json = args.output or RESULT_JSON
    global _results
    _results = load_results(result_json)
    # 默认跳过结果文件中已成功的邮箱（success=true），无记录或失败的一律继续
    if not args.no_skip_success and _results:
        before = len(emails)
        emails = [
            e
            for e in emails
            if not (_results.get(e.lower()) or {}).get("success")
        ]
        print(f"[*] 跳过已成功: {before - len(emails)}，剩余 {len(emails)}")
        if not emails:
            print("[*] 全部已成功，退出")
            return

    try:
        mail = MailAdmin()
    except Exception as e:
        print(f"[-] 邮箱服务初始化失败: {e}")
        return

    print("=" * 60)
    print("Grok 协议重置密码")
    print("=" * 60)
    print(f"[*] 邮箱数: {len(emails)} | 并发: {args.concurrency}")
    print(f"[*] 新密码: {password[:2]}{'*' * max(0, len(password) - 2)}")
    print(f"[*] 结果文件: {result_json}")

    # 扫描 getNumOneTimeLinks 的 action id（不带 email，避免额外触发重置邮件）
    try:
        session, imp = open_session()
        print(f"[+] 会话就绪 (impersonate={imp})")
        action_get_num = discover_get_num_action(session)
        session.close()
        print(f"[+] getNumOneTimeLinks: {action_get_num}")
    except Exception as e:
        print(f"[-] 初始化失败: {e}")
        return

    workers = max(1, args.concurrency)
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [
            ex.submit(
                reset_one,
                email,
                password,
                mail,
                action_get_num,
                result_json,
            )
            for email in emails
        ]
        try:
            concurrent.futures.wait(futs)
        except KeyboardInterrupt:
            stop_event.set()
            print("\n[!] 中断，等待线程退出...")
            concurrent.futures.wait(futs, timeout=30)

    # 最终再落盘一次，保证完整
    try:
        with file_lock:
            save_results(result_json)
    except Exception as e:
        print(f"[-] 最终写 {result_json} 失败: {e}")

    print("=" * 60)
    print(f"[*] 完成: 成功 {success_count} / 失败 {fail_count} / 总计 {len(emails)}")
    print(f"[*] 耗时: {time.time() - start_time:.1f}s")
    print(f"[*] 结果: {result_json}")


if __name__ == "__main__":
    main()
