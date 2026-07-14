import os, json, random, string, time, re, struct
import threading
import concurrent.futures
from urllib.parse import urljoin, urlparse
from curl_cffi import requests
from bs4 import BeautifulSoup

from g import EmailService, TurnstileService, UserAgreementService, NsfwSettingsService

# 基础配置
site_url = "https://accounts.x.ai"
# Cloudflare 会拦截旧 TLS 指纹（chrome120 等返回 403），优先用较新版本
DEFAULT_IMPERSONATE = "chrome136"
IMPERSONATE_CANDIDATES = [
    "chrome136",
    "chrome133a",
    "chrome131",
    "chrome124",
    "chrome120",
    "safari18_0",
]
CHROME_PROFILES = [
    {"impersonate": "chrome136", "version": "136.0.0.0", "brand": "chrome"},
    {"impersonate": "chrome133a", "version": "133.0.0.0", "brand": "chrome"},
    {"impersonate": "chrome131", "version": "131.0.0.0", "brand": "chrome"},
    {"impersonate": "chrome124", "version": "124.0.0.0", "brand": "chrome"},
]
def get_random_chrome_profile():
    profile = random.choice(CHROME_PROFILES)
    if profile.get("brand") == "edge":
        chrome_major = profile["version"].split(".")[0]
        chrome_version = f"{chrome_major}.0.0.0"
        ua = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{chrome_version} Safari/537.36 Edg/{profile['version']}"
        )
    else:
        ua = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{profile['version']} Safari/537.36"
        )
    return profile["impersonate"], ua
PROXIES = {
    # "http": "http://127.0.0.1:10808",
    # "https": "http://127.0.0.1:10808"
}

# 动态获取的全局变量
config = {
    "site_key": "0x4AAAAAAAhr9JGVDZbrZOo0",
    "action_id": None,
    "state_tree": "%5B%22%22%2C%7B%22children%22%3A%5B%22(app)%22%2C%7B%22children%22%3A%5B%22(auth)%22%2C%7B%22children%22%3A%5B%22sign-up%22%2C%7B%22children%22%3A%5B%22__PAGE__%22%2C%7B%7D%2C%22%2Fsign-up%22%2C%22refresh%22%5D%7D%5D%7D%2Cnull%2Cnull%5D%7D%2Cnull%2Cnull%5D%7D%2Cnull%2Cnull%2Ctrue%5D"
}

post_lock = threading.Lock()
file_lock = threading.Lock()
success_count = 0
start_time = time.time()
target_count = 100
stop_event = threading.Event()
output_file = None

def generate_random_name() -> str:
    length = random.randint(4, 6)
    return random.choice(string.ascii_uppercase) + ''.join(random.choice(string.ascii_lowercase) for _ in range(length - 1))

def generate_random_string(length: int = 15) -> str:
    return "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(length))

def encode_grpc_message(field_id, string_value):
    key = (field_id << 3) | 2
    value_bytes = string_value.encode('utf-8')
    length = len(value_bytes)
    payload = struct.pack('B', key) + struct.pack('B', length) + value_bytes
    return b'\x00' + struct.pack('>I', len(payload)) + payload

def encode_grpc_message_verify(email, code):
    p1 = struct.pack('B', (1 << 3) | 2) + struct.pack('B', len(email)) + email.encode('utf-8')
    p2 = struct.pack('B', (2 << 3) | 2) + struct.pack('B', len(code)) + code.encode('utf-8')
    payload = p1 + p2
    return b'\x00' + struct.pack('>I', len(payload)) + payload

