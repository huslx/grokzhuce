"""邮箱服务类 - 适配 cloudflare_temp_email"""
import os
import re
import time
import string
import random
import requests
from dotenv import load_dotenv


def _looks_like_date(digits: str) -> bool:
    if not digits or not digits.isdigit():
        return False
    if len(digits) == 4:
        n = int(digits)
        if 1900 <= n <= 2099:
            return True
    if len(digits) == 8:
        year = int(digits[:4])
        month = int(digits[4:6])
        day = int(digits[6:8])
        if 1900 <= year <= 2099 and 1 <= month <= 12 and 1 <= day <= 31:
            return True
    return False


def _normalize_mail_text(text: str) -> str:
    """HTML 粗剥离，便于从邮件正文抽码。"""
    if not text:
        return ""
    # 明显是 HTML 时去掉标签
    if "<" in text and ">" in text:
        text = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", text)
        text = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", text)
        text = re.sub(r"(?s)<[^>]+>", " ", text)
        text = re.sub(r"&nbsp;", " ", text)
        text = re.sub(r"&amp;", "&", text)
        text = re.sub(r"&lt;", "<", text)
        text = re.sub(r"&gt;", ">", text)
    return re.sub(r"\s+", " ", text).strip()


def extract_verification_code(text: str):
    """从邮件主题/正文提取验证码。

    兼容：
    - 通用 verification code / 验证码 格式
    - xAI: 主题 `UTF-6PW xAI confirmation code`，正文 code 在 confirm 关键词前
    """
    if not text:
        return None

    raw = text.strip()
    plain = _normalize_mail_text(text)

    # xAI 主题/正文: "{CODE} xAI confirmation code"
    for candidate in (raw, plain):
        m = re.search(
            r"(?i)\b([A-Z0-9][A-Z0-9-]{3,11})\s+xAI\s+confirmation\s+code\b",
            candidate,
        )
        if m:
            return m.group(1)

    # xAI HTML: 大号加粗验证码
    m = re.search(
        r"(?is)font-weight\s*:\s*bold[^>]*>\s*([A-Za-z0-9][A-Za-z0-9-]{3,11})\s*<",
        raw,
    )
    if m and re.search(r"\d", m.group(1)):
        return m.group(1)

    # xAI 正文: 在 "code below / email address" 后找带数字的短 token（如 UTF-6PW）
    for m in re.finditer(
        r"(?i)(?:use the code below|validate your email address)(.{0,160})",
        plain,
    ):
        window = m.group(1)
        for token in re.findall(r"\b([A-Za-z0-9][A-Za-z0-9-]{3,11})\b", window):
            if not re.search(r"\d", token):
                continue
            if _looks_like_date(token.replace("-", "")):
                continue
            if token.lower() in {"address", "email", "below", "please", "thank", "create", "2026"}:
                continue
            return token

    delim = r"\s*(?:[:：]|\bis\b|是|为|です)[\s:：]*"
    cn_ja_ko_kw = r"验证码|认证码|确认码|認証コード|인증\s*코드|코드"
    en_kw = r"verification\s*code|confirm(?:ation)?\s*code|security\s*code|passcode|OTP|pin\s*code"
    all_kw = f"{cn_ja_ko_kw}|{en_kw}"

    keyword_patterns = [
        re.compile(rf"\bcode{delim}(\d{{4,12}})\b", re.I),
        re.compile(rf"(?:{all_kw}){delim}(\d{{4,12}})\b", re.I),
        re.compile(rf"\bcode{delim}([A-Za-z0-9-]{{4,12}})\b", re.I),
        re.compile(rf"(?:{all_kw}){delim}([A-Za-z0-9-]{{4,12}})\b", re.I),
    ]

    for source in (plain, raw):
        for pattern in keyword_patterns:
            match = pattern.search(source)
            if match and match.group(1) and not _looks_like_date(match.group(1).replace("-", "")):
                return match.group(1)

    standalone = re.search(r"(?:^|\s)(\d{4,12})(?:\s|$|\.|,)", plain, re.M)
    if standalone and standalone.group(1) and not _looks_like_date(standalone.group(1)):
        return standalone.group(1)

    return None


