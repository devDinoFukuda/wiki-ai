"""Embeddings.

Azure OpenAI por padrão. A razão é o seu caso, não preferência: sua casa já é
Azure/M365, então o embedding fica no mesmo tenant — mesma garantia de dado que
o SharePoint — e você não hospeda modelo de 2GB, não gerencia VRAM, não baixa
GGUF. Nesse quesito é melhor que o QMD, que roda modelo local.

Sem configuração, o índice opera em modo LÉXICO. Isso é intencional: BM25
funciona no dia 1, embedding é upgrade. Mesma propriedade do QMD.

Env:
  AZURE_OPENAI_ENDPOINT    https://<recurso>.openai.azure.com
  AZURE_OPENAI_API_KEY
  AZURE_OPENAI_EMBED_DEPLOY   nome do deployment (ex.: text-embedding-3-small)
  AZURE_OPENAI_API_VERSION    default 2024-02-01
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

BATCH = 64
TIMEOUT = 60


class NoEmbedder:
    """Modo léxico. Não é erro — é o estado inicial."""

    available = False
    name = "none (lex-only)"

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise RuntimeError(
            "embeddings não configurados: defina AZURE_OPENAI_* ou use --lex-only"
        )


class AzureOpenAIEmbedder:
    available = True

    def __init__(self, endpoint, api_key, deployment, api_version="2024-02-01"):
        self.endpoint = endpoint.rstrip("/")
        self.api_key = api_key
        self.deployment = deployment
        self.api_version = api_version
        self.name = f"azure:{deployment}"

    def _url(self):
        return (
            f"{self.endpoint}/openai/deployments/{self.deployment}"
            f"/embeddings?api-version={self.api_version}"
        )

    def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), BATCH):
            batch = texts[i : i + BATCH]
            body = json.dumps({"input": batch}).encode()
            req = urllib.request.Request(
                self._url(),
                data=body,
                headers={"Content-Type": "application/json", "api-key": self.api_key},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                    data = json.loads(r.read())
            except urllib.error.HTTPError as e:
                raise RuntimeError(
                    f"Azure OpenAI {e.code}: {e.read().decode()[:300]}"
                ) from None
            out.extend(d["embedding"] for d in sorted(data["data"], key=lambda d: d["index"]))
        return out


def get_embedder():
    ep = os.getenv("AZURE_OPENAI_ENDPOINT")
    key = os.getenv("AZURE_OPENAI_API_KEY")
    dep = os.getenv("AZURE_OPENAI_EMBED_DEPLOY")
    if ep and key and dep:
        return AzureOpenAIEmbedder(
            ep, key, dep, os.getenv("AZURE_OPENAI_API_VERSION", "2024-02-01")
        )
    return NoEmbedder()
