"""Dispatcher de acoplamento — escolhe o motor certo e expõe a fachada única.

`cli.py` e os testes importam só este módulo (`from . import coupling as
cp_mod` / `from codescan import coupling as cp`) — nunca `coupling_generic`
ou `coupling_java*` diretamente. Isso mantém a troca de motor invisível para
quem consome o pacote.

Dois motores:
- `coupling_java.py`: motor especializado, ativado quando `has_java` detecta
  Java relevante no repo (via `surface["languages"]`, com fallback a uma
  varredura curta em disco). Grafo por CLASSE e por pacote, ciclos, classe-
  deus, hotspot de Ca, abstração especulativa — tudo com limiar estatístico
  (IQR) calculado a partir da distribuição real do projeto.
- `coupling_generic.py`: motor multi-linguagem (qualquer linguagem de
  `surface.py::LANGUAGES`), Ce/Ca/I/A/D/zona por MÓDULO do surface. É o
  fallback: repo sem Java, ou repo com Java mas onde nenhuma classe
  sobreviveu às exclusões da config (`summary["total_classes"] == 0`) — não
  faz sentido emitir um relatório Java vazio.

`analyze()` decide o motor e devolve sempre um dict com `"engine"` (`"java"`
ou `"generic"`). `render()`/`render_html()` despacham por esse campo — não
por reinspecionar o repo — então qualquer chamador que já tenha um
`analysis` em mãos (como os testes) não precisa saber como ele foi gerado.
`render_html` só existe para o motor Java (o genérico não tem view HTML);
para `engine == "generic"` devolve `None`.
"""

from __future__ import annotations

from . import coupling_generic
from . import coupling_java
from . import coupling_java_html
from . import coupling_java_md

# --- retrocompatibilidade -----------------------------------------------
# Re-exportação explícita (por nome, não `import *`) dos símbolos do motor
# genérico acessados fora deste módulo — hoje só pelos testes
# (`scripts/codescan/tests/test_coupling.py` usa `cp._extract_targets` e
# `cp._zone`), mas mantidos aqui como contrato público do dispatcher.
_extract_targets = coupling_generic._extract_targets
_zone = coupling_generic._zone

DEFAULT_CONFIG = coupling_java.DEFAULT_CONFIG


# --- API ------------------------------------------------------------------


def analyze(repo: str, surface: dict, config: dict | None = None) -> dict:
    """Java quando o repo tem Java; senão o motor genérico multi-linguagem.

    Se o motor Java rodar mas não sobrar nenhuma classe após as exclusões
    (`summary["total_classes"] == 0`), cai para o genérico em vez de emitir
    um relatório Java vazio.
    """
    if coupling_java.has_java(repo, surface):
        analysis = coupling_java.analyze(repo, surface, config=config)
        if analysis.get("summary", {}).get("total_classes", 0) > 0:
            return analysis
    analysis = coupling_generic.analyze(repo, surface)
    analysis["engine"] = "generic"
    return analysis


def render(surface: dict, analysis: dict, topic: str | None, now: str) -> str:
    """Despacha por `analysis["engine"]`; ausente (retrocompatibilidade dos
    testes existentes, que montam `analysis` à mão sem esse campo) cai no
    motor genérico."""
    if analysis.get("engine") == "java":
        return coupling_java_md.render(surface, analysis, topic, now)
    return coupling_generic.render(surface, analysis, topic, now)


def render_html(surface: dict, analysis: dict, now: str) -> str | None:
    """HTML interativo só existe para o motor Java; `None` para o genérico."""
    if analysis.get("engine") == "java":
        return coupling_java_html.render_html(surface, analysis, now)
    return None
