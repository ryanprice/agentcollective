"""
Inject Guard
------------
Multi-layer defence for the /inject endpoint.

Layer 1 — Rate limiting     : max 2 injections per 10s per IP
Layer 2 — Length cap        : 500 chars hard limit
Layer 3 — Content screening : regex blocklist for prompt-injection patterns
Layer 4 — Sanitization      : strip control chars, normalise whitespace
"""

import re
import time
import unicodedata
from collections import defaultdict, deque


# ── Rate limiter ──────────────────────────────────────────────────────────────
class RateLimiter:
    def __init__(self, max_calls: int = 2, window_secs: float = 10.0):
        self.max_calls   = max_calls
        self.window_secs = window_secs
        self._buckets: dict[str, deque] = defaultdict(deque)

    def allow(self, key: str) -> tuple[bool, float]:
        """Returns (allowed, retry_after_seconds)."""
        now    = time.monotonic()
        bucket = self._buckets[key]
        # Evict expired timestamps
        while bucket and now - bucket[0] > self.window_secs:
            bucket.popleft()
        if len(bucket) >= self.max_calls:
            retry_after = self.window_secs - (now - bucket[0])
            return False, round(retry_after, 1)
        bucket.append(now)
        return True, 0.0


# ── Prompt-injection patterns ─────────────────────────────────────────────────
# Each tuple: (pattern, human-readable reason)
_INJECTION_PATTERNS: list[tuple[re.Pattern, str]] = [
    # Instruction override attempts
    (re.compile(r'\bignore\s+(all\s+)?(previous|prior|above|your)\b', re.I), "instruction override"),
    (re.compile(r'\b(disregard|forget|override|bypass)\s+(your\s+)?(instruction|rule|guideline|constraint|system\s+prompt)', re.I), "instruction override"),
    (re.compile(r'\byou\s+are\s+now\b', re.I), "persona hijack"),
    (re.compile(r'\bnew\s+(instructions?|persona|role|directive|identity)\b', re.I), "persona hijack"),
    (re.compile(r'\bact\s+as\s+(if|a|an)\b', re.I), "persona hijack"),
    (re.compile(r'\bpretend\s+(you\s+are|to\s+be)\b', re.I), "persona hijack"),
    (re.compile(r'\byour\s+(true|real|actual|hidden)\s+(self|purpose|goal|directive)\b', re.I), "persona hijack"),
    # Fake system / authority tokens
    (re.compile(r'\[?\s*(SYSTEM|ADMIN|ROOT|DEVELOPER|ANTHROPIC|OPERATOR)\s*\]?\s*:', re.I), "fake authority token"),
    (re.compile(r'<\s*(system|instructions?|prompt|context)\s*>', re.I), "fake authority tag"),
    (re.compile(r'###\s*(system|instruction|override)', re.I), "fake authority header"),
    # Code execution triggers
    (re.compile(r'\brun(_script|_code)?\s*[:\(]', re.I), "code execution trigger"),
    (re.compile(r'\bexecute\s+(this\s+)?(code|script|command|python|bash)\b', re.I), "code execution trigger"),
    (re.compile(r'`{1,3}(python|bash|sh|shell|js|javascript)', re.I), "code block"),
    # Data exfiltration
    (re.compile(r'\b(curl|wget|nc|ncat|netcat|ssh|scp|rsync)\s', re.I), "network command"),
    (re.compile(r'/etc/(passwd|shadow|hosts|crontab|sudoers)', re.I), "sensitive file reference"),
    (re.compile(r'(api[_-]?key|secret[_-]?key|access[_-]?token|password|credential)', re.I), "credential keyword"),
    # Memory / context manipulation
    (re.compile(r'\b(clear|wipe|delete|erase|reset)\s+(your\s+)?(memory|context|history|conversation)\b', re.I), "memory manipulation"),
    (re.compile(r'\bdo\s+not\s+(log|record|remember|save|store)\b', re.I), "logging suppression"),
    # Unicode homoglyph / invisible char abuse
    (re.compile(r'[\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff]'), "hidden unicode characters"),
]

# ── Max length ────────────────────────────────────────────────────────────────
MAX_LENGTH = 500

# ── Singleton rate limiter ────────────────────────────────────────────────────
_rate_limiter = RateLimiter(max_calls=2, window_secs=10.0)


# ── Public API ────────────────────────────────────────────────────────────────
class GuardResult:
    def __init__(self, ok: bool, sanitized: str = "", reason: str = "", retry_after: float = 0):
        self.ok          = ok
        self.sanitized   = sanitized
        self.reason      = reason
        self.retry_after = retry_after


def check_inject(message: str, client_ip: str = "unknown") -> GuardResult:
    """
    Full guard pipeline. Returns GuardResult.
    Call .ok — if False, reject with .reason.
    If True, use .sanitized as the actual message.
    """

    # 1. Rate limit
    allowed, retry_after = _rate_limiter.allow(client_ip)
    if not allowed:
        return GuardResult(ok=False, reason=f"Rate limit exceeded. Try again in {retry_after}s.", retry_after=retry_after)

    # 2. Type check
    if not isinstance(message, str):
        return GuardResult(ok=False, reason="Message must be a string.")

    # 3. Strip and normalise unicode (NFC, remove control chars)
    message = unicodedata.normalize("NFC", message)
    message = "".join(ch for ch in message if unicodedata.category(ch)[0] != "C" or ch in "\n\t")

    # 4. Length cap — checked after normalisation
    if len(message) > MAX_LENGTH:
        return GuardResult(ok=False, reason=f"Message too long ({len(message)} chars). Max {MAX_LENGTH}.")

    if not message.strip():
        return GuardResult(ok=False, reason="Empty message.")

    # 5. Prompt-injection pattern scan
    for pattern, label in _INJECTION_PATTERNS:
        if pattern.search(message):
            return GuardResult(ok=False, reason=f"Message blocked: contains {label}.")

    # 6. Sanitize for context window injection
    #    - Collapse multiple newlines
    #    - Strip leading/trailing whitespace
    sanitized = re.sub(r'\n{3,}', '\n\n', message).strip()

    return GuardResult(ok=True, sanitized=sanitized)
