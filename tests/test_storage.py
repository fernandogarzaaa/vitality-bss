import os
import stat

import pytest

from vitality_bss.storage import BlobStore, StorageError, generate_key, get_store


def test_plain_roundtrip(tmp_path):
    store = BlobStore(tmp_path / "s", [])
    assert not store.encrypts
    key, enc = store.put(b"%PDF-data")
    assert enc is False
    assert len(key) == 32
    assert (tmp_path / "s" / f"{key}.bin").read_bytes() == b"%PDF-data"
    assert store.get(key, False) == b"%PDF-data"
    assert stat.S_IMODE(os.stat(tmp_path / "s" / f"{key}.bin").st_mode) == 0o600
    assert not list((tmp_path / "s").glob("*.tmp"))


def test_encrypted_roundtrip_and_ciphertext_on_disk(tmp_path):
    store = BlobStore(tmp_path, [generate_key()])
    assert store.encrypts
    key, enc = store.put(b"%PDF-secret PT-1001")
    assert enc is True
    raw = (tmp_path / f"{key}.bin").read_bytes()
    assert b"PT-1001" not in raw
    assert store.get(key, True) == b"%PDF-secret PT-1001"


def test_unique_keys(tmp_path):
    store = BlobStore(tmp_path, [])
    assert store.put(b"a")[0] != store.put(b"a")[0]


def test_invalid_key_rejected(tmp_path):
    store = BlobStore(tmp_path, [])
    for bad in ("../etc/passwd", "abc", "Z" * 32, ""):
        with pytest.raises(StorageError, match="invalid storage key"):
            store.get(bad, False)


def test_missing_blob(tmp_path):
    with pytest.raises(StorageError, match="missing"):
        BlobStore(tmp_path, []).get("a" * 32, False)


def test_encrypted_without_keys(tmp_path):
    key, _ = BlobStore(tmp_path, [generate_key()]).put(b"x")
    with pytest.raises(StorageError, match="not configured"):
        BlobStore(tmp_path, []).get(key, True)


def test_wrong_key(tmp_path):
    key, _ = BlobStore(tmp_path, [generate_key()]).put(b"x")
    with pytest.raises(StorageError, match="cannot be decrypted"):
        BlobStore(tmp_path, [generate_key()]).get(key, True)


def test_invalid_fernet_key(tmp_path):
    with pytest.raises(StorageError, match="invalid Fernet key"):
        BlobStore(tmp_path, ["not-a-key"])


def test_delete(tmp_path):
    store = BlobStore(tmp_path, [])
    key, _ = store.put(b"x")
    store.delete(key)
    assert not (tmp_path / f"{key}.bin").exists()
    store.delete(key)  # idempotent


def test_rotation(tmp_path):
    old, new = generate_key(), generate_key()
    key, _ = BlobStore(tmp_path, [old]).put(b"payload")
    rotated = BlobStore(tmp_path, [new, old])
    assert rotated.rotate(key, True) is True
    assert BlobStore(tmp_path, [new]).get(key, True) == b"payload"


def test_rotation_encrypts_plaintext(tmp_path):
    key, _ = BlobStore(tmp_path, []).put(b"plain")
    k = generate_key()
    assert BlobStore(tmp_path, [k]).rotate(key, False) is True
    assert b"plain" not in (tmp_path / f"{key}.bin").read_bytes()
    assert BlobStore(tmp_path, [k]).get(key, True) == b"plain"


def test_rotation_errors(tmp_path):
    key, _ = BlobStore(tmp_path, [generate_key()]).put(b"x")
    with pytest.raises(StorageError, match="requires"):
        BlobStore(tmp_path, []).rotate(key, True)
    with pytest.raises(StorageError, match="cannot be decrypted"):
        BlobStore(tmp_path, [generate_key()]).rotate(key, True)


def test_get_store_uses_settings(env, set_env):
    k = generate_key()
    set_env(ENCRYPTION_KEYS=f" {k} , ")
    store = get_store()
    assert store.encrypts
    assert store.root == env / "data" / "uploads"
    assert store.keys == [k]
