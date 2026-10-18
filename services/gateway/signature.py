import hashlib
import hmac

PREFIX = "sha256="


def sign(secret: str, body: bytes) -> str:
    """The value GitHub puts in X-Hub-Signature-256."""
    return PREFIX + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def is_valid(secret: str, body: bytes, header: str | None) -> bool:
    if not secret or not header or not header.startswith(PREFIX):
        return False
    # compare_digest takes the same time whether the first or last byte differs (no timing leak)
    return hmac.compare_digest(sign(secret, body), header)
