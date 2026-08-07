"""Analisador de acoplamento — Java (motor especializado)
==========================================================

Complementa `coupling.py` (motor multi-linguagem, baseado em heurística de
import genérica por regex-família) com uma leitura mais profunda, própria
de Java: parse do corpo de cada classe (ainda regex — não é AST completo,
mesma limitação já assumida no motor genérico), grafo de dependência POR
CLASSE (não só por pacote), detecção de ciclos, e quatro sinais de "por
classe" que não existem — nem fazem sentido — na visão por pacote:

    1. Ciclos de dependência entre classes 🟢 (fato de grafo, sem limiar)
    2. Classe-deus 🟢 (Ce outlier E WMC_max outlier — limiar por IQR)
    3. Implementação concreta sobre-dependida 🟡 (Ca outlier dentro do
       subgrupo de classes concretas — record/enum/interface ficam fora
       da comparação, têm perfil de Ca estruturalmente diferente)
    4. Abstração especulativa 🟡 (interface/abstract class com no máximo
       1 implementação e nenhum consumidor real)

Ver docs/especificacao-vistas.md (fonte que gerou este motor) para a
justificativa completa de cada sinal e de cada limiar.

Também agrega por pacote (Ca, Ce, I, A, D, zona) com a MESMA semântica de
zona do motor genérico (`coupling.py::_zone`): dor / transição / saudável
/ inutilidade, mais os casos degenerados "isolado" (Ce+Ca==0, pacote sem
nenhuma aresta) e "indeterminada (sem abstração)" (A não calculável).

🟢 Ce/Ca (classe e pacote): derivam do grafo de import interno resolvido
por regex + casamento de identificador no mesmo pacote (Java não exige
import dentro do próprio pacote) — aproxima (perde wildcard ambíguo,
reflection, DI por nome/qualifier), igual ao motor genérico.
🟡 A (abstração), classe-deus, hotspot concreto e abstração especulativa:
heurística — dependem de "kind"/"abstract" reconhecido por regex e de
limiares estatísticos (IQR, Tukey 1977) calculados a partir da
distribuição REAL de cada projeto, não de número fixo global.
"""
from __future__ import annotations

import os
import re
from collections import defaultdict
from dataclasses import dataclass, field

from .surface import LANGUAGES, MAX_FILE_BYTES, SKIP_DIRS, _is_generated

_JAVA_EXT = ".java"
_JAVA_LANG = LANGUAGES[_JAVA_EXT]  # "Java" — não hardcoded, deriva do mapa central do surface

# --- config -------------------------------------------------------------

DEFAULT_CONFIG = {
    # pastas ignoradas em qualquer nível da árvore (unidas ao SKIP_DIRS
    # genérico do surface.py — este motor conhece coisas que o genérico
    # não precisa saber, como "test"/"generated-sources")
    "exclude_dirs": [
        "target", "build", "out", ".git", ".idea", ".mvn", ".gradle",
        "test", "tests", "generated", "generated-sources", "node_modules",
    ],
    # sufixos de nome de arquivo (sem .java) ignorados
    "exclude_file_suffixes": [
        "Dto", "DTO", "Request", "Response", "Payload",
        "Config", "Configuration", "Properties", "Application",
        "Test", "Tests", "IT", "ITCase", "TestCase",
    ],
    # regex adicionais aplicadas ao caminho relativo (POSIX) do arquivo
    "exclude_file_patterns": [
        r"package-info\.java$",
        r"module-info\.java$",
    ],
    # se a classe tiver alguma destas anotações na declaração, é excluída
    "exclude_annotations": [
        "SpringBootApplication", "Configuration", "ConfigurationProperties",
        "Entity", "Embeddable",
    ],
    # sem limiar fixo aqui de propósito — Ce, WMC_max e Ca (concretas) são
    # avaliados por outlier estatístico (IQR), calculado a partir da
    # distribuição real de CADA projeto. Ver especificacao-vistas.md seção 4.
}


def _merge_config(config: dict | None) -> dict:
    """Config do usuário SUBSTITUI a default chave a chave (sem merge de
    listas) — mesmo comportamento do script original."""
    cfg = {k: (list(v) if isinstance(v, list) else v) for k, v in DEFAULT_CONFIG.items()}
    if config:
        for key, value in config.items():
            cfg[key] = value
    return cfg


# --- modelo de dados ------------------------------------------------------


