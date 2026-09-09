"""Perfil de análise por sistema: alavancas de flexibilização já existentes, ligadas.

O pipeline determinístico (`snapshot` -> `inventory` -> `extractors` ->
`capabilities` -> `investigation`) nasceu com um único regime: escopo por
prefixo, 13 campos de contrato obrigatórios, 40 células de matriz obrigatórias,
prioridade de gatilho fixa, 9 extratores fixos e nenhum orçamento vindo de
fora. Para repositórios diferentes isso derruba a QUALIDADE por dois caminhos
opostos: exige o que não se aplica (um `.sql` de migração não tem "mensageria")
e não deixa exigir o que se aplica (uma família de falha específica do sistema).

Este módulo é o **único** lugar onde essas alavancas viram dado. Ele não decide
nada sozinho: produz um `AnalysisProfile` imutável, validado, que os pontos
reais do pipeline aceitam por parâmetro *keyword* opcional. Com
`DEFAULT_PROFILE` (ou `None`), cada ponto se comporta exatamente como antes —
mesmos ids, mesmos hashes.

Três regras que o código aplica, não promete:

1. **Nenhuma exclusão silenciosa.** Excluir campo do contrato ou família da
   matriz exige `motivo` não vazio (`ProfileError` caso contrário), e o motivo
   viaja no payload do objetivo (`contract[campo]["motivo"]`,
   `matrix.cells[*]["note"]` + `justification_source`).
2. **Nenhuma mutação global.** `trigger_priority` sobrescreve a prioridade *por
   chamada* de `plan()`; `extra_excluded_dirs` compõe uma cópia local de
   `_EXCLUDED_DIR_NAMES`; `extractors` registra num `ExtractorRegistry` novo.
3. **Precedência declarada.** `resolve_profile()` mescla repo < store < cli e
   guarda a proveniência de cada camada em `source`, que sai em `summary()`.

Sem dependências de outros módulos de `analysis` no topo: `snapshot.py` e
`inventory.py` importam daqui, e `investigation.py` importa `snapshot`. As
constantes de `investigation` (CONTRACT_FIELDS, FAILURE_FAMILIES,
ReadingTrigger) são lidas por import tardio dentro das funções de validação.
"""

from __future__ import annotations

import fnmatch
import json
import os
import unicodedata
from dataclasses import dataclass, fields as _dc_fields, replace as _dc_replace
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "AnalysisProfile",
    "BUDGET_KEYS",
    "CAPABILITY_KEYS",
    "DEFAULT_PROFILE",
    "DISCOVERY_MAX_FILES_PER_OBJECTIVE",
    "POLICY_KEYS",
    "PROFILE_KEYS",
    "REPO_CONFIG_FILENAME",
    "REPO_LAYER_KEYS",
    "ProfileError",
    "is_repo_layer",
    "load_repo_config",
    "matches_any",
    "normalize_pattern",
    "resolve_profile",
    "validate_capability_tuning",
]


class ProfileError(ValueError):
    """Perfil de análise inválido: chave, tipo, faixa ou exclusão sem motivo."""


REPO_CONFIG_FILENAME = ".wiki-ai.json"

#: Teto EFETIVO de arquivos por objetivo de descoberta quando nenhuma camada
#: declara `discovery_max_files_per_objective`. O campo do perfil é
#: `int | None` (e não `int` com default 12) para que `None` continue
#: significando "nenhuma camada decidiu": é o que faz `merge()` herdar o valor
#: da camada de baixo e `to_dict()`/`is_default()` não emitirem a chave num
#: perfil que nunca a declarou.
DISCOVERY_MAX_FILES_PER_OBJECTIVE: int = 12

#: Chaves aceitas em `budget` (orçamento de pacote do runtime).
BUDGET_KEYS: tuple[str, ...] = (
    "max_bytes",
    "max_tokens",
    "overhead_bytes",
    "output_reserve_tokens",
)

