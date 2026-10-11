import time

import jwt
import pytest

from vitality_bss import security
from vitality_bss.config import get_settings


def test_hash_and_verify():
    h = security.hash_password("Correct-Horse-9")
    assert h.startswith("$2")
    assert security.verify_password("Correct-Horse-9", h)
    assert not security.verify_password("wrong", h)
    assert not security.verify_password("x", "not-a-hash")


def test_long_passwords_are_not_truncated():
    base = "A1!" + "x" * 80
    h = security.hash_password(base)
    assert security.verify_password(base, h)
    assert not security.verify_password(base[:72], h)
    assert security._pw_bytes("a" * 72) == b"a" * 72
    assert len(security._pw_bytes("a" * 73)) == 64


def test_bcrypt_rounds_from_settings(set_env):
    set_env(BCRYPT_ROUNDS="5")
    assert security.hash_password("x").startswith("$2b$05$")


@pytest.mark.parametrize("pw", ["Short1!", "alllowercaseletters", "ALLUPPER12345", "nodigitsorsym bols"])
def test_password_policy_rejects(pw):
    with pytest.raises(security.PasswordPolicyError):
        security.check_password_policy(pw)


@pytest.mark.parametrize("pw", ["Str0ng-Passw0rd!", "lowerUPPER1234", "lower1234!!!!!", "UPPER-lower-xx"])
def test_password_policy_accepts(pw):
    security.check_password_policy(pw)


def test_password_policy_min_length_boundary():
    security.check_password_policy("Abcdefgh1234", min_length=12)
    with pytest.raises(security.PasswordPolicyError, match="at least 13"):
        security.check_password_policy("Abcdefgh1234", min_length=13)


def test_token_roundtrip():
    tok = security.create_access_token(7, 3)
    claims = security.decode_access_token(tok)
    assert claims == security.TokenClaims(user_id=7, token_version=3)
    payload = jwt.decode(tok, get_settings().SECRET_KEY, algorithms=["HS256"], issuer="vitality-bss")
    assert payload["exp"] - payload["iat"] == get_settings().ACCESS_TOKEN_EXPIRE_MINUTES * 60


def test_token_custom_expiry_and_expired():
    tok = security.create_access_token(1, 0, expires_minutes=0)
    time.sleep(1.1)
    assert security.decode_access_token(tok) is None


def test_token_rejects_tampering_and_wrong_key():
    tok = security.create_access_token(1, 0)
    assert security.decode_access_token(tok + "x") is None
    forged = jwt.encode({"sub": "1", "iat": 0, "exp": 9999999999, "iss": "vitality-bss"}, "o" * 40, algorithm="HS256")
    assert security.decode_access_token(forged) is None
    wrong_iss = jwt.encode({"sub": "1", "iat": 0, "exp": 9999999999, "iss": "x"},
                           get_settings().SECRET_KEY, algorithm="HS256")
    assert security.decode_access_token(wrong_iss) is None
    no_sub = jwt.encode({"iat": 0, "exp": 9999999999, "iss": "vitality-bss"},
                        get_settings().SECRET_KEY, algorithm="HS256")
    assert security.decode_access_token(no_sub) is None
    bad_sub = jwt.encode({"sub": "abc", "iat": 0, "exp": 9999999999, "iss": "vitality-bss"},
                         get_settings().SECRET_KEY, algorithm="HS256")
    assert security.decode_access_token(bad_sub) is None
    assert security.decode_access_token("garbage") is None


def test_token_without_version_defaults_zero():
    tok = jwt.encode({"sub": "4", "iat": 0, "exp": 9999999999, "iss": "vitality-bss"},
                     get_settings().SECRET_KEY, algorithm="HS256")
    assert security.decode_access_token(tok) == security.TokenClaims(4, 0)


def test_csrf():
    t = security.csrf_token_for("session-a")
    assert len(t) == 64
    assert t == security.csrf_token_for("session-a")
    assert t != security.csrf_token_for("session-b")
    assert security.verify_csrf("session-a", t)
    assert not security.verify_csrf("session-b", t)
    assert not security.verify_csrf("session-a", "")
    assert not security.verify_csrf("session-a", None)
