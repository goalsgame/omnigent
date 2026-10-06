"""Google Cloud KMS cipher contract with an isolated KMS API stand-in."""

from __future__ import annotations

from types import SimpleNamespace

import google_crc32c
import pytest
from google.api_core.exceptions import InvalidArgument, PermissionDenied

from omnigent.stores.credential_store.gcp_kms_cipher import (
    CREDENTIAL_GCP_KMS_KEY_ENV_VAR,
    GcpKmsSecretCipher,
)
from omnigent.stores.credential_store.secret_cipher import (
    CREDENTIAL_CIPHER_ENV_VAR,
    SecretCipher,
    build_secret_cipher,
)

KEY = "projects/test/locations/europe-west1/keyRings/omnigent/cryptoKeys/credentials"
ALICE = {"workspace_id": "0", "user_id": "alice", "provider": "github", "account_id": ""}
BOB = {**ALICE, "user_id": "bob"}


class FakeKms:
    def __init__(self) -> None:
        self.rows = {}
        self.calls = 0
        self.forbidden = False
        self.bad_integrity = False

    def encrypt(self, *, request):
        assert request["plaintext_crc32c"] == google_crc32c.value(request["plaintext"])
        assert request["additional_authenticated_data_crc32c"] == google_crc32c.value(
            request["additional_authenticated_data"]
        )
        self.calls += 1
        blob = f"cipher-{self.calls}".encode()
        self.rows[blob] = (request["plaintext"], request["additional_authenticated_data"])
        return SimpleNamespace(
            name=f"{request['name']}/cryptoKeyVersions/1",
            ciphertext=blob,
            ciphertext_crc32c=google_crc32c.value(blob),
            verified_plaintext_crc32c=not self.bad_integrity,
            verified_additional_authenticated_data_crc32c=True,
        )

    def decrypt(self, *, request):
        if self.forbidden:
            raise PermissionDenied("KMS permission denied")
        assert request["ciphertext_crc32c"] == google_crc32c.value(request["ciphertext"])
        assert request["additional_authenticated_data_crc32c"] == google_crc32c.value(
            request["additional_authenticated_data"]
        )
        entry = self.rows.get(request["ciphertext"])
        if entry is None or entry[1] != request["additional_authenticated_data"]:
            raise InvalidArgument("Decryption failed")
        return SimpleNamespace(
            plaintext=entry[0],
            plaintext_crc32c=google_crc32c.value(entry[0]),
        )


def test_row_binding_and_key_scope():
    api = FakeKms()
    cipher = GcpKmsSecretCipher(KEY, client=api)
    assert isinstance(cipher, SecretCipher)
    ciphertext = cipher.encrypt("ghu_token", context=ALICE)
    assert "ghu_token" not in ciphertext
    assert cipher.decrypt(ciphertext, context=ALICE) == "ghu_token"
    assert cipher.decrypt(ciphertext, context=BOB) is None
    assert cipher.decrypt(ciphertext, context={**ALICE, "account_id": "named"}) is None
    assert cipher.decrypt(ciphertext, context=dict(reversed(list(ALICE.items())))) == "ghu_token"
    assert cipher.decrypt("corrupt", context=ALICE) is None
    with pytest.raises(ValueError, match="differs"):
        GcpKmsSecretCipher(KEY + "-other", client=api).decrypt(ciphertext, context=ALICE)


def test_operational_and_integrity_failures_propagate():
    api = FakeKms()
    cipher = GcpKmsSecretCipher(KEY, client=api)
    ciphertext = cipher.encrypt("ghu_token", context=ALICE)
    api.forbidden = True
    with pytest.raises(PermissionDenied):
        cipher.decrypt(ciphertext, context=ALICE)
    api.bad_integrity = True
    with pytest.raises(RuntimeError, match="integrity"):
        cipher.encrypt("other", context=ALICE)
    assert api.calls == 3


def test_backend_selection(monkeypatch):
    monkeypatch.setenv(CREDENTIAL_GCP_KMS_KEY_ENV_VAR, KEY)
    monkeypatch.setenv(CREDENTIAL_CIPHER_ENV_VAR, "gcp_kms")
    assert isinstance(build_secret_cipher(), GcpKmsSecretCipher)
    monkeypatch.delenv(CREDENTIAL_CIPHER_ENV_VAR)
    assert isinstance(build_secret_cipher(), GcpKmsSecretCipher)
    monkeypatch.setenv(CREDENTIAL_GCP_KMS_KEY_ENV_VAR, "short-name")
    with pytest.raises(ValueError, match="full CryptoKey"):
        build_secret_cipher()
