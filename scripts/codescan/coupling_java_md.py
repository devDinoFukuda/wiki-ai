"""Renderizador Markdown do analisador de acoplamento Java — pacote e classe.

Consome o dicionário `analysis` produzido pelo motor `coupling_java.py`
(contrato fixo do motor — este módulo não decide o que é sinal, só formata)
e devolve um artefato `source_type: code-repo`, no mesmo formato de front
matter que `coupling.py::render` já emite hoje.

Duas granularidades, dois critérios de validação — não é o mesmo gráfico em
duas escalas (ver docs internos do motor, seção "especificação de vistas"):

- **Por pacote**: métricas clássicas de Robert C. Martin (Ca, Ce, I, A, D,
  Main Sequence), sem adaptação — confirmadas contra o código-fonte do
  JDepend como sendo métricas de PACOTE, não de classe. Zonas de risco com
  limite estrutural fixo (0.5/0.5, vem da própria fórmula).
- **Por classe**: 4 sinais independentes, não uma fórmula combinada —
  ciclos de dependência, classe-deus (Ce **e** WMC máx outliers ao mesmo
  tempo), implementação concreta sobre-dependida (Ca outlier numa classe
  não-abstrata) e abstração especulativa (interface/abstrata com ≤1
  implementação e nenhum consumidor real). Todos os limiares numéricos são
  calculados por IQR a partir da distribuição real deste projeto — não são
  números fixos escolhidos a priori.

Todo bloco Mermaid gerado aqui respeita os gates do projeto (labels quoted,
IDs ASCII, uma declaração por linha, sem node id duplicado, sem bloco
vazio) — ver `references/sdd-contract.md` §1.6.
"""

from __future__ import annotations

import re
import unicodedata

# --- formatação --------------------------------------------------------------


def _fmt(v) -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:.2f}"
    return str(v)


def _fmt_threshold(v) -> str:
    return f"{v:.2f}" if v is not None else "N/A (poucas classes p/ estatística)"


def _ascii(text: str) -> str:
    raw = unicodedata.normalize("NFD", text)
    return "".join(ch for ch in raw if unicodedata.category(ch) != "Mn")


def _shorten(text: str, limit: int) -> str:
    """Trunca preservando INÍCIO e FIM (não só o início). Pacotes Java
    divergem no sufixo (ex.: br.com.acme...quote.application vs
    ...quote.domain) — truncar só à esquerda preserva o prefixo comum e
    descarta justo a parte discriminante. `tail` recebe 2/3 do espaço
    disponível (prioriza o sufixo); `head` fica com o 1/3 restante.
    Espelha `coupling_generic.py::_shorten` — mesmo formato nos dois
    motores."""
    if len(text) <= limit:
        return text
    if limit <= 1:
        return text[:limit]
    tail = (limit - 1) * 2 // 3
    head = limit - 1 - tail
    return text[:head] + "…" + text[len(text) - tail:]


def _mermaid_label_pair(text: str, limit: int = 64) -> tuple[str, str]:
    """Rótulo sanitizado para Mermaid: (completo, encurtado)."""
    label = re.sub(r"[\r\n\t]+", " ", str(text))
    label = re.sub(r"\s+", " ", label).strip() or "modulo"
    label = label.replace("\\", "/").replace('"', "'")
    return label, _shorten(label, limit)


def _mermaid_label(text: str, limit: int = 64) -> str:
    _full, label = _mermaid_label_pair(text, limit)
    return label


def _legend_block(pairs: dict[str, str]) -> list[str]:
    """Tabela de legenda logo após um bloco Mermaid: rótulo encurtado →
    nome completo. Só emitida se algum rótulo foi de fato encurtado;
    ordenada pelo nome completo (determinismo)."""
    if not pairs:
        return []
    lines = [
        "Legenda dos rótulos abreviados:",
        "",
        "| Rótulo no diagrama | Nome completo |",
        "|---|---|",
    ]
    for full in sorted(pairs):
        lines.append(f"| `{pairs[full]}` | `{full}` |")
    lines.append("")
    return lines


def _leaf(path: str) -> str:
    return path.replace("\\", "/").rstrip("/").split("/")[-1] or path


def _cycle_line(cycle: list[str]) -> str:
    return " -> ".join(cycle)