@dataclass
class _JavaClass:
    fqcn: str
    simple_name: str
    package: str
    file_path: str
    kind: str  # class | interface | enum | record | annotation
    is_abstract: bool
    raw_imports: list[str] = field(default_factory=list)
    deps: set[str] = field(default_factory=set)  # FQCNs internas resolvidas
    method_count: int = 0
    wmc: int = 0  # complexidade ciclomática somada (informativo)
    max_method_wmc: int = 0  # maior complexidade de UM método (usado na classe-deus)
    raw_supertypes: list[str] = field(default_factory=list)  # nomes em implements/extends
    implements_or_extends: set[str] = field(default_factory=set)  # FQCNs resolvidas


# --- filtros de exclusão ---------------------------------------------------


def _combined_skip_dirs(cfg: dict) -> set[str]:
    """Diretórios nunca descidos: SKIP_DIRS do surface.py (convenção do
    pacote, aplicada a qualquer linguagem) unido ao exclude_dirs da config
    Java (coisas que o SKIP_DIRS genérico não conhece, como "test")."""
    return {d.lower() for d in cfg["exclude_dirs"]} | {d.lower() for d in SKIP_DIRS}


def _path_excluded_by_pattern(rel_path: str, cfg: dict) -> str | None:
    for pattern in cfg["exclude_file_patterns"]:
        if re.search(pattern, rel_path):
            return f"padrão de arquivo: {pattern}"
    return None


def _name_excluded_by_suffix(stem: str, cfg: dict) -> str | None:
    for suffix in cfg["exclude_file_suffixes"]:
        if stem.endswith(suffix):
            return f"sufixo de nome: *{suffix}.java"
    return None


def _content_excluded_by_annotation(content: str, cfg: dict) -> str | None:
    for ann in cfg["exclude_annotations"]:
        if re.search(rf"@{re.escape(ann)}\b", content):
            return f"anotação: @{ann}"
    return None


# --- parsing (regex — suficiente para mapa de arquitetura, não substitui
# um parser AST completo) ---------------------------------------------------

_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT_RE = re.compile(r"//[^\n]*")
_STRING_LITERAL_RE = re.compile(r'"(?:\\.|[^"\\])*"')
_CHAR_LITERAL_RE = re.compile(r"'(?:\\.|[^'\\])'")

_PACKAGE_RE = re.compile(r"^\s*package\s+([\w.]+)\s*;", re.MULTILINE)
_IMPORT_RE = re.compile(r"^\s*import\s+(static\s+)?([\w.]+(?:\.\*)?)\s*;", re.MULTILINE)

# tipo primário: public (abstract|final)? (class|interface|enum|record) Nome
_TYPE_DECL_RE = re.compile(
    r"\b(?:public\s+)?(?P<mods>(?:abstract|final|sealed|static)\s+)*"
    r"(?P<kind>class|interface|enum|record)\s+(?P<name>\w+)"
)
_ANNOTATION_DECL_RE = re.compile(r"@interface\s+(\w+)")

# assinatura de método (heurística): nome + '(' parâmetros ')' + opcional throws + '{'
_METHOD_SIG_RE = re.compile(
    r"(?:public|private|protected|static|final|synchronized|abstract|default|\s)*"
    r"[\w<>\[\],\s?]+?\s+(\w+)\s*\([^;{}]*\)\s*(?:throws\s+[\w.,\s]+)?\s*\{"
)

_DECISION_POINT_RE = re.compile(
    r"\b(if|else\s+if|for|while|case|catch)\b|(\&\&|\|\||\?\s*[^:]+:)"
)

_IDENTIFIER_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\b")


def _strip_comments_and_strings(content: str) -> str:
    text = _BLOCK_COMMENT_RE.sub(" ", content)
    text = _LINE_COMMENT_RE.sub(" ", text)
    text = _STRING_LITERAL_RE.sub('""', text)
    text = _CHAR_LITERAL_RE.sub("''", text)
    return text


def _find_package(clean_text: str) -> str:
    m = _PACKAGE_RE.search(clean_text)
    return m.group(1) if m else "(default)"


def _find_imports(clean_text: str) -> list[str]:
    return [m.group(2) for m in _IMPORT_RE.finditer(clean_text)]