class EmailService:
    def __init__(self):
        load_dotenv()
        self.base_url = (
            os.getenv("MAIL_BASE_URL")
            or os.getenv("WORKER_DOMAIN")
            or ""
        ).rstrip("/")
        if self.base_url and not self.base_url.startswith("http"):
            self.base_url = f"https://{self.base_url}"

        self.admin_password = (
            os.getenv("MAIL_ADMIN_PASSWORD")
            or os.getenv("ADMIN_PASSWORD")
            or os.getenv("FREEMAIL_TOKEN")
        )
        self.domain = os.getenv("MAIL_DOMAIN", "").strip()
        self.site_password = os.getenv("MAIL_SITE_PASSWORD", "").strip()

        if not self.base_url:
            raise ValueError("Missing: MAIL_BASE_URL (or WORKER_DOMAIN)")
        if not self.admin_password:
            raise ValueError("Missing: MAIL_ADMIN_PASSWORD (or ADMIN_PASSWORD)")
        if not self.domain:
            raise ValueError("Missing: MAIL_DOMAIN")

        self.admin_headers = {
            "x-admin-auth": self.admin_password,
            "Content-Type": "application/json",
            "x-lang": "zh",
        }
        if self.site_password:
            self.admin_headers["x-custom-auth"] = self.site_password

        # address -> {jwt, address_id}
        self._mailboxes = {}

    def _auth_headers(self, jwt: str) -> dict:
        headers = {
            "Authorization": f"Bearer {jwt}",
            "x-lang": "zh",
        }
        if self.site_password:
            headers["x-custom-auth"] = self.site_password
        return headers

    @staticmethod
    def _random_name(length: int = 12) -> str:
        alphabet = string.ascii_lowercase + string.digits
        return "".join(random.choices(alphabet, k=length))

    def create_email(self):
        """创建临时邮箱 POST /admin/new_address"""
        try:
            res = requests.post(
                f"{self.base_url}/admin/new_address",
                headers=self.admin_headers,
                json={
                    "name": self._random_name(),
                    "domain": self.domain,
                    "enablePrefix": False,
                },
                timeout=15,
            )
            if res.status_code != 200:
                print(f"[-] 创建邮箱失败: {res.status_code} - {res.text}")
                return None, None

            data = res.json()
            email = data.get("address")
            jwt = data.get("jwt")
            address_id = data.get("address_id")
            if not email or not jwt:
                print(f"[-] 创建邮箱失败: 响应缺少 address/jwt - {data}")
                return None, None

            self._mailboxes[email] = {
                "jwt": jwt,
                "address_id": address_id,
            }
            return jwt, email
        except Exception as e:
            print(f"[-] 创建邮箱失败: {e}")
            return None, None

    def _extract_code_from_mail(self, mail: dict):
        # subject 优先：xAI 把验证码放在主题最前面
        for field in ("subject", "text", "html"):
            code = extract_verification_code(mail.get(field) or "")
            if code:
                return code.replace("-", "")
        # 组合字段再试一次，避免 code 跨字段
        combined = "\n".join(
            str(mail.get(f) or "") for f in ("subject", "text", "html")
        )
        code = extract_verification_code(combined)
        return code.replace("-", "") if code else None

    def fetch_verification_code(self, email, max_attempts=40):
        """轮询收件箱获取验证码 GET /api/parsed_mails"""
        box = self._mailboxes.get(email)
        if not box or not box.get("jwt"):
            print(f"[-] 无法获取验证码: 未找到邮箱 JWT ({email})")
            return None

        headers = self._auth_headers(box["jwt"])
        interval = 2

        for attempt in range(max_attempts):
            try:
                res = requests.get(
                    f"{self.base_url}/api/parsed_mails",
                    params={"limit": 10, "offset": 0},
                    headers=headers,
                    timeout=15,
                )
                if res.status_code == 429:
                    time.sleep(min(interval * 2, 10))
                    interval = min(interval * 2, 10)
                    continue
                if res.status_code == 200:
                    payload = res.json()
                    results = payload.get("results") or []
                    for mail in results:
                        code = self._extract_code_from_mail(mail)
                        if code:
                            return code
            except Exception:
                pass

            time.sleep(interval)
            # 轻微退避，避免打爆 API
            if attempt > 0 and attempt % 5 == 0:
                interval = min(interval + 1, 5)

        return None

    def delete_email(self, address):
        """删除邮箱 DELETE /api/delete_address（优先用地址 JWT）"""
        if not address:
            return False

        box = self._mailboxes.pop(address, None)
        try:
            if box and box.get("jwt"):
                res = requests.delete(
                    f"{self.base_url}/api/delete_address",
                    headers=self._auth_headers(box["jwt"]),
                    timeout=15,
                )
                if res.status_code == 200 and res.json().get("success"):
                    return True

            # 回退：admin 按 id 删除
            address_id = (box or {}).get("address_id")
            if not address_id:
                address_id = self._lookup_address_id(address)
            if not address_id:
                return False

            res = requests.delete(
                f"{self.base_url}/admin/delete_address/{address_id}",
                headers=self.admin_headers,
                timeout=15,
            )
            return res.status_code == 200 and res.json().get("success")
        except Exception:
            return False

    def _lookup_address_id(self, address: str):
        try:
            res = requests.get(
                f"{self.base_url}/admin/address",
                params={"limit": 20, "offset": 0, "query": address},
                headers=self.admin_headers,
                timeout=15,
            )
            if res.status_code != 200:
                return None
            results = (res.json() or {}).get("results") or []
            for row in results:
                if row.get("name") == address:
                    return row.get("id")
        except Exception:
            return None
        return None
