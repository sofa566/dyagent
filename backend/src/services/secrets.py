"""
Secrets provider 介面與預設實作。

目的：
- 以引用（ref）載入金鑰，避免在 DB 存放明文。
- 之後可替換為 Vault/KMS 等實作。
"""

from __future__ import annotations

from typing import Optional, Protocol
import os
from src.core.config import settings
from src.core.logging import get_logger


class SecretsProvider(Protocol):
    def get(self, ref: str) -> Optional[str]:
        ...


class EnvSecretsProvider:
    """從環境變數載入祕密的預設實作。"""

    def get(self, ref: str) -> Optional[str]:
        try:
            return os.environ.get(ref) or None
        except Exception:
            return None


class VaultSecretsProvider:
    """占位實作：優先讀取環境變數 "VAULT__{ref}"，否則回 None。
    真正接 Vault 可在此處接 SDK。
    """

    def get(self, ref: str) -> Optional[str]:
        return os.environ.get(f"VAULT__{ref}") or None


class AWSSecretsProvider:
    """占位實作：優先讀取環境變數 "AWS__{ref}"。"""

    def get(self, ref: str) -> Optional[str]:
        return os.environ.get(f"AWS__{ref}") or None


class GCPSecretsProvider:
    """占位實作：優先讀取環境變數 "GCP__{ref}"。"""

    def get(self, ref: str) -> Optional[str]:
        return os.environ.get(f"GCP__{ref}") or None


class K8sSecretsProvider:
    """占位實作：優先讀取環境變數 "K8S__{ref}"。"""

    def get(self, ref: str) -> Optional[str]:
        return os.environ.get(f"K8S__{ref}") or None


def get_secrets_provider() -> SecretsProvider:
    log = get_logger("secrets")
    prov = (settings.SECRETS_PROVIDER or "env").lower()
    if prov == "env":
        log.info("secrets.provider", provider="env")
        return EnvSecretsProvider()
    if prov == "vault":
        log.info("secrets.provider", provider="vault", addr=bool(settings.VAULT_ADDR))
        return VaultSecretsProvider()
    if prov == "aws":
        log.info("secrets.provider", provider="aws", region=settings.AWS_REGION or "")
        return AWSSecretsProvider()
    if prov == "gcp":
        log.info("secrets.provider", provider="gcp", project=settings.GCP_PROJECT_ID or "")
        return GCPSecretsProvider()
    if prov == "k8s":
        log.info("secrets.provider", provider="k8s", namespace=settings.K8S_NAMESPACE or "")
        return K8sSecretsProvider()
    log.warning("secrets.provider.unknown", provider=prov)
    return EnvSecretsProvider()