def _find_primary_type(clean_text: str, filename_stem: str) -> dict | None:
    """Procura o tipo top-level cujo nome bate com o nome do arquivo
    (convenção padrão Java). Se não achar, usa a primeira declaração
    encontrada como fallback."""
    ann_match = _ANNOTATION_DECL_RE.search(clean_text)
    if ann_match and ann_match.group(1) == filename_stem:
        return {"name": filename_stem, "kind": "annotation", "is_abstract": False, "match": ann_match}

    candidates = []
    for m in _TYPE_DECL_RE.finditer(clean_text):
        name = m.group("name")
        kind = m.group("kind")
        mods = m.group("mods") or ""
        is_abstract = kind == "interface" or "abstract" in mods
        candidates.append({"name": name, "kind": kind, "is_abstract": is_abstract, "match": m})

    for c in candidates:
        if c["name"] == filename_stem:
            return c
    return candidates[0] if candidates else None


def _extract_braced_body(text: str, open_brace_pos: int) -> tuple[str | None, int]:
    """Dado a posição de um '{', usa casamento de chaves (não regex) para
    achar o '}' correspondente. Retorna (conteúdo entre chaves, posição
    logo após o '}') ou (None, len(text)) se não fechar."""
    depth = 0
    i = open_brace_pos
    n = len(text)
    start_content = open_brace_pos + 1
    while i < n:
        c = text[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start_content:i], i + 1
        i += 1
    return None, n


def _extract_methods_and_wmc(clean_text: str) -> tuple[int, int, int]:
    """Conta métodos e soma a complexidade ciclomática (WMC) da classe.
    TAMBÉM retorna o maior WMC de um método individual (max_method_wmc) —
    isso importa porque a SOMA sozinha confunde "muitos métodos simples"
    (ex: um ExceptionHandler com 1 método por tipo de exceção, cada um
    trivial) com "um método profundamente emaranhado" (o problema real
    que classe-deus deveria capturar). Duas classes podem ter a mesma
    soma de WMC por motivos opostos — max_method_wmc desambigua isso.
    Usa casamento de chaves para isolar o corpo de cada método (não só
    regex), para não contar pontos de decisão fora do escopo do método.
    Heurística: não resolve escopo (nested classes/lambdas complexas
    podem inflar um pouco o número), mas não depende de resolver
    identificador nenhum, então não sofre do problema de shadowing."""
    method_count = 0
    wmc = 0
    max_method_wmc = 0
    pos = 0
    while True:
        m = _METHOD_SIG_RE.search(clean_text, pos)
        if not m:
            break
        brace_pos = m.end() - 1
        body, end_pos = _extract_braced_body(clean_text, brace_pos)
        if body is None:
            pos = m.end()
            continue
        method_count += 1
        decision_points = len(_DECISION_POINT_RE.findall(body))
        method_wmc = 1 + decision_points  # complexidade ciclomática de McCabe (base 1)
        wmc += method_wmc
        max_method_wmc = max(max_method_wmc, method_wmc)
        pos = end_pos
    return method_count, wmc, max_method_wmc


def _extract_supertypes(clean_text: str, type_match) -> list[str]:
    """Extrai nomes em 'implements X, Y' / 'extends X, Y' do CABEÇALHO do
    tipo primário (do fim do nome até a primeira '{'), evitando pegar
    'extends'/'implements' de outro lugar do arquivo por engano."""
    header_start = type_match.end()
    brace_idx = clean_text.find("{", header_start)
    if brace_idx == -1:
        return []
    header = clean_text[header_start:brace_idx]

    names = []
    for keyword in ("extends", "implements"):
        km = re.search(rf"\b{keyword}\s+(.+?)(?=\bextends\b|\bimplements\b|$)", header)
        if not km:
            continue
        raw_list = km.group(1)
        for part in raw_list.split(","):
            part = part.strip()
            part = re.sub(r"<.*?>", "", part)  # remove generics tipo <T>
            part = part.strip()
            if part:
                names.append(part)
    return names


# --- varredura do projeto ---------------------------------------------------


