"""脱敏与注入安全。

两个不同层次：

1. :func:`redact_secrets` —— **首次落盘前**执行，所有 scope 一视同仁，覆盖密码、token、
   密钥、证件号、手机号、银行卡。之后任何渲染都无法撤回已落盘的秘密。
2. :func:`cross_domain_redact` —— 仅在 group/public 渲染时执行，覆盖 QQ 号、群号、链接等
   跨域身份信息。
3. :func:`sanitize_for_injection` —— 进入 prompt 前剥离角色控制标记与容器结束标记。
"""

from __future__ import annotations

import re

from .common import sha256_hex

_REDACTION_TOKEN = "[已脱敏:{kind}]"

_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.DOTALL,
)
_SECRET_ASSIGN_RE = re.compile(
    r"(?i)\b(password|passwd|pwd|pass|token|access[_-]?token|refresh[_-]?token|"
    r"secret|api[_-]?key|apikey|app[_-]?secret|client[_-]?secret|authorization|"
    r"session[_-]?id|cookie)\b"
    r"(\s*[:=：]\s*)"
    r"(\"[^\"]{1,200}\"|'[^']{1,200}'|[^\s,，;；]{1,200})"
)
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-+/=]{8,}")
_ID_CARD_RE = re.compile(r"(?<!\d)(?:\d{17}[\dXx]|\d{15})(?!\d)")
_PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_BANK_CARD_RE = re.compile(r"(?<!\d)\d{16,19}(?!\d)")
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")
_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_DIGITS_RE = re.compile(r"(?<!\d)\d{5,12}(?!\d)")

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_ZERO_WIDTH_RE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060\ufeff]")
_MARKER_RE = re.compile(r"</?\s*(?:persona[_\- ]?data|persona[_\- ]?memory|system|developer|tool)\b[^>]*>", re.IGNORECASE)
_SPECIAL_TOKEN_RE = re.compile(r"<\|[^|>]{0,40}\|>|\[/?(?:INST|SYS)\]|<</?SYS>>", re.IGNORECASE)
_ROLE_PREFIX_RE = re.compile(r"(?im)^\s*(system|assistant|developer|tool|用户|系统)\s*[:：]\s*")
_CONTAINER_CLOSE_RE = re.compile(r"</?\s*persona[_\- ]?(?:data|memory)\b[^>]*>", re.IGNORECASE)


def redact_secrets(text: str) -> tuple[str, dict[str, int]]:
    """对高敏内容做确定性脱敏，返回 ``(脱敏文本, 各类型命中次数)``。"""

    if not isinstance(text, str) or not text:
        return "", {}

    hits: dict[str, int] = {}

    def replace(kind: str, match: re.Match[str], group: int = 0) -> str:
        hits[kind] = hits.get(kind, 0) + 1
        if group == 0:
            return _REDACTION_TOKEN.format(kind=kind)
        prefix = match.group(1)
        sep = match.group(2)
        return f"{prefix}{sep}{_REDACTION_TOKEN.format(kind=kind)}"

    text = _PRIVATE_KEY_RE.sub(lambda m: replace("私钥", m), text)
    text = _BEARER_RE.sub(lambda m: replace("Token", m), text)
    text = _SECRET_ASSIGN_RE.sub(lambda m: replace("凭据", m, group=1), text)
    # 顺序很重要：先证件号、手机号，再银行卡，避免 18 位身份证被当成银行卡重复替换。
    text = _ID_CARD_RE.sub(lambda m: replace("证件号", m), text)
    text = _PHONE_RE.sub(lambda m: replace("手机号", m), text)
    text = _BANK_CARD_RE.sub(lambda m: replace("银行卡", m), text)
    text = _EMAIL_RE.sub(lambda m: replace("邮箱", m), text)
    return text, hits


def cross_domain_redact(text: str, *, known_ids: list[str] | None = None) -> str:
    """群/公开渲染时抹掉跨域身份信息与链接。"""

    if not isinstance(text, str) or not text:
        return ""
    result = text
    for known in sorted({item for item in (known_ids or []) if item}, key=len, reverse=True):
        result = result.replace(known, "[身份ID]")
    result = _URL_RE.sub("[链接]", result)
    result = _EMAIL_RE.sub("[邮箱]", result)
    result = _DIGITS_RE.sub("[数字ID]", result)
    return result


def sanitize_for_injection(text: str) -> str:
    """剥离角色控制标记、特殊 token 与容器标记，防止内容逃逸数据区。"""

    if not isinstance(text, str):
        return ""
    result = _CONTROL_RE.sub("", text)
    result = _ZERO_WIDTH_RE.sub("", result)
    result = _CONTAINER_CLOSE_RE.sub("", result)
    result = _MARKER_RE.sub("", result)
    result = _SPECIAL_TOKEN_RE.sub("", result)
    result = _ROLE_PREFIX_RE.sub("", result)
    return result.strip()


def normalize_text(text: object, *, max_chars: int = 8000) -> str:
    """规范化待存储文本：统一换行、去控制字符、限制长度。"""

    if not isinstance(text, str):
        return ""
    result = text.replace("\r\n", "\n").replace("\r", "\n")
    result = _CONTROL_RE.sub("", result)
    result = _ZERO_WIDTH_RE.sub("", result)
    result = "\n".join(line.rstrip() for line in result.split("\n")).strip()
    if max_chars > 0 and len(result) > max_chars:
        result = result[: max_chars - 1] + "…"
    return result


def fingerprint(text: str) -> str:
    """返回文本指纹，用于去重与审计（不保留原文）。"""

    return sha256_hex(text)[:32]