#: Chaves aceitas em `policy` (política de execução do runtime).
POLICY_KEYS: tuple[str, ...] = ("max_bytes", "max_tokens", "max_concurrency", "timeout_s")

#: Chaves aceitas em `capabilities` -> `capabilities.discover(**...)`.
CAPABILITY_KEYS: tuple[str, ...] = (
    "hub_fraction",
    "hub_min_entries",
    "max_evidence_per_capability",
    "max_sites_per_gap",
)

#: Chaves que a camada REPO (`.wiki-ai.json` dentro do repositório analisado)
#: pode declarar. Tudo que fica de fora é privilégio de quem OPERA a análise
#: (store/cli), não de quem é analisado.
#:
#: O critério não é estético, é de superfície de ataque: `extractors` faz
#: `importlib.import_module` + chamada de fábrica (execução de código arbitrário
#: só por o repositório conter um arquivo), e `budget`/`policy`/`capabilities`/
#: `max_rounds`/`result_extra_keys` são orçamento, política de execução,
#: thresholds e chaves aceitas de resultado — o repositório analisado não tem
#: por que decidir quanto do orçamento de quem o analisa ele consome, nem o que
#: o runtime aceita de volta. O que sobra na allowlist é ESCOPO e OBRIGAÇÃO:
#: descrever o próprio sistema (o que ler, o que não se aplica e por quê).
REPO_LAYER_KEYS: frozenset[str] = frozenset(
    {
        "include",
        "exclude",
        "objectives",
        "max_reading_needs",
        "contract_exclusions",
        "failure_families_excluded",
        "failure_families_extra",
        "trigger_priority",
        "discovery_max_files_per_objective",
    }
)

_EMPTY_MAP: Mapping[str, Any] = MappingProxyType({})

_GLOB_CHARS = ("*", "?", "[")


# --------------------------------------------------------------------------
# Caminhos: prefixo OU glob, uma única semântica para snapshot e inventory
# --------------------------------------------------------------------------


def normalize_pattern(raw: str) -> str:
    """Padrão relativo, separador `/`, forma NFC — mesma normalização de `snapshot`."""
    return unicodedata.normalize("NFC", (raw or "").replace("\\", "/")).strip("/")


def _is_glob(pattern: str) -> bool:
    return any(ch in pattern for ch in _GLOB_CHARS)


def matches_any(path: str, patterns: Sequence[str] | None) -> bool:
    """`True` se `path` casa com algum padrão (prefixo OU glob `fnmatch`).

    Prefixo: `path == p` ou `path` começando por `p + "/"` — a semântica
    histórica de `snapshot._within_scope`, preservada byte a byte para padrões
    sem metacaractere. Glob: `fnmatch.fnmatchcase` sobre o caminho inteiro
    (`*` atravessa `/`, então `src/*` casa `src/a/b.py`), mais o casamento do
    diretório (`src/**` casa também com o próprio `src`).
    """
    if not patterns:
        return False
    for raw in patterns:
        p = normalize_pattern(raw)
        if not p:
            continue
        if _is_glob(p):
            if fnmatch.fnmatchcase(path, p):
                return True
            # `src/**` deve admitir o conteúdo de `src` sem exigir barra dupla.
            if p.endswith("/**") and (path == p[:-3] or path.startswith(p[:-3] + "/")):
                return True
        elif path == p or path.startswith(p + "/"):
            return True
    return False


# --------------------------------------------------------------------------
# Validação de tipos primitivos
# --------------------------------------------------------------------------


def _err(source: str, detail: str) -> ProfileError:
    return ProfileError(f"perfil de análise ({source}): {detail}")


def is_repo_layer(source: str) -> bool:
    """`True` quando `source` designa a camada REPO (o arquivo do analisado).

    A camada é decidida pelo PARÂMETRO `source` de quem carrega — nunca por
    algo escrito dentro do payload. Um `.wiki-ai.json` não consegue se declarar
    "store" para escapar da allowlist, porque não é ele quem informa a camada.
    """
    s = (source or "").strip().casefold()
    return s == "repo" or s.startswith("repo:")