def _collect_java_files(repo: str, cfg: dict) -> list[str]:
    """Caminhos relativos (POSIX) de todo .java sob repo. Diretórios
    ignorados são podados durante a caminhada — nunca visitados, silencioso
    (mesmo padrão de SKIP_DIRS em coupling.py), sem entrada em `excluded`
    para arquivos que estão dentro deles. Ordenado para determinismo: a
    ordem de iteração do os.walk não é garantida entre sistemas de arquivo."""
    skip_dirs = _combined_skip_dirs(cfg)
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(repo):
        dirnames[:] = [
            d for d in dirnames
            if not d.startswith(".") and d.lower() not in skip_dirs
        ]
        for fn in filenames:
            if fn.endswith(_JAVA_EXT):
                rel = os.path.relpath(os.path.join(dirpath, fn), repo).replace("\\", "/")
                found.append(rel)
    return sorted(found)


def _scan_project(repo: str, cfg: dict) -> tuple[dict[str, _JavaClass], list[dict]]:
    """Varre o projeto e monta o mapa de classes Java incluídas + a lista
    de arquivos excluídos com o motivo. Exclusão por diretório é silenciosa
    (ver _collect_java_files); as demais (padrão de nome, sufixo, anotação,
    gerado, grande demais, erro de leitura, sem tipo reconhecido) entram em
    `excluded` com o mesmo motivo do script original."""
    classes: dict[str, _JavaClass] = {}
    excluded: list[dict] = []

    for rel in _collect_java_files(repo, cfg):
        full = os.path.join(repo, rel)
        fn = os.path.basename(rel)
        stem = fn[: -len(_JAVA_EXT)]

        if _is_generated(fn):
            excluded.append({"file_path": rel, "reason": "arquivo gerado"})
            continue

        reason = _path_excluded_by_pattern(rel, cfg)
        if reason:
            excluded.append({"file_path": rel, "reason": reason})
            continue

        reason = _name_excluded_by_suffix(stem, cfg)
        if reason:
            excluded.append({"file_path": rel, "reason": reason})
            continue

        try:
            if os.path.getsize(full) > MAX_FILE_BYTES:
                excluded.append({"file_path": rel, "reason": "arquivo grande demais (> MAX_FILE_BYTES)"})
                continue
        except OSError as exc:
            excluded.append({"file_path": rel, "reason": f"erro de leitura: {exc}"})
            continue

        try:
            with open(full, encoding="utf-8-sig", errors="replace") as fh:
                raw_content = fh.read()
        except OSError as exc:
            excluded.append({"file_path": rel, "reason": f"erro de leitura: {exc}"})
            continue

        reason = _content_excluded_by_annotation(raw_content, cfg)
        if reason:
            excluded.append({"file_path": rel, "reason": reason})
            continue

        clean = _strip_comments_and_strings(raw_content)
        primary = _find_primary_type(clean, stem)
        if primary is None:
            excluded.append({"file_path": rel, "reason": "nenhum tipo Java reconhecido"})
            continue

        package = _find_package(clean)
        fqcn = f"{package}.{primary['name']}" if package != "(default)" else primary["name"]
        imports = _find_imports(clean)
        method_count, wmc, max_method_wmc = _extract_methods_and_wmc(clean)
        supertypes = _extract_supertypes(clean, primary["match"]) if primary.get("match") else []

        classes[fqcn] = _JavaClass(
            fqcn=fqcn,
            simple_name=primary["name"],
            package=package,
            file_path=rel,
            kind=primary["kind"],
            is_abstract=primary["is_abstract"],
            raw_imports=imports,
            method_count=method_count,
            wmc=wmc,
            max_method_wmc=max_method_wmc,
            raw_supertypes=supertypes,
        )

    excluded.sort(key=lambda e: e["file_path"])
    return classes, excluded


# --- resolução de dependências ----------------------------------------------


def _pkg_roots(classes: dict[str, _JavaClass]) -> set[str]:
    """Primeiro segmento de cada pacote conhecido no projeto — usado só
    para decidir se um import não resolvido "parece" interno (mesma raiz
    de pacote do projeto) ou é claramente externo (biblioteca/JDK)."""
    roots = set()
    for jc in classes.values():
        if jc.package != "(default)":
            roots.add(jc.package.split(".")[0])
    return roots


def _looks_internal_import(imp: str, roots: set[str]) -> bool:
    if not roots:
        return False
    target = imp[:-2] if imp.endswith(".*") else imp
    first = target.split(".", 1)[0]
    return first in roots