def send_email_code_grpc(session, email):
    url = f"{site_url}/auth_mgmt.AuthManagement/CreateEmailValidationCode"
    data = encode_grpc_message(1, email)
    headers = {"content-type": "application/grpc-web+proto", "x-grpc-web": "1", "x-user-agent": "connect-es/2.1.1", "origin": site_url, "referer": f"{site_url}/sign-up?redirect=grok-com"}
    try:
        # print(f"[debug] {email} 正在发送验证码请求...")
        res = session.post(url, data=data, headers=headers, timeout=15)
        # print(f"[debug] {email} 请求结束，状态码: {res.status_code}")
        return res.status_code == 200
    except Exception as e:
        print(f"[-] {email} 发送验证码异常: {e}")
        return False

def verify_email_code_grpc(session, email, code):
    url = f"{site_url}/auth_mgmt.AuthManagement/VerifyEmailValidationCode"
    data = encode_grpc_message_verify(email, code)
    headers = {"content-type": "application/grpc-web+proto", "x-grpc-web": "1", "x-user-agent": "connect-es/2.1.1", "origin": site_url, "referer": f"{site_url}/sign-up?redirect=grok-com"}
    try:
        res = session.post(url, data=data, headers=headers, timeout=15)
        # print(f"[debug] {email} 验证响应状态: {res.status_code}, 内容长度: {len(res.content)}")
        return res.status_code == 200
    except Exception as e:
        print(f"[-] {email} 验证验证码异常: {e}")
        return False