def _assert_repo_layer_keys(source: str, keys: Iterable[str]) -> None:
    """Recusa, na camada repo, qualquer chave fora de `REPO_LAYER_KEYS`."""
    forbidden = sorted(set(keys) - REPO_LAYER_KEYS - {"source"})
    if not forbidden:
        return
    raise _err(
        source,
        f"chave(s) {', '.join(forbidden)} não são aceitas na camada do repositório "
        f"({REPO_CONFIG_FILENAME}) — permitida(s) apenas em store/cli. O repositório "
        "analisado declara ESCOPO e OBRIGAÇÃO (o que ler, o que não se aplica e por quê); "
        "carregar extratores, orçamento, política, thresholds e chaves de resultado é "
        f"decisão de quem opera a análise. Aceitas aqui: {', '.join(sorted(REPO_LAYER_KEYS))}"
    )


def _as_int(source: str, key: str, value: Any, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _err(source, f"{key!r} deve ser inteiro, recebido {type(value).__name__}")
    if value < minimum:
        raise _err(source, f"{key!r} deve ser >= {minimum}, recebido {value}")
    return value


def _as_str_tuple(source: str, key: str, value: Any) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise _err(source, f"{key!r} deve ser lista de strings, recebido {type(value).__name__}")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise _err(
                source, f"{key!r}: item deve ser string, recebido {type(item).__name__}"
            )
        item = item.strip()
        if not item:
            raise _err(source, f"{key!r}: item vazio não é padrão nem identificador")
        out.append(item)
    return tuple(out)


def _as_reason_map(source: str, key: str, value: Any) -> dict[str, str]:
    """Mapeamento `alvo -> motivo`, com motivo obrigatório e não vazio."""
    if not isinstance(value, Mapping):
        raise _err(source, f"{key!r} deve ser objeto {{alvo: motivo}}, recebido {type(value).__name__}")
    out: dict[str, str] = {}
    for target, reason in value.items():
        if not isinstance(target, str) or not target.strip():
            raise _err(source, f"{key!r}: alvo da exclusão deve ser string não vazia")
        if not isinstance(reason, str) or not reason.strip():
            raise _err(
                source,
                f"{key!r}: exclusão de {target!r} sem motivo — exclusão silenciosa é proibida "
                "(§6.1.3); declare por que este item não se aplica a este sistema",
            )
        out[target.strip()] = reason.strip()
    return out


def _as_int_map(source: str, key: str, value: Any, allowed: Sequence[str]) -> dict[str, int]:
    if not isinstance(value, Mapping):
        raise _err(source, f"{key!r} deve ser objeto, recebido {type(value).__name__}")
    out: dict[str, int] = {}
    for k, v in value.items():
        if k not in allowed:
            raise _err(
                source, f"{key!r}: chave desconhecida {k!r} (aceitas: {', '.join(allowed)})"
            )
        out[k] = _as_int(source, f"{key}.{k}", v, minimum=0)
    return out


def validate_capability_tuning(
    tuning: Mapping[str, Any] | None, *, source: str = "capabilities"
) -> dict[str, float | int]:
    """Faixas de `capabilities.discover`: `hub_fraction` em (0, 1], inteiros >= 0.

    Usada tanto por `AnalysisProfile.from_mapping` quanto pela própria
    `capabilities.discover`, para que um override fora de faixa falhe no ponto
    de uso e não produza um mapa de capacidades silenciosamente degradado.
    """
    if not tuning:
        return {}
    if not isinstance(tuning, Mapping):
        raise _err(source, f"capabilities deve ser objeto, recebido {type(tuning).__name__}")
    out: dict[str, float | int] = {}
    for k, v in tuning.items():
        if k not in CAPABILITY_KEYS:
            raise _err(
                source,
                f"capabilities: chave desconhecida {k!r} (aceitas: {', '.join(CAPABILITY_KEYS)})",
            )
        if k == "hub_fraction":
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise _err(source, f"capabilities.hub_fraction deve ser número, recebido {type(v).__name__}")
            fv = float(v)
            if not (0.0 < fv <= 1.0):
                raise _err(
                    source,
                    f"capabilities.hub_fraction deve estar em (0, 1], recebido {fv!r} — fora "
                    "dessa faixa o critério de hub deixa de existir ou engole todo símbolo",
                )
            out[k] = fv
        else:
            out[k] = _as_int(source, f"capabilities.{k}", v, minimum=0)
    return out


def _as_families_extra(source: str, value: Any) -> dict[str, tuple[str, ...]]:
    from .investigation import FAILURE_FAMILIES  # import tardio: evita ciclo

    if not isinstance(value, Mapping):
        raise _err(source, f"failure_families_extra deve ser objeto, recebido {type(value).__name__}")
    out: dict[str, tuple[str, ...]] = {}
    for family, items in value.items():
        if not isinstance(family, str) or not family.strip():
            raise _err(source, "failure_families_extra: nome de família deve ser string não vazia")
        family = family.strip()
        if family in FAILURE_FAMILIES:
            raise _err(
                source,
                f"failure_families_extra: {family!r} já é família do §6.5 — para mudar o que "
                "ela cobre use failure_families_excluded, não uma redefinição silenciosa",
            )
        parsed = _as_str_tuple(source, f"failure_families_extra.{family}", items)
        if not parsed:
            raise _err(
                source,
                f"failure_families_extra: família {family!r} sem itens — família vazia não "
                "acrescenta obrigação nenhuma",
            )
        out[family] = parsed
    return out


def _as_trigger_priority(source: str, value: Any) -> dict[str, int]:
    from .investigation import ReadingTrigger  # import tardio: evita ciclo

    if not isinstance(value, Mapping):
        raise _err(source, f"trigger_priority deve ser objeto, recebido {type(value).__name__}")
    valid = {t.value for t in ReadingTrigger}
    out: dict[str, int] = {}
    for k, v in value.items():
        if not isinstance(k, str) or k not in valid:
            raise _err(
                source,
                f"trigger_priority: gatilho desconhecido {k!r} (aceitos: {', '.join(sorted(valid))})",
            )
        out[k] = _as_int(source, f"trigger_priority.{k}", v, minimum=0)
    return out


def _as_contract_exclusions(source: str, value: Any) -> dict[str, str]:
    from .investigation import CONTRACT_FIELDS  # import tardio: evita ciclo

    parsed = _as_reason_map(source, "contract_exclusions", value)
    for name in parsed:
        if name not in CONTRACT_FIELDS:
            raise _err(
                source,
                f"contract_exclusions: {name!r} não é campo do contrato §6.3 "
                f"(aceitos: {', '.join(CONTRACT_FIELDS)})",
            )
    if len(parsed) == len(CONTRACT_FIELDS):
        raise _err(
            source,
            "contract_exclusions: excluir os 13 campos do §6.3 esvazia o contrato — o objetivo "
            "passaria a `complete` sem descrever nada",
        )
    return parsed


def _as_families_excluded(source: str, value: Any) -> dict[str, str]:
    from .investigation import FAILURE_FAMILIES  # import tardio: evita ciclo

    parsed = _as_reason_map(source, "failure_families_excluded", value)
    for family in parsed:
        if family not in FAILURE_FAMILIES:
            raise _err(
                source,
                f"failure_families_excluded: família desconhecida {family!r} "
                f"(aceitas: {', '.join(sorted(FAILURE_FAMILIES))})",
            )
    return parsed


def _as_extractors(source: str, value: Any) -> tuple[str, ...]:
    specs = _as_str_tuple(source, "extractors", value)
    for spec in specs:
        if ":" not in spec or not spec.partition(":")[0] or not spec.rpartition(":")[2]:
            raise _err(
                source, f"extractors: extensão inválida {spec!r}: use 'pacote.modulo:fabrica'"
            )
    return specs


# --------------------------------------------------------------------------
# O perfil
# --------------------------------------------------------------------------


def _freeze(data: Mapping[str, Any]) -> Mapping[str, Any]:
    """Cópia imutável com chaves ordenadas — determinismo de `to_dict`/`summary`."""
    return MappingProxyType(dict(sorted(data.items())))


def _dedup(*groups: Iterable[str]) -> tuple[str, ...]:
    """União ORDENADA (ordem de aparição, sem repetição) — merge determinístico."""
    seen: dict[str, None] = {}
    for group in groups:
        for item in group:
            seen.setdefault(item, None)
    return tuple(seen)


@dataclass(frozen=True)
class AnalysisProfile:
    """Alavancas de análise de UM sistema. Imutável, validado, com proveniência."""

    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    objectives: tuple[str, ...] = ()
    max_reading_needs: int | None = None
    max_rounds: int | None = None
    budget: Mapping[str, int] = _EMPTY_MAP
    policy: Mapping[str, int] = _EMPTY_MAP
    capabilities: Mapping[str, float | int] = _EMPTY_MAP
    contract_exclusions: Mapping[str, str] = _EMPTY_MAP
    failure_families_excluded: Mapping[str, str] = _EMPTY_MAP
    failure_families_extra: Mapping[str, tuple[str, ...]] = _EMPTY_MAP
    trigger_priority: Mapping[str, int] = _EMPTY_MAP
    #: Teto de arquivos por objetivo de descoberta (`ObjectiveKind.DISCOVERY`).
    #: `None` = nenhuma camada decidiu; `investigation.plan()` aplica
    #: `DISCOVERY_MAX_FILES_PER_OBJECTIVE`. Mínimo aceito: 1 (0 significaria
    #: objetivo sem arquivo nenhum, isto é, escopo apagado em silêncio).
    discovery_max_files_per_objective: int | None = None
    extractors: tuple[str, ...] = ()
    result_extra_keys: tuple[str, ...] = ()
    source: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        # Congela mapeamentos passados como dict cru pelo construtor direto.
        for f in _dc_fields(self):
            value = getattr(self, f.name)
            if isinstance(value, dict):
                object.__setattr__(self, f.name, _freeze(value))
            elif isinstance(value, list):
                object.__setattr__(self, f.name, tuple(value))

    # -- construção --------------------------------------------------------
    @classmethod
    def from_mapping(cls, data: Mapping[str, Any], *, source: str) -> "AnalysisProfile":
        """Constrói validando TUDO. Qualquer desvio vira `ProfileError` nomeando `source`.

        Rejeita: chave desconhecida, tipo errado, exclusão sem motivo, campo
        fora de `CONTRACT_FIELDS`, família fora de `FAILURE_FAMILIES`, valor
        fora de faixa (`hub_fraction` em (0,1], inteiros >= 0).

        Quando `source` designa a camada REPO (`is_repo_layer`), rejeita também
        toda chave fora de `REPO_LAYER_KEYS` — `extractors` à frente, que
        executaria código arbitrário do repositório analisado. A camada vem do
        PARÂMETRO, então o payload não escolhe o próprio privilégio.
        """
        if not isinstance(data, Mapping):
            raise _err(source, f"perfil deve ser objeto, recebido {type(data).__name__}")
        # `source` é TOLERADO na entrada e IGNORADO: a proveniência é de quem
        # carrega o perfil, nunca do dado carregado. Sem essa tolerância,
        # `from_mapping(p.to_dict(), source=...)` — o caminho que o CLI usa para
        # reidratar um perfil guardado no store — quebraria com "chave
        # desconhecida" no próprio round-trip. `to_dict()` também não emite
        # `source`, então as duas pontas concordam.
        unknown = sorted(set(map(str, data)) - set(PROFILE_KEYS) - {"source"})
        if unknown:
            raise _err(
                source,
                f"chave(s) desconhecida(s): {', '.join(unknown)} "
                f"(aceitas: {', '.join(PROFILE_KEYS)})",
            )
        if is_repo_layer(source):
            _assert_repo_layer_keys(source, map(str, data))
        kwargs: dict[str, Any] = {}
        if "include" in data:
            kwargs["include"] = tuple(
                normalize_pattern(p) for p in _as_str_tuple(source, "include", data["include"])
            )
        if "exclude" in data:
            kwargs["exclude"] = tuple(
                normalize_pattern(p) for p in _as_str_tuple(source, "exclude", data["exclude"])
            )
        if "objectives" in data:
            kwargs["objectives"] = _as_str_tuple(source, "objectives", data["objectives"])
        for scalar in ("max_reading_needs", "max_rounds"):
            if scalar in data and data[scalar] is not None:
                kwargs[scalar] = _as_int(source, scalar, data[scalar], minimum=0)
        if (
            "discovery_max_files_per_objective" in data
            and data["discovery_max_files_per_objective"] is not None
        ):
            kwargs["discovery_max_files_per_objective"] = _as_int(
                source,
                "discovery_max_files_per_objective",
                data["discovery_max_files_per_objective"],
                minimum=1,
            )
        if "budget" in data:
            kwargs["budget"] = _freeze(_as_int_map(source, "budget", data["budget"], BUDGET_KEYS))
        if "policy" in data:
            kwargs["policy"] = _freeze(_as_int_map(source, "policy", data["policy"], POLICY_KEYS))
        if "capabilities" in data:
            kwargs["capabilities"] = _freeze(
                validate_capability_tuning(data["capabilities"], source=source)
            )
        if "contract_exclusions" in data:
            kwargs["contract_exclusions"] = _freeze(
                _as_contract_exclusions(source, data["contract_exclusions"])
            )
        if "failure_families_excluded" in data:
            kwargs["failure_families_excluded"] = _freeze(
                _as_families_excluded(source, data["failure_families_excluded"])
            )
        if "failure_families_extra" in data:
            kwargs["failure_families_extra"] = _freeze(
                _as_families_extra(source, data["failure_families_extra"])
            )
        if "trigger_priority" in data:
            kwargs["trigger_priority"] = _freeze(
                _as_trigger_priority(source, data["trigger_priority"])
            )
        if "extractors" in data:
            kwargs["extractors"] = _as_extractors(source, data["extractors"])
        if "result_extra_keys" in data:
            kwargs["result_extra_keys"] = _as_str_tuple(
                source, "result_extra_keys", data["result_extra_keys"]
            )
        # `source` do payload é ignorado: proveniência é de quem carrega, não do dado.
        kwargs["source"] = (source,)
        return cls(**kwargs)

    # -- composição --------------------------------------------------------
    def merge(self, other: "AnalysisProfile") -> "AnalysisProfile":
        """`other` prevalece. Escalares não-`None` sobrescrevem; tuplas unem;
        mapeamentos unem com `other` por cima; `source` concatena."""
        if not isinstance(other, AnalysisProfile):
            raise ProfileError(
                f"merge exige AnalysisProfile, recebido {type(other).__name__}"
            )
        return AnalysisProfile(
            include=_dedup(self.include, other.include),
            exclude=_dedup(self.exclude, other.exclude),
            objectives=_dedup(self.objectives, other.objectives),
            max_reading_needs=(
                other.max_reading_needs
                if other.max_reading_needs is not None
                else self.max_reading_needs
            ),
            max_rounds=(
                other.max_rounds if other.max_rounds is not None else self.max_rounds
            ),
            budget=_freeze({**self.budget, **other.budget}),
            policy=_freeze({**self.policy, **other.policy}),
            capabilities=_freeze({**self.capabilities, **other.capabilities}),
            contract_exclusions=_freeze(
                {**self.contract_exclusions, **other.contract_exclusions}
            ),
            failure_families_excluded=_freeze(
                {**self.failure_families_excluded, **other.failure_families_excluded}
            ),
            failure_families_extra=_freeze(
                {**self.failure_families_extra, **other.failure_families_extra}
            ),
            trigger_priority=_freeze({**self.trigger_priority, **other.trigger_priority}),
            discovery_max_files_per_objective=(
                other.discovery_max_files_per_objective
                if other.discovery_max_files_per_objective is not None
                else self.discovery_max_files_per_objective
            ),
            extractors=_dedup(self.extractors, other.extractors),
            result_extra_keys=_dedup(self.result_extra_keys, other.result_extra_keys),
            source=self.source + other.source,
        )

    # -- leitura -----------------------------------------------------------
    def is_default(self) -> bool:
        """`True` quando nenhuma alavanca foi ligada (`source` não conta).

        Um `.wiki-ai.json` vazio produz proveniência mas nenhum efeito: o
        pipeline precisa saber que pode seguir o caminho histórico.
        """
        for f in _dc_fields(self):
            if f.name == "source":
                continue
            if getattr(self, f.name) != getattr(DEFAULT_PROFILE, f.name):
                return False
        return True

    def to_dict(self) -> dict[str, Any]:
        """Só chaves não-default, JSON-serializável, determinístico.

        `source` fica DE FORA: é proveniência de carga, não configuração. Quem
        reidrata declara a camada no parâmetro `source` de `from_mapping`, e
        `from_mapping(p.to_dict(), source=...)` é round-trip fechado — a saída
        daqui é sempre entrada válida lá. Para ler a proveniência use
        `AnalysisProfile.source` ou `summary()["source"]`.
        """
        out: dict[str, Any] = {}
        for f in _dc_fields(self):
            if f.name == "source":
                continue
            value = getattr(self, f.name)
            if value == getattr(DEFAULT_PROFILE, f.name):
                continue
            if isinstance(value, Mapping):
                out[f.name] = {
                    k: (list(v) if isinstance(v, tuple) else v)
                    for k, v in sorted(value.items())
                }
            elif isinstance(value, tuple):
                out[f.name] = list(value)
            else:
                out[f.name] = value
        return dict(sorted(out.items()))

    def summary(self) -> dict[str, Any]:
        """Compacto para o envelope do CLI: proveniência, contagens, exclusões, overrides."""
        overrides: dict[str, Any] = {}
        if self.max_reading_needs is not None:
            overrides["max_reading_needs"] = self.max_reading_needs
        if self.max_rounds is not None:
            overrides["max_rounds"] = self.max_rounds
        if self.discovery_max_files_per_objective is not None:
            overrides["discovery_max_files_per_objective"] = (
                self.discovery_max_files_per_objective
            )
        for name in ("budget", "policy", "capabilities", "trigger_priority"):
            value = getattr(self, name)
            if value:
                overrides[name] = dict(sorted(value.items()))
        return {
            "source": list(self.source),
            "is_default": self.is_default(),
            "include": len(self.include),
            "exclude": len(self.exclude),
            "objectives": len(self.objectives),
            "contract_exclusions": dict(sorted(self.contract_exclusions.items())),
            "failure_families_excluded": dict(sorted(self.failure_families_excluded.items())),
            "failure_families_extra": {
                k: list(v) for k, v in sorted(self.failure_families_extra.items())
            },
            "extractors": list(self.extractors),
            "result_extra_keys": list(self.result_extra_keys),
            "overrides": overrides,
        }

    # -- consulta usada pelo pipeline --------------------------------------
    def selects_objective(self, *, objective_id: str, capability_id: str, name: str) -> bool:
        """Filtro do §6.4 por objetivo: id exato OU substring (case-insensitive).

        `objectives` vazio admite tudo — o denominador histórico. Quando há
        filtro, o corte é do CHAMADOR e não pode ser confundido com cobertura:
        `plan()` registra os objetivos suprimidos em `accounting`.
        """
        if not self.objectives:
            return True
        for raw in self.objectives:
            if raw == objective_id:
                return True
            needle = raw.casefold()
            if needle and (needle in capability_id.casefold() or needle in name.casefold()):
                return True
        return False

    def repo_layer_violations(self) -> tuple[str, ...]:
        """Campos NÃO-default que a camada repo não pode declarar.

        Existe porque `AnalysisProfile(...)` pode ser construído direto, sem
        passar por `from_mapping`: `resolve_profile` consulta isto para que um
        `repo_config` forjado em memória não eleve privilégio.
        """
        return tuple(
            f.name
            for f in _dc_fields(self)
            if f.name != "source"
            and f.name not in REPO_LAYER_KEYS
            and getattr(self, f.name) != getattr(DEFAULT_PROFILE, f.name)
        )

    def with_source(self, source: str) -> "AnalysisProfile":
        return _dc_replace(self, source=self.source + (source,))


DEFAULT_PROFILE = AnalysisProfile()

#: Chaves aceitas no topo do perfil — derivadas dos campos, sem lista paralela.
PROFILE_KEYS: tuple[str, ...] = tuple(
    f.name for f in _dc_fields(AnalysisProfile) if f.name != "source"
)


# --------------------------------------------------------------------------
# Carga
# --------------------------------------------------------------------------


def load_repo_config(repo_abs: str) -> AnalysisProfile | None:
    """Lê `<repo>/.wiki-ai.json`. Ausente -> `None`. Inválido -> `ProfileError`.

    O erro sempre nomeia o CAMINHO do arquivo: um perfil quebrado precisa ser
    corrigido no repositório analisado, não adivinhado por quem chama.

    Carrega na CAMADA REPO, logo só aceita `REPO_LAYER_KEYS`: um repositório
    hostil não consegue executar código no analisador declarando `extractors`,
    nem consumir o orçamento de quem o analisa declarando `budget`/`policy`.
    """
    path = os.path.join(os.path.abspath(repo_abs), REPO_CONFIG_FILENAME)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, UnicodeDecodeError) as exc:
        raise ProfileError(f"{path}: não pôde ser lido: {type(exc).__name__}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ProfileError(f"{path}: JSON inválido na linha {exc.lineno}: {exc.msg}") from exc
    if not isinstance(raw, Mapping):
        raise ProfileError(
            f"{path}: raiz deve ser um objeto JSON, recebido {type(raw).__name__}"
        )
    return AnalysisProfile.from_mapping(raw, source=f"repo:{REPO_CONFIG_FILENAME}")


def resolve_profile(
    *,
    repo_config: AnalysisProfile | None = None,
    store_profile: Mapping[str, Any] | None = None,
    cli_overrides: Mapping[str, Any] | None = None,
) -> AnalysisProfile:
    """Mescla as camadas na ordem `repo < store < cli`.

    `store_profile` e `cli_overrides` passam por `from_mapping` com `source`
    `"store"` e `"cli"`, portanto sofrem a MESMA validação do arquivo do repo —
    não existe caminho de entrada sem validação. Eles NÃO sofrem a restrição de
    camada: `extractors`, `budget`, `policy`, `capabilities`, `max_rounds` e
    `result_extra_keys` são privilégio de quem opera a análise.

    `repo_config` é reverificado aqui contra `REPO_LAYER_KEYS` mesmo já tendo
    passado por `load_repo_config`: um `AnalysisProfile` construído direto (sem
    `from_mapping`) não pode virar porta de entrada para elevar privilégio.
    """
    if repo_config is not None:
        if not isinstance(repo_config, AnalysisProfile):
            raise ProfileError(
                "resolve_profile(repo_config=...) exige AnalysisProfile, recebido "
                f"{type(repo_config).__name__}"
            )
        _assert_repo_layer_keys(
            repo_config.source[0] if repo_config.source else "repo",
            repo_config.repo_layer_violations(),
        )
    resolved = repo_config if repo_config is not None else DEFAULT_PROFILE
    if store_profile is not None:
        resolved = resolved.merge(AnalysisProfile.from_mapping(store_profile, source="store"))
    if cli_overrides is not None:
        resolved = resolved.merge(AnalysisProfile.from_mapping(cli_overrides, source="cli"))
    return resolved