def _resolve_dependencies(classes: dict[str, _JavaClass], repo: str) -> int:
    """Resolve os imports (e usos sem import, mesmo pacote) para FQCNs
    internas do projeto. Abordagem heurística baseada em regex, não em
    resolução semântica completa — adequada para mapa de arquitetura, não
    para refatoração automatizada.

    Passo 1: imports explícitos (incluindo wildcard "import x.y.*").
    Passo 2: identificadores usados no corpo do arquivo que batem com um
    simple_name conhecido — cobre uso no mesmo pacote (Java não exige
    import dentro do mesmo pacote).

    Também conta, ao longo do passo 1, imports que "parecem" internos
    (raiz de pacote conhecida no projeto) mas não viraram aresta nem por
    import explícito nem por wildcard — mesmo conceito de
    unresolved_internal_imports do motor genérico (coupling.py),
    adaptado à granularidade de import Java."""
    simple_index: dict[str, list[str]] = defaultdict(list)
    for fqcn, jc in classes.items():
        simple_index[jc.simple_name].append(fqcn)
    fqcn_set = set(classes.keys())
    roots = _pkg_roots(classes)
    unresolved = 0

    for jc in classes.values():
        resolved: set[str] = set()

        # 1) imports explícitos
        for imp in jc.raw_imports:
            if imp.endswith(".*"):
                imp_pkg = imp[:-2]
                matched = False
                for cand_fqcn, cand in classes.items():
                    if cand.package == imp_pkg and cand_fqcn != jc.fqcn:
                        resolved.add(cand_fqcn)
                        matched = True
                if not matched and _looks_internal_import(imp, roots):
                    unresolved += 1
            elif imp in fqcn_set:
                if imp != jc.fqcn:
                    resolved.add(imp)
            elif _looks_internal_import(imp, roots):
                unresolved += 1

        # 2) heurística de identificadores (mesmo pacote / uso direto)
        full = os.path.join(repo, jc.file_path)
        try:
            with open(full, encoding="utf-8-sig", errors="replace") as fh:
                raw_content = fh.read()
            clean = _strip_comments_and_strings(raw_content)
            tokens = set(_IDENTIFIER_RE.findall(clean))
        except OSError:
            tokens = set()

        for token in tokens:
            if token == jc.simple_name:
                continue
            candidates = simple_index.get(token)
            if not candidates:
                continue
            if len(candidates) == 1:
                target = candidates[0]
            else:
                same_pkg = [c for c in candidates if classes[c].package == jc.package]
                target = same_pkg[0] if same_pkg else None
            if target and target != jc.fqcn:
                resolved.add(target)

        jc.deps = resolved

        # resolve implements/extends para FQCN, usando o mesmo índice
        implements_resolved: set[str] = set()
        for name in jc.raw_supertypes:
            candidates = simple_index.get(name)
            if not candidates:
                continue
            if len(candidates) == 1:
                target = candidates[0]
            else:
                same_pkg = [c for c in candidates if classes[c].package == jc.package]
                target = same_pkg[0] if same_pkg else None
            if target and target != jc.fqcn:
                implements_resolved.add(target)
        jc.implements_or_extends = implements_resolved

    return unresolved


# --- limiar estatístico e zona (compartilhados com a agregação por pacote) --


def _iqr_outlier_threshold(values: list[int]) -> float | None:
    """Limiar de outlier estatístico via regra do boxplot (Tukey, 1977):
    Q3 + 1.5*IQR. Específico da distribuição passada — não é um número
    fixo global, se adapta à escala de cada projeto/subgrupo. Precisa de
    pelo menos 4 valores para quartis fazerem sentido; com menos que
    isso, retorna None (nenhuma classe é marcada — N pequeno demais para
    estatística, melhor não marcar nada do que marcar errado)."""
    if len(values) < 4:
        return None
    s = sorted(values)
    n = len(s)

    def percentile(p):
        k = (n - 1) * p
        f, c = int(k), min(int(k) + 1, n - 1)
        return s[f] + (s[c] - s[f]) * (k - f)

    q1, q3 = percentile(0.25), percentile(0.75)
    iqr = q3 - q1
    return q3 + 1.5 * iqr


def _zone(i: float, a: float | None) -> str:
    """Mesma lógica de limiar de coupling.py::_zone (0.3/0.7, D<=0.3) —
    a visão "por pacote" usa a metodologia de Martin sem adaptação, então
    o vocabulário de zona precisa ser o mesmo em qualquer motor."""
    if a is None:
        return "indeterminada (sem abstração)"
    if i <= 0.3 and a <= 0.3:
        return "dor"
    if i >= 0.7 and a >= 0.7:
        return "inutilidade"
    d = abs(a + i - 1)
    return "saudável" if d <= 0.3 else "transição"