# --- diagrama Plano Abstração × Instabilidade (por pacote) -------------------
# Replica fielmente coupling.py::render — esse código já passa nos gates.

_MERMAID_POINT_RE = re.compile(r"^P[0-9]+$")


def _mermaid_point_name(index: int) -> str:
    """Identificador Mermaid ASCII, sem depender do nome do módulo."""
    if index < 1:
        raise ValueError("indice de ponto Mermaid deve iniciar em 1")
    return f"P{index:03d}"


def _mermaid_node_leaf(row: dict) -> tuple[str, str]:
    """Leaf do módulo sanitizado para Mermaid: (completo, encurtado)."""
    leaf = _leaf(row["module"])
    leaf = re.sub(r"[^A-Za-z0-9_.-]+", " ", leaf).strip() or "modulo"
    return leaf, _shorten(leaf, 36)


def _mermaid_node_label(row: dict) -> str:
    _full, leaf = _mermaid_node_leaf(row)
    instability = row.get("instability")
    abstractness = row.get("abstractness")
    i = "NA" if instability is None else f"{instability:.2f}"
    a = "NA" if abstractness is None else f"{abstractness:.2f}"
    return f"{leaf} | I={i} A={a}"


def _zone_key(zone: str) -> str:
    # Vocabulário do motor Java: dor, transição, saudável, inutilidade,
    # isolado, indeterminada (sem abstração). Só os 4 primeiros aparecem no
    # diagrama (isolado/indeterminada não têm par I/A completo, ver `pts`
    # abaixo) — o fallback ASCII cobre qualquer rótulo inesperado.
    mapped = {
        "dor": "dor",
        "transição": "transicao",
        "saudável": "saudavel",
        "inutilidade": "inutilidade",
    }
    if zone in mapped:
        return mapped[zone]
    return re.sub(r"[^A-Za-z0-9_]+", "_", _ascii(zone).lower()).strip("_") or "zona"


def _module_ids(mods: list[dict]) -> dict[str, str]:
    return {r["module"]: f"M{idx:03d}" for idx, r in enumerate(mods, start=1)}


# --- ciclos, render-safe (IDs ASCII sequenciais + labels quoted) -------------
# Diferente do relatório original (que usava o nome quoted como ID direto —
# não passa no gate "node id duplicado"/"referenciado sem definição" deste
# projeto): aqui cada nó do ciclo ganha um ID ASCII sequencial e é definido
# com `id["label"]` uma única vez.


def _mermaid_for_cycles(cycles: list[list[str]], id_prefix: str,
                         max_cycles: int = 15, max_nodes: int = 80
                         ) -> tuple[str, dict[str, str]]:
    """Devolve (corpo mermaid, legenda {completo: encurtado})."""
    if not cycles:
        return "", {}
    node_order: dict[str, None] = {}
    edge_order: dict[tuple[str, str], None] = {}
    truncated_cycles = len(cycles) > max_cycles
    for cyc in cycles[:max_cycles]:
        for name in cyc:
            node_order.setdefault(name, None)
        for a, b in zip(cyc, cyc[1:]):
            edge_order.setdefault((a, b), None)
    nodes = list(node_order.keys())
    truncated_nodes = len(nodes) > max_nodes
    nodes = nodes[:max_nodes]
    node_set = set(nodes)
    ids = {name: f"{id_prefix}{idx:03d}" for idx, name in enumerate(nodes, start=1)}
    edges = [(a, b) for (a, b) in edge_order if a in node_set and b in node_set]
    if not edges:
        return "", {}
    lines = ["```mermaid", "flowchart LR"]
    legend: dict[str, str] = {}
    for name in nodes:
        full, label = _mermaid_label_pair(name, limit=40)
        lines.append(f'  {ids[name]}["{label}"]')
        if label != full:
            legend[full] = label
    for a, b in edges:
        lines.append(f"  {ids[a]} --> {ids[b]}")
    lines.append("```")
    notes = []
    if truncated_cycles:
        notes.append(f"_(mostrando {max_cycles} de {len(cycles)} ciclos.)_")
    if truncated_nodes:
        notes.append(f"_(grafo de ciclos truncado a {max_nodes} nós.)_")
    body = "\n".join(lines)
    if notes:
        body += "\n\n" + "\n".join(notes)
    return body, legend


# --- API -----------------------------------------------------------------