def register_single_thread():
    # 错峰启动，防止瞬时并发过高
    time.sleep(random.uniform(0, 5))

    try:
        email_service = EmailService()
        turnstile_service = TurnstileService()
        user_agreement_service = UserAgreementService()
        nsfw_service = NsfwSettingsService()
    except Exception as e:
        print(f"[-] 服务初始化失败: {e}")
        return

    # 修正：直接从 config 获取
    final_action_id = config["action_id"]
    if not final_action_id:
        print("[-] 线程退出：缺少 Action ID")
        return

    current_email = None  # 追踪当前邮箱，确保异常时能删除

    while True:
        try:
            if stop_event.is_set():
                if current_email:
                    try: email_service.delete_email(current_email)
                    except: pass
                return
            impersonate_fingerprint, account_user_agent = get_random_chrome_profile()
            with requests.Session(impersonate=impersonate_fingerprint, proxies=PROXIES) as session:
                # 预热连接
                try: session.get(site_url, timeout=10)
                except: pass

                password = generate_random_string()

                try:
                    jwt, email = email_service.create_email()
                    current_email = email
                except Exception as e:
                    print(f"[-] 邮箱服务抛出异常: {e}")
                    jwt, email, current_email = None, None, None

                if not email:
                    time.sleep(5); continue

                if stop_event.is_set():
                    email_service.delete_email(email)
                    current_email = None
                    return

                print(f"[*] 开始注册: {email}")

                # Step 1: 发送验证码
                if not send_email_code_grpc(session, email):
                    email_service.delete_email(email)
                    current_email = None
                    time.sleep(5); continue

                # Step 2: 获取验证码
                verify_code = email_service.fetch_verification_code(email)
                if not verify_code:
                    email_service.delete_email(email)
                    current_email = None
                    continue

                # Step 3: 验证验证码
                if not verify_email_code_grpc(session, email, verify_code):
                    email_service.delete_email(email)
                    current_email = None
                    continue

                # Step 4: 注册重试循环
                for attempt in range(3):
                    if stop_event.is_set():
                        email_service.delete_email(email)
                        current_email = None
                        return
                    task_id = turnstile_service.create_task(site_url, config["site_key"])
                    token = turnstile_service.get_response(task_id)

                    if not token or token == "CAPTCHA_FAIL":
                        continue

                    headers = {
                        "user-agent": account_user_agent, "accept": "text/x-component", "content-type": "text/plain;charset=UTF-8",
                        "origin": site_url, "referer": f"{site_url}/sign-up", "cookie": f"__cf_bm={session.cookies.get('__cf_bm','')}",
                        "next-router-state-tree": config["state_tree"], "next-action": final_action_id
                    }
                    payload = [{
                        "emailValidationCode": verify_code,
                        "createUserAndSessionRequest": {
                            "email": email, "givenName": generate_random_name(), "familyName": generate_random_name(),
                            "clearTextPassword": password, "tosAcceptedVersion": "$undefined"
                        },
                        "turnstileToken": token, "promptOnDuplicateEmail": True
                    }]

                    with post_lock:
                        res = session.post(f"{site_url}/sign-up", json=payload, headers=headers)

                    if res.status_code == 200:
                        match = re.search(r'(https://[^" \s]+set-cookie\?q=[^:" \s]+)1:', res.text)
                        if not match:
                            email_service.delete_email(email)
                            current_email = None
                            break
                        if match:
                            verify_url = match.group(1)
                            session.get(verify_url, allow_redirects=True)
                            sso = session.cookies.get("sso")
                            sso_rw = session.cookies.get("sso-rw")
                            if not sso:
                                email_service.delete_email(email)
                                current_email = None
                                break

                            tos_result = user_agreement_service.accept_tos_version(
                                sso=sso,
                                sso_rw=sso_rw or "",
                                impersonate=impersonate_fingerprint,
                                user_agent=account_user_agent,
                            )
                            tos_hex = tos_result.get("hex_reply") or ""
                            if not tos_result.get("ok") or not tos_hex:
                                email_service.delete_email(email)
                                current_email = None
                                break

                            nsfw_result = nsfw_service.enable_nsfw(
                                sso=sso,
                                sso_rw=sso_rw or "",
                                impersonate=impersonate_fingerprint,
                                user_agent=account_user_agent,
                            )
                            nsfw_hex = nsfw_result.get("hex_reply") or ""
                            if not nsfw_result.get("ok") or not nsfw_hex:
                                email_service.delete_email(email)
                                current_email = None
                                break

                            # 立即进行二次验证 (enable_unhinged)
                            unhinged_result = nsfw_service.enable_unhinged(sso)
                            unhinged_ok = unhinged_result.get("ok", False)

                            with file_lock:
                                global success_count
                                if success_count >= target_count:
                                    if not stop_event.is_set():
                                        stop_event.set()
                                    print(f"[*] 已达到目标数量，删除邮箱: {email}")
                                    email_service.delete_email(email)
                                    current_email = None
                                    break
                                try:
                                    with open(output_file, "a") as f: f.write(sso + "\n")
                                except Exception as write_err:
                                    print(f"[-] 写入文件失败: {write_err}")
                                    email_service.delete_email(email)
                                    current_email = None
                                    break
                                success_count += 1
                                avg = (time.time() - start_time) / success_count
                                nsfw_tag = "✓" if unhinged_ok else "✗"
                                print(f"[✓] 注册成功: {success_count}/{target_count} | {email} | SSO: {sso[:15]}... | 平均: {avg:.1f}s | NSFW: {nsfw_tag}")
                                email_service.delete_email(email)
                                current_email = None
                                if success_count >= target_count and not stop_event.is_set():
                                    stop_event.set()
                                    print(f"[*] 已达到目标数量: {success_count}/{target_count}，停止新注册")
                            break  # 跳出 for 循环，继续 while True 注册下一个

                    time.sleep(3)
                else:
                    # 如果重试 3 次都失败 (for 循环没有被 break)
                    email_service.delete_email(email)
                    current_email = None
                    time.sleep(5)

        except Exception as e:
            print(f"[-] 异常: {str(e)[:50]}")
            # 异常时确保删除邮箱
            if current_email:
                try:
                    email_service.delete_email(current_email)
                except:
                    pass
                current_email = None
            time.sleep(5)