# --- métricas por classe -----------------------------------------------------


def _compute_class_metrics(classes: dict[str, _JavaClass]) -> tuple[dict[str, dict], dict[str, float | None]]:
    ca_count: dict[str, int] = defaultdict(int)
    for jc in classes.values():
        for dep in jc.deps:
            if dep in classes:
                ca_count[dep] += 1

    # mapa reverso: quem implementa/estende cada tipo (não é o mesmo que
    # "depende de" — implements/extends é um relacionamento específico)
    implementor_of: dict[str, set[str]] = defaultdict(set)
    for fqcn, jc in classes.items():
        for target in jc.implements_or_extends:
            if target in classes:
                implementor_of[target].add(fqcn)

    raw = {}
    for fqcn, jc in classes.items():
        ce = len({d for d in jc.deps if d in classes})
        ca = ca_count.get(fqcn, 0)
        implementor_count = len(implementor_of.get(fqcn, set()))
        raw[fqcn] = {"ce": ce, "ca": ca, "implementor_count": implementor_count}

    # limiares estatísticos, calculados a partir da distribuição REAL
    # deste projeto (regra do boxplot / IQR) — não são números fixos
    # escolhidos a priori. Ver especificacao-vistas.md seção 4.
    ce_threshold = _iqr_outlier_threshold([r["ce"] for r in raw.values()])
    wmc_threshold = _iqr_outlier_threshold([jc.max_method_wmc for jc in classes.values()])

    # Ca de "implementação concreta sobre-dependida" é comparado só
    # dentro do subgrupo kind == class (não interface/record/enum) —
    # esses têm perfil de Ca estruturalmente diferente e normalmente
    # saudável (um value object ou uma interface é ESPERADO ter Ca alto;
    # misturar no mesmo cálculo distorceria o limiar).
    concrete_class_fqcns = [
        fqcn for fqcn, jc in classes.items()
        if jc.kind == "class" and not jc.is_abstract
    ]
    ca_threshold_concrete = _iqr_outlier_threshold(
        [raw[fqcn]["ca"] for fqcn in concrete_class_fqcns]
    )

    metrics: dict[str, dict] = {}
    for fqcn, jc in classes.items():
        r = raw[fqcn]
        ce, ca, implementor_count = r["ce"], r["ca"], r["implementor_count"]

        is_ce_outlier = ce_threshold is not None and ce > ce_threshold
        is_wmc_outlier = wmc_threshold is not None and jc.max_method_wmc > wmc_threshold

        # classe-deus: as DUAS dimensões precisam ser outlier ao mesmo
        # tempo — amplitude (Ce) e profundidade (WMC_max). A interseção
        # pode legitimamente dar vazio mesmo quando cada eixo isolado tem
        # sinal — por isso is_ce_outlier e is_wmc_outlier são reportados
        # também, separadamente.
        is_god = is_ce_outlier and is_wmc_outlier

        # abstração especulativa: interface/classe abstrata com no
        # máximo 1 implementação real E nenhum consumidor além dela
        # mesma — não gera ganho de desacoplamento na prática.
        is_speculative = jc.is_abstract and implementor_count <= 1 and ca <= implementor_count

        # implementação concreta sobre-dependida: classe concreta (não
        # interface/abstract/record/enum) da qual muitas outras dependem
        # diretamente, sem camada de abstração no meio — mesmo princípio
        # da Zona de Dor do "Por pacote", aplicado à classe.
        is_concrete_hotspot = (
            jc.kind == "class" and not jc.is_abstract
            and ca_threshold_concrete is not None and ca > ca_threshold_concrete
        )

        metrics[fqcn] = {
            "fqcn": fqcn,
            "label": jc.simple_name,
            "package": jc.package,
            "kind": jc.kind,
            "ca": ca,
            "ce": ce,
            "method_count": jc.method_count,
            "wmc": jc.wmc,
            "max_method_wmc": jc.max_method_wmc,
            "is_god": is_god,
            "is_concrete_hotspot": is_concrete_hotspot,
            "is_speculative": is_speculative,
            "is_ce_outlier": is_ce_outlier,
            "is_wmc_outlier": is_wmc_outlier,
        }

    thresholds = {"ce": ce_threshold, "max_method_wmc": wmc_threshold, "ca_concrete": ca_threshold_concrete}
    return metrics, thresholds