def render(surface: dict, analysis: dict, topic: str | None, now: str) -> str:
    from .export import _origin, _repo_name

    repo = _repo_name(surface)
    mods = analysis.get("modules") or []
    classes = analysis.get("classes") or []
    class_edges = analysis.get("class_edges") or []
    edges = analysis.get("edges") or []
    cycles_classes = analysis.get("cycles_classes") or []
    cycles_packages = analysis.get("cycles_packages") or []
    thresholds = analysis.get("thresholds") or {}
    excluded = analysis.get("excluded") or []
    summary = analysis.get("summary") or {}

    fm = [
        "---",
        f"id: sb-codescan-{repo}-coupling",
        "source_type: code-repo",
        f'origin: "{_origin(surface)} — coupling (grafo de imports, motor Java)"',
        f"captured_at: {now}",
        "promoted: false",
        "promoted_by:",
        "promoted_at:",
        "confidence: reviewed",
    ]
    if topic:
        fm.append(f"topic: {topic}")
    fm += ["supersedes:", "source_link:", "---", ""]

    out = fm + [
        f"# Mapa de acoplamento — {repo} (motor Java)",
        "",
        "> **Por pacote** (métricas de Robert C. Martin, sem adaptação — mesma "
        "granularidade em que Ca/Ce/I/A/D/Main Sequence foram definidas, "
        "confirmado contra o código-fonte do JDepend): **Ca** = quantos "
        "pacotes dependem deste · **Ce** = de quantos pacotes este depende · "
        "**I** = Ce/(Ce+Ca) · **A** = proporção de classes abstratas no "
        "pacote · **D** = distância da Main Sequence (0=ideal).",
        "",
        "> **Por classe**: 4 sinais independentes, não uma fórmula combinada "
        "— ciclos de dependência, classe-deus (Ce **e** WMC máx outliers "
        "simultâneos), implementação concreta sobre-dependida (Ca outlier "
        "numa classe não-abstrata) e abstração especulativa (interface/"
        "abstrata com ≤1 implementação e nenhum consumidor real). Todos os "
        "limiares são calculados por IQR a partir da distribuição real deste "
        "projeto — ver `## Limiares calculados (IQR)`.",
        "",
        f"Resumo: {summary.get('total_classes', len(classes))} classe(s) em "
        f"{summary.get('total_packages', len(mods))} pacote(s) · "
        f"{summary.get('cycles_classes', len(cycles_classes))} ciclo(s) entre "
        f"classes · {summary.get('cycles_packages', len(cycles_packages))} "
        f"ciclo(s) entre pacotes · {summary.get('god_classes', 0)} classe(s)-"
        f"deus · {summary.get('concrete_hotspots', 0)} hotspot(s) de Ca · "
        f"{summary.get('speculative_abstractions', 0)} abstração(ões) "
        f"especulativa(s) · {summary.get('total_excluded', len(excluded))} "
        "arquivo(s) excluído(s) da análise.",
        "",
    ]

    # --- 1. Métricas por módulo (obrigatória) --------------------------------
    out += [
        "## Métricas por módulo",
        "",
        "| Módulo | Ce 🟢 | Ca 🟢 | I 🟢 | A 🟡 | D | Zona 🟡 |",
        "|---|---:|---:|---:|---:|---:|---|",
    ]
    for r in mods:
        out.append(
            f"| `{r['module']}` | {r['ce']} | {r['ca']} | {_fmt(r.get('instability'))} "
            f"| {_fmt(r.get('abstractness'))} | {_fmt(r.get('distance'))} | {r.get('zone')} |"
        )
    out.append("")

    # --- 2. Plano Abstração × Instabilidade (obrigatória) --------------------
    pts = [r for r in mods if r.get("instability") is not None and r.get("abstractness") is not None]
    if pts:
        zone_order = {"dor": 0, "transição": 1, "saudável": 2, "inutilidade": 3}
        diagram_pts = sorted(
            pts,
            key=lambda r: (
                zone_order.get(r.get("zone", ""), 99),
                -(r.get("distance") or 0),
                r.get("module", ""),
            ),
        )[:24]
        out += [
            "## Plano Abstração × Instabilidade",
            "",
            "Diagrama determinístico por zona; a tabela acima mantém todos os "
            "módulos (inclusive isolados/indeterminados, que não têm ponto "
            "plotável aqui por falta de I ou A).",
            "",
            "```mermaid",
            "flowchart LR",
            "  classDef dor fill:#fee2e2,stroke:#991b1b,color:#111827",
            "  classDef transicao fill:#fef3c7,stroke:#92400e,color:#111827",
            "  classDef saudavel fill:#dcfce7,stroke:#166534,color:#111827",
            "  classDef inutilidade fill:#f3e8ff,stroke:#7e22ce,color:#111827",
        ]
        grouped: dict[str, list[tuple[int, dict]]] = {}
        for idx, r in enumerate(diagram_pts, start=1):
            grouped.setdefault(_zone_key(r["zone"]), []).append((idx, r))
        plano_legend: dict[str, str] = {}
        for zid, title in (
            ("dor", "Dor"),
            ("transicao", "Transicao"),
            ("saudavel", "Saudavel"),
            ("inutilidade", "Inutilidade"),
        ):
            rows = grouped.get(zid) or []
            if not rows:
                continue
            out.append(f'  subgraph zone_{zid}["{title}"]')
            hub = f"Z{zid.upper()}"
            out.append(f'    {hub}["{title}"]')
            for idx, r in rows:
                node = _mermaid_point_name(idx)
                out.append(f'    {node}["{_mermaid_node_label(r)}"]')
                out.append(f"    {hub} --> {node}")
                full, short = _mermaid_node_leaf(r)
                if short != full:
                    plano_legend[full] = short
            out.append("  end")
            nodes = ",".join([hub] + [_mermaid_point_name(idx) for idx, _r in rows])
            out.append(f"  class {nodes} {zid}")
        out += ["```", ""]
        out += _legend_block(plano_legend)
    else:
        out += [
            "## Plano Abstração × Instabilidade",
            "",
            "(nenhum pacote com I e A calculáveis ao mesmo tempo — todos "
            "isolados ou sem abstração detectável; ver tabela acima.)",
            "",
        ]

    # --- 3. Grafo de dependências entre pacotes -------------------------------
    if len(mods) >= 2:
        ids = _module_ids(mods)
        filtered_edges = [(s, d) for s, d in edges if s in ids and d in ids]
        if filtered_edges:
            out += [
                "## Grafo de dependências entre pacotes",
                "",
                "Grafo completo de dependências entre pacotes (não só os "
                "ciclos) — visão geral de arquitetura, resolvida pelo motor "
                "Java (import por pacote, não regex genérico de linguagem).",
                "",
                "```mermaid",
                "flowchart LR",
            ]
            grafo_legend: dict[str, str] = {}
            for module, node in ids.items():
                full, label = _mermaid_label_pair(module)
                out.append(f'  {node}["{label}"]')
                if label != full:
                    grafo_legend[full] = label
            for s, d in filtered_edges:
                out.append(f"  {ids[s]} --> {ids[d]}")
            out += ["```", ""]
            out += _legend_block(grafo_legend)

    # --- 4. Dependências circulares --------------------------------------------
    out += ["## Dependências circulares", ""]
    out += ["### Classes", ""]
    if cycles_classes:
        for idx, cyc in enumerate(cycles_classes, 1):
            out.append(f"{idx}. `{_cycle_line(cyc)}`")
    else:
        out.append("Nenhuma dependência circular detectada entre classes.")
    out.append("")
    mermaid_cc, cc_legend = _mermaid_for_cycles(cycles_classes, "CC")
    if mermaid_cc:
        out += ["#### Grafo dos ciclos (classes)", "", mermaid_cc, ""]
        out += _legend_block(cc_legend)

    out += ["### Pacotes", ""]
    if cycles_packages:
        for idx, cyc in enumerate(cycles_packages, 1):
            out.append(f"{idx}. `{_cycle_line(cyc)}`")
    else:
        out.append("Nenhuma dependência circular detectada entre pacotes.")
    out.append("")
    mermaid_pc, pc_legend = _mermaid_for_cycles(cycles_packages, "PC")
    if mermaid_pc:
        out += ["#### Grafo dos ciclos (pacotes)", "", mermaid_pc, ""]
        out += _legend_block(pc_legend)

    # --- 5. Tabela de métricas — por classe -------------------------------------
    out += [
        "## Tabela de métricas — por classe",
        "",
        "Ordenada por WMC máx decrescente (desempate por FQCN). Sem coluna de "
        "zona/D de propósito — Main Sequence não se aplica nessa granularidade "
        "(A vira binário, geometricamente degenerado nessa escala).",
        "",
        "| Classe | Pacote | Tipo | Ca | Ce | Métodos | WMC soma | WMC max | Classe-deus | Hotspot Ca | Especulativa |",
        "|---|---|---|---:|---:|---:|---:|---:|---|---|---|",
    ]
    max_class_rows = 200
    ordered_classes = sorted(
        classes, key=lambda c: (-c.get("max_method_wmc", 0), c.get("fqcn", ""))
    )
    for c in ordered_classes[:max_class_rows]:
        out.append(
            f"| {c['label']} | {c['package']} | {c['kind']} | {c['ca']} | {c['ce']} | "
            f"{c['method_count']} | {c['wmc']} | {c['max_method_wmc']} | "
            f"{'sim' if c['is_god'] else 'não'} | {'sim' if c['is_concrete_hotspot'] else 'não'} | "
            f"{'sim' if c['is_speculative'] else 'não'} |"
        )
    out.append("")
    if len(ordered_classes) > max_class_rows:
        faltando = len(ordered_classes) - max_class_rows
        out.append(
            f"_({max_class_rows} de {len(ordered_classes)} classes mostradas — "
            f"{faltando} classe(s) fora deste limite, não listadas aqui.)_"
        )
        out.append("")

    # dependências por classe (para as listas abaixo) — derivadas de class_edges,
    # já que o contrato não traz depends_on/depended_by prontos por classe.
    label_by_fqcn = {c["fqcn"]: c.get("label", c["fqcn"]) for c in classes}
    depends_on: dict[str, list[str]] = {}
    depended_by: dict[str, list[str]] = {}
    for src, dst in class_edges:
        if src in label_by_fqcn and dst in label_by_fqcn:
            depends_on.setdefault(src, [])
            if label_by_fqcn[dst] not in depends_on[src]:
                depends_on[src].append(label_by_fqcn[dst])
            depended_by.setdefault(dst, [])
            if label_by_fqcn[src] not in depended_by[dst]:
                depended_by[dst].append(label_by_fqcn[src])
    for mapping in (depends_on, depended_by):
        for key in mapping:
            mapping[key] = sorted(mapping[key])

    def _dep_of(c: dict, mapping: dict[str, list[str]]) -> list[str]:
        return mapping.get(c["fqcn"], [])

    # --- 6. Classes-deus ---------------------------------------------------------
    god = [c for c in classes if c.get("is_god")]
    out += [
        "## Classes-deus",
        "",
        f"`is_god = Ce outlier (IQR, limiar={_fmt_threshold(thresholds.get('ce'))}) "
        f"E WMC máx outlier (IQR, limiar={_fmt_threshold(thresholds.get('max_method_wmc'))})`"
        " — as duas condições precisam ser verdadeiras ao mesmo tempo. Só Ce "
        "alto não basta: orquestradores/gateways/handlers legítimos têm Ce "
        "alto e WMC máx baixo. Interseção vazia é resultado legítimo em "
        "projetos bem decompostos, não falha do critério.",
        "",
    ]
    if god:
        for c in sorted(god, key=lambda c: (-c["max_method_wmc"], c["fqcn"])):
            dep = _dep_of(c, depends_on)
            out.append(
                f"- `{c['package']}.{c['label']}` — Ce={c['ce']}, WMC máx={c['max_method_wmc']} "
                f"(soma={c['wmc']} em {c['method_count']} método(s)) "
                f"(depende de: {', '.join(dep) or '(nenhuma)'})"
            )
    else:
        out.append("(nenhuma)")
    out.append("")

    # --- 7. Implementações concretas sobre-dependidas -----------------------------
    hotspots = [c for c in classes if c.get("is_concrete_hotspot")]
    out += [
        "## Implementações concretas sobre-dependidas",
        "",
        "`is_concrete_hotspot = classe concreta (não interface/abstract/"
        f"record/enum) com Ca outlier (IQR, limiar={_fmt_threshold(thresholds.get('ca_concrete'))}) "
        "dentro do subgrupo de classes concretas.` Mesmo princípio da Zona "
        "de Dor do \"Por pacote\": Ca alto sem abstração no meio significa "
        "que qualquer mudança de implementação atinge diretamente todo "
        "mundo que depende dela. Classe utilitária estática pode aparecer "
        "aqui com risco baixo na prática — avalie caso a caso, essa "
        "distinção não é feita automaticamente.",
        "",
    ]
    if hotspots:
        for c in sorted(hotspots, key=lambda c: (-c["ca"], c["fqcn"])):
            dep = _dep_of(c, depended_by)
            shown_dep = dep[:10]
            suffix = "..." if len(dep) > 10 else ""
            out.append(
                f"- `{c['package']}.{c['label']}` — Ca={c['ca']} "
                f"(usada por: {', '.join(shown_dep) or '(nenhuma)'}{suffix})"
            )
    else:
        out.append("(nenhuma)")
    out.append("")

    # --- 8. Abstrações especulativas -----------------------------------------------
    speculative = [c for c in classes if c.get("is_speculative")]
    out += [
        "## Abstrações especulativas",
        "",
        "`is_speculative = interface/classe abstrata com no máximo 1 "
        "implementação real e nenhum consumidor além dela mesma.` Fowler "
        "chama isso de *Speculative Generality* — abstração que não gera "
        "ganho de desacoplamento na prática. Candidatas a virar classe "
        "concreta direto (remover a abstração) ou a ganhar consumidores de "
        "verdade.",
        "",
    ]
    if speculative:
        for c in sorted(speculative, key=lambda c: (c["package"], c["label"])):
            dep = _dep_of(c, depended_by)
            out.append(
                f"- `{c['package']}.{c['label']}` — tipo={c['kind']}, Ca={c['ca']}, "
                f"usada por: {', '.join(dep) or '(nenhuma)'}"
            )
    else:
        out.append("(nenhuma)")
    out.append("")

    # --- 9. Limiares calculados (IQR) --------------------------------------------
    out += [
        "## Limiares calculados (IQR)",
        "",
        "Método: `limiar = Q3 + 1.5 * IQR` (regra do boxplot, Tukey 1977) "
        "sobre a distribuição real das classes deste projeto — não é número "
        "fixo escolhido a priori, é específico desta execução. Projeto com "
        "poucas classes pode não ter amostra suficiente para o cálculo "
        "(marcado como N/A abaixo).",
        "",
        f"- Ce (classe-deus, eixo amplitude): **{_fmt_threshold(thresholds.get('ce'))}**",
        f"- WMC máx (classe-deus, eixo profundidade): **{_fmt_threshold(thresholds.get('max_method_wmc'))}**",
        "- Ca dentro do subgrupo de classes concretas (implementação "
        f"sobre-dependida): **{_fmt_threshold(thresholds.get('ca_concrete'))}**",
        "",
    ]

    # --- 10. Arquivos excluídos da análise ------------------------------------------
    out += ["## Arquivos excluídos da análise", ""]
    if excluded:
        by_reason: dict[str, int] = {}
        for e in excluded:
            reason = e.get("reason", "?")
            by_reason[reason] = by_reason.get(reason, 0) + 1
        reasons_sorted = sorted(by_reason.items(), key=lambda kv: (-kv[1], kv[0]))
        out += [
            f"Total: {len(excluded)} arquivo(s), agrupados por motivo (não "
            "listados individualmente — ver exemplos abaixo).",
            "",
            "| Motivo | Quantidade |",
            "|---|---:|",
        ]
        out += [f"| {reason} | {count} |" for reason, count in reasons_sorted]
        out.append("")
        examples = sorted(excluded, key=lambda e: e.get("file_path", ""))[:20]
        out.append(f"Exemplos (até 20 de {len(excluded)}):")
        out.append("")
        out += [f"- `{e.get('file_path')}` — {e.get('reason')}" for e in examples]
        out.append("")
    else:
        out.append("Nenhum arquivo excluído.")
        out.append("")

    # --- 11. Aviso de resolução de imports ---------------------------------------
    if not edges and analysis.get("unresolved_internal_imports", 0):
        out += [
            "## Aviso de resolução de imports",
            "",
            "- 🔴 Imports internos detectados não foram resolvidos como arestas.",
            "",
        ]

    return "\n".join(out)