def _extract_action_candidates(js_content: str):
    """从 Next.js chunk 提取 (action_id, name) 候选。"""
    patterns = [
        r'createServerReference\)\("([a-f0-9]{40,44})"[^)]*"([^"]*)"\)',
        r'createServerReference\("([a-f0-9]{40,44})"[^)]*"([^"]*)"\)',
    ]
    found = []
    for pat in patterns:
        found.extend(re.findall(pat, js_content))
    # 兼容旧版：以 7f 开头的 42 位 hex
    for m in re.findall(r'\b(7f[a-fA-F0-9]{40})\b', js_content):
        found.append((m, "legacy7f"))
    return found


def _pick_action_id(candidates):
    """优先 default（注册表单），其次 7f 前缀，跳过 getSession 等无关 action。"""
    if not candidates:
        return None
    skip = {"getsession", "session", "get_session"}
    usable = [(aid, name) for aid, name in candidates if name.lower() not in skip]
    if not usable:
        usable = candidates
    for aid, name in usable:
        if name == "default":
            return aid
    for aid, name in usable:
        if aid.startswith("7f") or name == "legacy7f":
            return aid
    return usable[0][0]


def _fetch_signup_html():
    """尝试多个 TLS 指纹拿到注册页 HTML（避免 Cloudflare 403）。"""
    last_err = None
    for imp in IMPERSONATE_CANDIDATES:
        try:
            with requests.Session(impersonate=imp) as s:
                res = s.get(f"{site_url}/sign-up", timeout=30)
                if res.status_code == 200 and "_next/static" in res.text:
                    return res.text, imp
                last_err = f"{imp} -> HTTP {res.status_code}"
        except Exception as e:
            last_err = f"{imp} -> {e}"
    raise RuntimeError(last_err or "无法访问注册页")


def main():
    print("=" * 60 + "\nGrok 注册机\n" + "=" * 60)
    
    # 1. 扫描参数
    print("[*] 正在初始化...")
    start_url = f"{site_url}/sign-up"
    try:
        html, used_imp = _fetch_signup_html()
        print(f"[+] 注册页可访问 (impersonate={used_imp})")

        # Key（HTML 或默认值）
        key_match = re.search(r'(0x4[A-Za-z0-9_-]{20,})', html)
        if key_match:
            config["site_key"] = key_match.group(1)
        # Tree
        tree_match = re.search(r'next-router-state-tree":"([^"]+)"', html)
        if tree_match:
            config["state_tree"] = tree_match.group(1)

        # Action ID：扫描 Next.js 静态脚本，收集全部候选后再挑选
        soup = BeautifulSoup(html, 'html.parser')
        js_urls = [
            urljoin(start_url, script['src'])
            for script in soup.find_all('script', src=True)
            if '_next/static' in script.get('src', '')
        ]
        candidates = []
        with requests.Session(impersonate=used_imp) as s:
            for js_url in js_urls:
                try:
                    js_content = s.get(js_url, timeout=30).text
                except Exception:
                    continue
                candidates.extend(_extract_action_candidates(js_content))
        config["action_id"] = _pick_action_id(candidates)
        if config["action_id"]:
            print(f"[+] Action ID: {config['action_id']}")
    except Exception as e:
        print(f"[-] 初始化扫描失败: {e}")
        return

    if not config["action_id"]:
        print("[-] 错误: 未找到 Action ID（可能页面被 Cloudflare 拦截，或前端结构已变更）")
        return

    # 2. 启动
    try:
        t = int(input("\n并发数 (默认8): ").strip() or 8)
    except: t = 8

    try:
        total = int(input("注册数量 (默认100): ").strip() or 100)
    except: total = 100

    global target_count, output_file
    target_count = max(1, total)

    from datetime import datetime
    os.makedirs("keys", exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_file = f"keys/grok_{timestamp}_{target_count}.txt"

    print(f"[*] 启动 {t} 个线程，目标 {target_count} 个")
    print(f"[*] 输出: {output_file}")
    with concurrent.futures.ThreadPoolExecutor(max_workers=t) as executor:
        futures = [executor.submit(register_single_thread) for _ in range(t)]
        concurrent.futures.wait(futures)

if __name__ == "__main__":
    main()