def _build_class_edges(classes: dict[str, _JavaClass]) -> list[tuple[str, str]]:
    edges: set[tuple[str, str]] = set()
    for fqcn, jc in classes.items():
        for dep in jc.deps:
            if dep in classes:
                edges.add((fqcn, dep))
    return sorted(edges)


# --- agregação por pacote ----------------------------------------------------


def _aggregate_by_package(classes: dict[str, _JavaClass]) -> tuple[list[dict], list[tuple[str, str]]]:
    """Constrói o grafo de pacotes a partir do grafo de classes já
    resolvido e aplica a metodologia de Martin (Ca, Ce, I, A, D, zona)
    sem adaptação — mesma leitura estrutural do "Por pacote" de
    coupling.py, só que alimentada pelo grafo por classe deste motor."""
    pkg_of = {fqcn: jc.package for fqcn, jc in classes.items()}
    pkg_classes: dict[str, list[str]] = defaultdict(list)
    for fqcn, jc in classes.items():
        pkg_classes[jc.package].append(fqcn)

    pkg_deps: dict[str, set[str]] = defaultdict(set)
    pkg_dependents: dict[str, set[str]] = defaultdict(set)
    for fqcn, jc in classes.items():
        src_pkg = jc.package
        for dep in jc.deps:
            if dep not in classes:
                continue
            dst_pkg = pkg_of[dep]
            if dst_pkg != src_pkg:
                pkg_deps[src_pkg].add(dst_pkg)
                pkg_dependents[dst_pkg].add(src_pkg)

    edges: set[tuple[str, str]] = set()
    for src, dsts in pkg_deps.items():
        for dst in dsts:
            edges.add((src, dst))

    rows = []
    for pkg, members in pkg_classes.items():
        ce = len(pkg_deps.get(pkg, set()))
        ca = len(pkg_dependents.get(pkg, set()))
        instability = (ce / (ce + ca)) if (ce + ca) > 0 else None
        abstract_count = sum(1 for m in members if classes[m].is_abstract)
        abstractness = (abstract_count / len(members)) if members else None

        if instability is None:
            zone = "isolado"
            distance = None
        else:
            zone = _zone(instability, abstractness)
            distance = abs((abstractness or 0) + instability - 1)

        rows.append({
            "module": pkg,
            "ce": ce,
            "ca": ca,
            "instability": None if instability is None else round(instability, 2),
            "abstractness": None if abstractness is None else round(abstractness, 2),
            "distance": None if distance is None else round(distance, 2),
            "zone": zone,
        })

    rows.sort(key=lambda r: (r["distance"] is None, -(r["distance"] or 0), r["module"]))
    return rows, sorted(edges)


# --- detecção de ciclos (DFS com pilha de recursão) -------------------------


def _find_cycles(adjacency: dict[str, set[str]], limit: int = 50) -> list[list[str]]:
    """Encontra ciclos no grafo dirigido via DFS. Não enumera TODOS os
    ciclos simples possíveis (isso explode combinatorialmente em grafos
    densos) — encontra um conjunto representativo de ciclos via
    back-edges, suficiente para apontar onde estão os problemas."""
    visited: set[str] = set()
    stack: list[str] = []
    on_stack: set[str] = set()
    cycles: list[list[str]] = []
    seen_signatures: set[frozenset] = set()

    def dfs(node):
        if len(cycles) >= limit:
            return
        visited.add(node)
        stack.append(node)
        on_stack.add(node)

        for neighbor in sorted(adjacency.get(node, [])):
            if len(cycles) >= limit:
                break
            if neighbor in on_stack:
                idx = stack.index(neighbor)
                cycle = stack[idx:] + [neighbor]
                signature = frozenset(cycle[:-1])
                if signature not in seen_signatures:
                    seen_signatures.add(signature)
                    cycles.append(cycle)
            elif neighbor not in visited:
                dfs(neighbor)

        stack.pop()
        on_stack.discard(node)

    for node in sorted(adjacency.keys()):
        if node not in visited and len(cycles) < limit:
            dfs(node)

    return cycles


# --- API pública --------------------------------------------------------------


