# 统一的 Headers 配置 - 必须包含 CSRF 保护和设备指纹

default_headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
    "Content-Type": "application/json",
    "Accept": "application/json",
    "Origin": "https://app.mercury.com",
    "Referer": "https://app.mercury.com/",
    "X-CSRF-PROTECT": "1",  # 必须开启
    "X-Device-Fingerprint": "d220bf0969b6628e012691593339e0e2",  # 如果报错，需更新此指纹
    "X-Timezone-Offset": "-6:00",
    "X-Timezone-IANA": "America/Chicago"
}

# 向后兼容
headers = default_headers

