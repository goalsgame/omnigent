"""Google Cloud KMS encryption for per-user integration credentials."""

from __future__ import annotations

import base64
import json
import logging
import os
import re
from typing import Any

from omnigent.stores.credential_store.secret_cipher import SecretContext, _kms_context

_logger = logging.getLogger(__name__)
CREDENTIAL_GCP_KMS_KEY_ENV_VAR = "OMNIGENT_CREDENTIAL_GCP_KMS_KEY_ID"
_KEY_NAME = re.compile(r"projects/[^/]+/locations/[^/]+/keyRings/[^/]+/cryptoKeys/[^/]+\Z")
_PREFIX = "gcpkms"


def build_gcp_kms_secret_cipher() -> GcpKmsSecretCipher | None:
    """Build the GCP backend when its CryptoKey resource name is configured."""
    key_id = os.environ.get(CREDENTIAL_GCP_KMS_KEY_ENV_VAR, "").strip()
    return GcpKmsSecretCipher(key_id) if key_id else None


def _aad(context: SecretContext) -> bytes:
    return json.dumps(_kms_context(context), sort_keys=True, separators=(",", ":")).encode()


def _crc(data: bytes) -> int:
    try:
        import google_crc32c
    except ImportError as exc:  # pragma: no cover - import guard
        raise ImportError("The GCP KMS backend needs `pip install 'omnigent[gcp-kms]'`.") from exc
    return google_crc32c.value(data)


class GcpKmsSecretCipher:
    """Encrypt small credentials with Cloud KMS and row identity as authenticated data."""

    def __init__(self, key_id: str, *, client: Any | None = None) -> None:
        if not _KEY_NAME.fullmatch(key_id):
            raise ValueError("GCP KMS key must be a full CryptoKey resource name")
        self._key_id = key_id
        self._client = client

    @property
    def _kms(self) -> Any:
        if self._client is None:
            try:
                from google.cloud import kms_v1
            except ImportError as exc:  # pragma: no cover - import guard
                raise ImportError(
                    "The GCP KMS backend needs `pip install 'omnigent[gcp-kms]'`."
                ) from exc
            self._client = kms_v1.KeyManagementServiceClient()
        return self._client

    def encrypt(self, plaintext: str, *, context: SecretContext) -> str:
        data = plaintext.encode()
        aad = _aad(context)
        request = {
            "name": self._key_id,
            "plaintext": data,
            "additional_authenticated_data": aad,
            "plaintext_crc32c": _crc(data),
            "additional_authenticated_data_crc32c": _crc(aad),
        }
        for _ in range(2):
            response = self._kms.encrypt(request=request)
            if (
                response.name.startswith(f"{self._key_id}/cryptoKeyVersions/")
                and response.verified_plaintext_crc32c
                and response.verified_additional_authenticated_data_crc32c
                and response.ciphertext_crc32c == _crc(response.ciphertext)
            ):
                key = base64.urlsafe_b64encode(self._key_id.encode()).decode()
                blob = base64.b64encode(response.ciphertext).decode()
                return f"{_PREFIX}:{key}:{blob}"
        raise RuntimeError("GCP KMS encrypt response failed integrity verification")

    def decrypt(self, ciphertext: str, *, context: SecretContext) -> str | None:
        try:
            prefix, encoded_key, encoded_blob = ciphertext.split(":", 2)
            if prefix != _PREFIX:
                return None
            key = base64.b64decode(encoded_key, altchars=b"-_", validate=True).decode()
            blob = base64.b64decode(encoded_blob, validate=True)
        except (ValueError, UnicodeError):
            return None
        if key != self._key_id:
            raise ValueError("GCP KMS credential key differs from the configured key")
        aad = _aad(context)
        request = {
            "name": self._key_id,
            "ciphertext": blob,
            "additional_authenticated_data": aad,
            "ciphertext_crc32c": _crc(blob),
            "additional_authenticated_data_crc32c": _crc(aad),
        }
        from google.api_core.exceptions import InvalidArgument

        try:
            response = self._kms.decrypt(request=request)
        except InvalidArgument as exc:
            if "decrypt" not in str(exc).lower() or "checksum" in str(exc).lower():
                raise
            _logger.warning("GCP KMS could not decrypt a credential under its row identity")
            return None
        if response.plaintext_crc32c != _crc(response.plaintext):
            raise RuntimeError("GCP KMS decrypt response failed integrity verification")
        return response.plaintext.decode()