def analyze(repo: str, surface: dict, config: dict | None = None) -> dict:
    """Motor de acoplamento especializado em Java: parse por regex do
    corpo de cada classe, grafo de dependência por classe E por pacote,
    ciclos, classe-deus, hotspot concreto e abstração especulativa.

    `surface` é aceito com a mesma assinatura do motor genérico (surface.py
    do codescan) mas não é usado aqui — este motor faz sua própria
    varredura do disco, pois precisa do CONTEÚDO de cada arquivo (corpo de
    classe, imports, supertypes), não só da contagem que o surface produz.
    """
    del surface  # não usado; motor Java varre o disco por conta própria
    repo = os.path.abspath(repo)
    cfg = _merge_config(config)

    classes, excluded = _scan_project(repo, cfg)
    unresolved_internal_imports = _resolve_dependencies(classes, repo)

    class_metrics, thresholds = _compute_class_metrics(classes)
    module_rows, pkg_edges = _aggregate_by_package(classes)
    class_edges = _build_class_edges(classes)

    class_adj = {fqcn: (jc.deps & set(classes.keys())) for fqcn, jc in classes.items()}
    pkg_adj: dict[str, set[str]] = defaultdict(set)
    for fqcn, jc in classes.items():
        for dep in jc.deps:
            if dep in classes and classes[dep].package != jc.package:
                pkg_adj[jc.package].add(classes[dep].package)

    cycles_classes_fqcn = _find_cycles(class_adj)
    cycles_packages = _find_cycles(dict(pkg_adj))
    fqcn_to_simple = {fqcn: jc.simple_name for fqcn, jc in classes.items()}
    cycles_classes = [[fqcn_to_simple[n] for n in cyc] for cyc in cycles_classes_fqcn]

    class_nodes = sorted(class_metrics.values(), key=lambda c: c["fqcn"])

    god_count = sum(1 for n in class_nodes if n["is_god"])
    speculative_count = sum(1 for n in class_nodes if n["is_speculative"])
    concrete_hotspot_count = sum(1 for n in class_nodes if n["is_concrete_hotspot"])
    ce_outlier_count = sum(1 for n in class_nodes if n["is_ce_outlier"])
    wmc_outlier_count = sum(1 for n in class_nodes if n["is_wmc_outlier"])

    return {
        "engine": "java",
        "modules": module_rows,
        "edges": pkg_edges,
        "unresolved_internal_imports": unresolved_internal_imports,
        "classes": class_nodes,
        "class_edges": class_edges,
        "cycles_classes": cycles_classes,
        "cycles_packages": cycles_packages,
        "thresholds": thresholds,
        "excluded": excluded,
        "summary": {
            "total_classes": len(class_nodes),
            "total_packages": len(module_rows),
            "total_excluded": len(excluded),
            "cycles_classes": len(cycles_classes),
            "cycles_packages": len(cycles_packages),
            "god_classes": god_count,
            "speculative_abstractions": speculative_count,
            "concrete_hotspots": concrete_hotspot_count,
            "ce_outliers": ce_outlier_count,
            "wmc_outliers": wmc_outlier_count,
        },
    }


_HAS_JAVA_SCAN_BUDGET = 20_000  # arquivos inspecionados antes de desistir — mantém o walk "curto"


def has_java(repo: str, surface: dict) -> bool:
    """True se o repo tem Java relevante o bastante para justificar o
    motor especializado. Barato: primeiro tenta o surface.json (já
    calculado pelo estágio 1) — só cai para uma caminhada curta em disco
    se o surface não trouxer essa informação, e para assim que acha o
    primeiro .java (ou depois de inspecionar um número limitado de
    arquivos, para nunca virar uma varredura completa por acidente)."""
    languages = (surface or {}).get("languages")
    if languages:
        java = languages.get(_JAVA_LANG)
        if java is not None:
            return int(java.get("files", 0) or 0) > 0

    repo = os.path.abspath(repo)
    skip_dirs = _combined_skip_dirs(DEFAULT_CONFIG)
    inspected = 0
    for dirpath, dirnames, filenames in os.walk(repo):
        dirnames[:] = [
            d for d in dirnames
            if not d.startswith(".") and d.lower() not in skip_dirs
        ]
        for fn in filenames:
            inspected += 1
            if fn.endswith(_JAVA_EXT) and not _is_generated(fn):
                return True
            if inspected >= _HAS_JAVA_SCAN_BUDGET:
                return False
    return False
