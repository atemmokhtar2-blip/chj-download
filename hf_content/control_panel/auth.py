import base64
import hashlib
import hmac
import time
from fastapi import Request, HTTPException
from fastapi.responses import RedirectResponse
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from .config import BOT_TOKEN, OWNER_ID, SECRET_KEY

# ── Permanent access token (deterministic from SECRET_KEY + OWNER_ID) ──────
_tok_raw = hmac.new(
    SECRET_KEY.encode(),
    f"titanx-panel-owner-{OWNER_ID}-v1".encode(),
    hashlib.sha256,
).digest()
ACCESS_TOKEN: str = base64.urlsafe_b64encode(_tok_raw)[:24].decode()

_signer = URLSafeTimedSerializer(SECRET_KEY)
SESSION_COOKIE = "titanx_session"
SESSION_MAX_AGE = 86400 * 7  # 7 days


def verify_telegram_hash(data: dict) -> bool:
    """Verify Telegram Login Widget HMAC."""
    received_hash = data.get("hash", "")
    if not received_hash:
        return False
    check_data = {k: v for k, v in data.items() if k != "hash"}
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(check_data.items()))
    secret = hashlib.sha256(BOT_TOKEN.encode()).digest()
    expected = hmac.new(secret, data_check_string.encode(), hashlib.sha256).hexdigest()

    # Check auth_date freshness (within 24h)
    try:
        auth_date = int(data.get("auth_date", 0))
        if time.time() - auth_date > 86400:
            return False
    except (ValueError, TypeError):
        return False

    return hmac.compare_digest(expected, received_hash)


def create_session(user_id: int) -> str:
    return _signer.dumps({"uid": user_id, "ts": int(time.time())})


def decode_session(token: str) -> dict | None:
    try:
        return _signer.loads(token, max_age=SESSION_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None


def get_session(request: Request) -> dict | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    return decode_session(token)


class NotAuthenticated(Exception):
    pass


def require_owner(request: Request) -> dict:
    """FastAPI dependency — raises NotAuthenticated if not owner."""
    session = get_session(request)
    if not session:
        raise NotAuthenticated()
    if session.get("uid") != OWNER_ID:
        raise HTTPException(status_code=403, detail="⛔ وصول مرفوض — هذا اللوحة للمالك فقط.")
    return session


def get_optional_session(request: Request) -> dict | None:
    return get_session(request)
