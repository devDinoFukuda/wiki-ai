"""Renderizador HTML interativo do analisador de acoplamento Java.

Porta `html_report.py` (gráfico Main Sequence + toggle "Por classe"/"Por
pacote") para o contrato de artefatos do projeto: `render_html(surface,
analysis, now) -> str`, determinístico, autocontido (zero requisição
externa), sem dependência Python fora da stdlib.

O `analysis` recebido segue o contrato do motor `coupling_java.py`, que é
mais enxuto que o `report_data` do relatório original (não traz `id`,
`color`, `depends_on`/`depended_by` nem `implementor_count` prontos por nó)
— este módulo deriva tudo isso a partir de `edges`/`class_edges` antes de
serializar para o JSON embutido no HTML.
"""

from __future__ import annotations

import html
import json

# --- transformação analysis (contrato do motor) -> DATA (contrato do JS) ----


def _package_color(zone: str | None) -> str:
    return {
        "dor": "#C97B5F",
        "inutilidade": "#B08D3E",
        "saudável": "#7C8B5A",
    }.get(zone or "", "#7B8CA6")


def _class_color(c: dict) -> str:
    if c.get("is_god"):
        return "#C97B5F"
    if c.get("is_concrete_hotspot"):
        return "#8C6E82"
    if c.get("is_speculative"):
        return "#B08D3E"
    return "#7B8CA6"


def _is_abstract_kind(kind: str | None) -> bool:
    k = (kind or "").lower()
    return "interface" in k or "abstract" in k


def _build_data(analysis: dict, now: str, project: str) -> dict:
    mods = sorted(analysis.get("modules") or [], key=lambda m: m["module"])
    classes = sorted(analysis.get("classes") or [], key=lambda c: c["fqcn"])
    edges = sorted({(s, d) for s, d in (analysis.get("edges") or [])})
    class_edges = sorted({(s, d) for s, d in (analysis.get("class_edges") or [])})
    cycles_classes = [list(c) for c in (analysis.get("cycles_classes") or [])]
    cycles_packages = [list(c) for c in (analysis.get("cycles_packages") or [])]
    thresholds = analysis.get("thresholds") or {}
    summary = analysis.get("summary") or {}

    class_count_by_pkg: dict[str, int] = {}
    for c in classes:
        pkg = c.get("package", "")
        class_count_by_pkg[pkg] = class_count_by_pkg.get(pkg, 0) + 1

    pkg_dep_on: dict[str, set] = {}
    pkg_dep_by: dict[str, set] = {}
    for src, dst in edges:
        pkg_dep_on.setdefault(src, set()).add(dst)
        pkg_dep_by.setdefault(dst, set()).add(src)

    package_nodes = []
    for m in mods:
        module = m["module"]
        package_nodes.append({
            "id": module,
            "label": module,
            "class_count": class_count_by_pkg.get(module, 0),
            "ca": m.get("ca", 0),
            "ce": m.get("ce", 0),
            "i": m.get("instability"),
            "a": m.get("abstractness"),
            "d": m.get("distance"),
            "zone": m.get("zone"),
            "color": _package_color(m.get("zone")),
            "depends_on": sorted(pkg_dep_on.get(module, set())),
            "depended_by": sorted(pkg_dep_by.get(module, set())),
        })

    label_by_fqcn = {c["fqcn"]: c.get("label", c["fqcn"]) for c in classes}
    cls_dep_on: dict[str, set] = {}
    cls_dep_by: dict[str, set] = {}
    for src, dst in class_edges:
        if src in label_by_fqcn and dst in label_by_fqcn:
            cls_dep_on.setdefault(src, set()).add(label_by_fqcn[dst])
            cls_dep_by.setdefault(dst, set()).add(label_by_fqcn[src])

    class_nodes = []
    for c in classes:
        fqcn = c["fqcn"]
        class_nodes.append({
            "id": fqcn,
            "label": c.get("label", fqcn),
            "package": c.get("package", ""),
            "kind": c.get("kind", ""),
            "is_abstract": _is_abstract_kind(c.get("kind")),
            "ca": c.get("ca", 0),
            "ce": c.get("ce", 0),
            "method_count": c.get("method_count", 0),
            "wmc": c.get("wmc", 0),
            "max_method_wmc": c.get("max_method_wmc", 0),
            "is_god": bool(c.get("is_god")),
            "is_concrete_hotspot": bool(c.get("is_concrete_hotspot")),
            "is_speculative": bool(c.get("is_speculative")),
            "is_ce_outlier": bool(c.get("is_ce_outlier")),
            "is_wmc_outlier": bool(c.get("is_wmc_outlier")),
            "class_mode_color": _class_color(c),
            "depends_on": sorted(cls_dep_on.get(fqcn, set())),
            "depended_by": sorted(cls_dep_by.get(fqcn, set())),
        })

    return {
        "project": project,
        "generated_at": now,
        "summary": summary,
        "thresholds": {
            "ce": thresholds.get("ce"),
            "max_method_wmc": thresholds.get("max_method_wmc"),
            "ca_concrete": thresholds.get("ca_concrete"),
        },
        "classes": {"nodes": class_nodes},
        "packages": {"nodes": package_nodes},
        "cycles_classes": cycles_classes,
        "cycles_packages": cycles_packages,
    }


# --- template HTML (portado de html_report.py) -------------------------------

_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Mapa de acoplamento — __PROJECT_NAME__</title>
<style>
  :root {
    --bg: #FAF7F1;
    --card: #FFFFFF;
    --border: #E4DFD4;
    --ink: #1E1C18;
    --ink-soft: #6B6459;
    --terracota: #C97B5F;
    --ocre: #B08D3E;
    --oliva: #7C8B5A;
    --aco: #7B8CA6;
    --zona-dor-bg: #F6E9E4;
    --zona-inutil-bg: #F3EBD9;
    --zona-safe-bg: #EEF0E7;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #16140F;
      --card: #211E18;
      --border: #3A362C;
      --ink: #F1ECE1;
      --ink-soft: #B4AC9B;
      --zona-dor-bg: #3A2620;
      --zona-inutil-bg: #362E1A;
      --zona-safe-bg: #202A1E;
    }
  }
  :root[data-theme="dark"] {
    --bg: #16140F;
    --card: #211E18;
    --border: #3A362C;
    --ink: #F1ECE1;
    --ink-soft: #B4AC9B;
    --zona-dor-bg: #3A2620;
    --zona-inutil-bg: #362E1A;
    --zona-safe-bg: #202A1E;
  }
  :root[data-theme="light"] {
    --bg: #FAF7F1;
    --card: #FFFFFF;
    --border: #E4DFD4;
    --ink: #1E1C18;
    --ink-soft: #6B6459;
    --zona-dor-bg: #F6E9E4;
    --zona-inutil-bg: #F3EBD9;
    --zona-safe-bg: #EEF0E7;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    background: var(--bg);
    color: var(--ink);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
    padding: 40px 24px 80px;
  }
  .wrap { max-width: 1240px; margin: 0 auto; }
  h1 {
    font-family: Georgia, 'Iowan Old Style', 'Times New Roman', serif;
    font-size: 2.6rem;
    font-weight: 400;
    margin: 0 0 6px;
    letter-spacing: -0.01em;
  }
  h1 em { color: var(--terracota); font-style: italic; }
  .subtitle {
    max-width: 760px;
    color: var(--ink-soft);
    font-size: 1.02rem;
    line-height: 1.55;
    margin: 0 0 6px;
  }
  .hint { font-size: 0.86rem; color: var(--ink-soft); margin: 18px 0 28px; }
  .stats-row {
    display: flex; gap: 14px; flex-wrap: wrap; margin: 0 0 30px;
  }
  .stat-chip {
    background: var(--card); border: 1px solid var(--border); border-radius: 10px;
    padding: 10px 16px; font-size: 0.82rem; color: var(--ink-soft);
  }
  .stat-chip b { color: var(--ink); font-size: 1.05rem; display: block; font-family: ui-monospace, 'SF Mono', Consolas, monospace; }
  .toggle-row { display: flex; align-items: center; gap: 14px; margin-bottom: 18px; flex-wrap: wrap; }
  .toggle-group {
    display: inline-flex; background: var(--zona-safe-bg); border-radius: 999px; padding: 3px;
  }
  .toggle-group button {
    border: none; background: transparent; padding: 9px 20px; border-radius: 999px;
    font-size: 0.86rem; cursor: pointer; color: var(--ink-soft); font-weight: 500;
  }
  .toggle-group button.active { background: var(--ink); color: var(--bg); }
  .count-note { color: var(--ink-soft); font-size: 0.85rem; }
  .main-grid { display: grid; grid-template-columns: 2.6fr 1fr; gap: 22px; align-items: start; }
  @media (max-width: 900px) { .main-grid { grid-template-columns: 1fr; } }
  .main-grid > * { min-width: 0; }
  .panel {
    background: var(--card); border: 1px solid var(--border); border-radius: 14px;
    padding: 26px; position: relative;
  }
  .chart-panel { min-height: 560px; overflow: hidden; }
  svg { width: 100%; height: auto; display: block; overflow: hidden; border-radius: 8px; }
  .axis-label { font-size: 11px; fill: var(--ink-soft); font-family: ui-monospace, 'SF Mono', Consolas, monospace; letter-spacing: 0.02em; }
  .quadrant-label { font-size: 10.5px; fill: var(--ink-soft); letter-spacing: 0.06em; font-family: ui-monospace, 'SF Mono', Consolas, monospace; text-transform: uppercase; }
  .quadrant-label.danger { fill: var(--terracota); font-weight: 600; }
  .quadrant-label.warn { fill: var(--ocre); font-weight: 600; }
  .ms-line { stroke: var(--ink-soft); stroke-width: 1.4; stroke-dasharray: 5 5; opacity: 0.55; }
  .ms-text { font-size: 9.5px; fill: var(--ink-soft); font-family: ui-monospace, monospace; letter-spacing: 0.04em; }
  .node { cursor: pointer; stroke-width: 1.4; stroke: var(--card); transition: opacity .12s; }
  .node:hover { opacity: 0.85; }
  .node.dim { opacity: 0.18; }
  .node.god { stroke: var(--ink); stroke-width: 3.2; }
  .dep-line { stroke-width: 1; fill: none; opacity: 0.9; }
  .dep-line.out { stroke: var(--terracota); }
  .dep-line.in { stroke: var(--aco); stroke-dasharray: 4 3; }

  .tooltip {
    position: fixed; background: var(--card);
    border: 1px solid var(--border); border-radius: 12px; padding: 18px 20px;
    box-shadow: 0 12px 32px rgba(30,28,24,0.24); font-size: 0.86rem; z-index: 999;
    pointer-events: auto; overflow-wrap: break-word; overflow-y: auto;
    box-sizing: border-box; color: var(--ink);
  }
  .tooltip .path { font-family: ui-monospace, 'SF Mono', Consolas, monospace; font-size: 0.78rem; color: var(--ink-soft); margin-bottom: 2px; }
  .tooltip .title { font-family: Georgia, serif; font-size: 1.05rem; margin: 0 0 12px; }
  .tooltip .metrics { display: grid; grid-template-columns: 1fr auto; gap: 4px 10px; font-family: ui-monospace, monospace; font-size: 0.8rem; margin-bottom: 10px; }
  .tooltip .metrics span:nth-child(odd) { color: var(--ink-soft); }
  .tooltip .metrics span:nth-child(even) { font-weight: 600; text-align: right; }
  .tooltip .explain { line-height: 1.5; margin: 0 0 10px; color: var(--ink); }
  .tooltip .depline { font-size: 0.78rem; line-height: 1.5; }
  .tooltip .depline b { color: var(--ink); }

  .legend-title { font-family: Georgia, serif; font-size: 1.15rem; margin: 0 0 14px; display:flex; align-items:center; gap:8px;}
  .legend-title .num { color: var(--terracota); font-family: ui-monospace, monospace; font-size: 0.8rem; }
  .legend-item { display: flex; gap: 10px; align-items: flex-start; margin-bottom: 12px; font-size: 0.86rem; line-height: 1.45; }
  .dot { width: 12px; height: 12px; border-radius: 50%; margin-top: 3px; flex-shrink: 0; }
  .legend-divider { border: none; border-top: 1px solid var(--border); margin: 16px 0; }
  .legend-small { font-size: 0.78rem; color: var(--ink-soft); text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 10px; }

  .cycles-panel { margin-top: 22px; }
  .cycle-item { font-family: ui-monospace, 'SF Mono', Consolas, monospace; font-size: 0.8rem; line-height: 1.9; padding: 12px 0; border-bottom: 1px solid var(--border); overflow-wrap: break-word; word-break: break-word; }
  .cycle-item:last-child { border-bottom: none; }
  .cycle-item .arrow { color: var(--ink-soft); padding: 0 4px; }
  .cycle-item .node-name { color: var(--terracota); }
  .empty-note { color: var(--ink-soft); font-size: 0.86rem; }
  .footer-note { color: var(--ink-soft); font-size: 0.78rem; margin-top: 40px; }
</style>
</head>
<body>
<div class="wrap">
  <h1>Mapa de <em>acoplamento</em> — __PROJECT_NAME__</h1>
  <p class="subtitle">
    Motor Java. <b>Por pacote</b> usa as metricas classicas de Robert C. Martin: abstracao (A) x
    instabilidade (I), com a <i>Main Sequence</i> como equilibrio ideal — essa e a
    granularidade em que essas metricas foram concebidas. <b>Por classe</b> usa outro
    eixo, pensado pra essa granularidade: acoplamento (Ce) x complexidade (WMC) —
    ajuda a achar classes que fazem muita coisa E dependem de muita coisa ao mesmo tempo.
  </p>
  <p class="hint">Passe o mouse sobre um ponto para ver metricas e dependencias &middot; clique para fixar &middot; gerado em __GENERATED_AT__</p>

  <div class="stats-row" id="statsRow"></div>

  <div class="toggle-row">
    <div class="toggle-group">
      <button id="btnByClass" class="active">Por classe</button>
      <button id="btnByPackage">Por pacote</button>
    </div>
    <span class="count-note" id="countNote"></span>
  </div>

  <div class="main-grid">
    <div class="panel chart-panel">
      <svg id="chart" width="760" height="620" viewBox="0 0 760 620" preserveAspectRatio="xMidYMid meet"></svg>
    </div>
    <div id="sidebarColumn">
      <div class="panel" id="legendClasses">
        <p class="legend-title"><span class="num">01</span> Como ler — por classe</p>
        <p class="legend-small">Fundo do quadrante (contexto)</p>
        <div class="legend-item"><span class="dot" style="background:#EEF0E7"></span><div><b>Verde-claro</b> — simples, ou orquestra/delega sem complexidade interna.</div></div>
        <div class="legend-item"><span class="dot" style="background:#F3EBD9"></span><div><b>Ocre-claro</b> — complexa por dentro, mas isolada (poucas dependencias).</div></div>
        <div class="legend-item"><span class="dot" style="background:#F6E9E4"></span><div><b>Terracota-claro</b> — candidata a classe-deus: acoplamento E complexidade fora do padrao do projeto.</div></div>
        <hr class="legend-divider">
        <p class="legend-small">Cor do ponto (classificacao)</p>
        <div class="legend-item"><span class="dot" style="background:#7B8CA6"></span><div><b>Azul-aco</b> — classe comum, nada fora do padrao.</div></div>
        <div class="legend-item"><span class="dot" style="background:#C97B5F"></span><div><b>Terracota</b> — classe-deus: Ce e WMC max fora do padrao ao mesmo tempo.</div></div>
        <div class="legend-item"><span class="dot" style="background:#8C6E82"></span><div><b>Ameixa</b> — implementacao concreta sobre-dependida: Ca fora do padrao numa classe (nao interface/record/enum). Pode aparecer em qualquer quadrante, ja que Ca nao e um dos dois eixos do grafico.</div></div>
        <div class="legend-item"><span class="dot" style="background:#B08D3E"></span><div><b>Ocre</b> — abstracao especulativa: no maximo 1 implementacao, sem consumidor real. Tambem pode aparecer em qualquer quadrante.</div></div>
        <hr class="legend-divider">
        <div class="legend-item"><span style="width:18px;height:18px;border-radius:50%;background:#ccc;margin-top:1px;flex-shrink:0;"></span><div><b>Tamanho</b> = acoplamento total (Ca + Ce). Maior &rarr; mexer afeta mais coisa.</div></div>
        <div class="legend-item"><span style="width:14px;height:14px;border-radius:50%;background:#C97B5F;border:3px solid var(--ink);margin-top:2px;flex-shrink:0;"></span><div><b>Anel grosso</b> = classe-deus (destaque extra).</div></div>
        <hr class="legend-divider">
        <p class="legend-small">Eixos</p>
        <div class="legend-item"><div><b>X = Ce</b> (de quantas classes esta depende) · <b>Y = WMC max</b> (complexidade do metodo mais complexo da classe, nao a soma). As linhas centrais marcam limiares <b>calculados automaticamente</b> (outlier estatistico, especifico deste projeto — nao e numero configurado manualmente).</div></div>
        <hr class="legend-divider">
        <p class="legend-small">Ao clicar em um ponto</p>
        <div class="legend-item"><span style="width:14px;height:2px;background:var(--terracota);margin-top:9px;flex-shrink:0;"></span><div><b>Depende de</b> (Ce) — linhas solidas que saem da classe.</div></div>
        <div class="legend-item"><span style="width:14px;height:2px;background:var(--aco);margin-top:9px;flex-shrink:0;border-top:2px dashed var(--aco);"></span><div><b>Dependem dela</b> (Ca) — linhas tracejadas que entram.</div></div>
      </div>

      <div class="panel" id="legendPackages" style="display:none;">
        <p class="legend-title"><span class="num">01</span> Como ler — por pacote</p>
        <div class="legend-item"><span class="dot" style="background:#7C8B5A"></span><div><b>Verde-oliva</b> — perto da Main Sequence. Saudavel.</div></div>
        <div class="legend-item"><span class="dot" style="background:#C97B5F"></span><div><b>Terracota</b> — zona de dor: estavel e concreto.</div></div>
        <div class="legend-item"><span class="dot" style="background:#B08D3E"></span><div><b>Ocre</b> — zona de inutilidade: instavel e abstrato.</div></div>
        <div class="legend-item"><span class="dot" style="background:#7B8CA6"></span><div><b>Azul-aco</b> — fora da linha, sem risco, isolado ou sem abstracao detectavel.</div></div>
        <hr class="legend-divider">
        <div class="legend-item"><span style="width:18px;height:18px;border-radius:50%;background:#ccc;margin-top:1px;flex-shrink:0;"></span><div><b>Tamanho</b> = acoplamento total (Ca + Ce). Maior &rarr; mexer afeta mais coisa.</div></div>
        <hr class="legend-divider">
        <p class="legend-small">Ao clicar em um ponto</p>
        <div class="legend-item"><span style="width:14px;height:2px;background:var(--terracota);margin-top:9px;flex-shrink:0;"></span><div><b>Depende de</b> (Ce) — linhas solidas que saem do pacote.</div></div>
        <div class="legend-item"><span style="width:14px;height:2px;background:var(--aco);margin-top:9px;flex-shrink:0;border-top:2px dashed var(--aco);"></span><div><b>Dependem dele</b> (Ca) — linhas tracejadas que entram.</div></div>
      </div>

      <div class="panel cycles-panel">
        <p class="legend-title"><span class="num">02</span> Dependencias circulares</p>
        <div id="cyclesBox"></div>
      </div>

      <div class="panel cycles-panel" id="speculativePanel">
        <p class="legend-title"><span class="num">03</span> Abstracoes especulativas</p>
        <div id="speculativeBox"></div>
      </div>

      <div class="panel cycles-panel" id="hotspotPanel">
        <p class="legend-title"><span class="num">04</span> Implementacoes concretas sobre-dependidas</p>
        <div id="hotspotBox"></div>
      </div>
    </div>
  </div>
  <p class="footer-note">Gerado deterministicamente por coupling_java_html.py em __GENERATED_AT__ &middot; sem requisicao externa.</p>
</div>


<script id="report-data" type="application/json">__DATA_JSON__</script>
<script>
const DATA = JSON.parse(document.getElementById('report-data').textContent);
let mode = 'classes';
let pinned = null;
let hovered = null;

function fmtNum(v) {
  return (v === null || v === undefined) ? '—' : v.toFixed(2);
}
function numOr(v, d) {
  return (v === null || v === undefined) ? d : v;
}

const svgNS = "http://www.w3.org/2000/svg";
const svg = document.getElementById('chart');
const PAD = { l: 56, r: 30, t: 30, b: 54 };
const W = 760, H = 620;
const plotW = W - PAD.l - PAD.r;
const plotH = H - PAD.t - PAD.b;

// --- escalas dependem do modo ---
// Por pacote usa I/A normalizado [0,1] (metrica classica de Martin).
// Por classe usa Ce/WMC em escala DINAMICA (os valores reais variam
// bastante -- normalizar em [0,1] fixo faria tudo colar nas bordas de
// novo, o mesmo problema que a versao antiga tinha).
function xPix(i) { return PAD.l + i * plotW; }
function yPix(a) { return PAD.t + (1 - a) * plotH; }

let classScales = null; // recalculado a cada renderChart() em modo classe

function computeClassScales(nodes) {
  const th = DATA.thresholds || {};
  const ceMax = Math.max(1, ...nodes.map(n => n.ce));
  const wmcMax = Math.max(1, ...nodes.map(n => n.max_method_wmc));

  // limiares calculados por IQR no backend (nao numero fixo configuravel).
  // Em projetos muito pequenos (< 4 classes) nao da pra calcular
  // estatistica com sentido -- o backend manda null nesse caso, e aqui
  // usamos so o maior valor real como referencia de escala (sem desenhar
  // quadrante/linha de limiar com peso estatistico, ja que nao existe).
  const ceThreshold = numOr(th.ce, ceMax);
  const wmcThreshold = numOr(th.max_method_wmc, wmcMax);

  // eixo SIMETRICO: o limiar fica EXATAMENTE no centro (50%), igual a
  // Main Sequence do "Por pacote". Com escala em raiz quadrada,
  // sqrt(limiar)/sqrt(4*limiar) = 1/2 exato -- garantido
  // matematicamente, NAO depende do maior valor real (valores acima de
  // 2x o limiar sao limitados na borda de proposito -- outlier extremo
  // deve bater na borda visualmente, isso reforca que e extremo, sem
  // distorcer a proporcao de todo mundo em volta).
  const ceAxisMax = ceThreshold * 4;
  const wmcAxisMax = wmcThreshold * 4;
  const ceScaleMax = Math.sqrt(ceAxisMax);
  const wmcScaleMax = Math.sqrt(wmcAxisMax);

  return {
    ceAxisMax, wmcAxisMax, ceThreshold, wmcThreshold,
    hasCeThreshold: th.ce !== null && th.ce !== undefined,
    hasWmcThreshold: th.max_method_wmc !== null && th.max_method_wmc !== undefined,
    xOf: (ce) => PAD.l + Math.min(1, Math.sqrt(ce) / ceScaleMax) * plotW,
    yOf: (wmc) => PAD.t + (1 - Math.min(1, Math.sqrt(wmc) / wmcScaleMax)) * plotH,
  };
}

function rawPos(n) {
  if (mode === 'classes') {
    return { cx: classScales.xOf(n.ce), cy: classScales.yOf(n.max_method_wmc) };
  }
  // pacotes isolados (Ce=Ca=0) ou sem abstracao detectavel tem i/a nulos --
  // sem posicao estatistica real; cai na origem do quadrante "estavel e
  // abstrato" por convencao de exibicao, o tooltip explica o motivo.
  return { cx: xPix(numOr(n.i, 0)), cy: yPix(numOr(n.a, 0)) };
}

// Varios pontos podem ter EXATAMENTE o mesmo valor de dado (ex: 5 classes
// com Ce=0 e WMC=0 -- bem comum: classes so com campos, interfaces sem
// metodo default). Jitter aleatorio nao garante separacao visual quando
// isso acontece. Em vez disso: agrupa por posicao base identica e arruma
// os membros do grupo em um anel ao redor do centro compartilhado --
// garante que ficam visualmente distintos, nao sobrepostos.
let nodeFinalPos = {};

function computeFinalPositions(nodes) {
  const basePos = {};
  nodes.forEach(n => { basePos[n.id] = rawPos(n); });

  const groups = {};
  nodes.forEach(n => {
    const p = basePos[n.id];
    const key = Math.round(p.cx) + '_' + Math.round(p.cy);
    (groups[key] = groups[key] || []).push(n.id);
  });

  const finalPos = {};
  Object.keys(groups).forEach(key => {
    const ids = groups[key].slice().sort();
    const center = basePos[ids[0]];
    if (ids.length === 1) {
      finalPos[ids[0]] = center;
      return;
    }
    const ringR = 9 + 3 * Math.sqrt(ids.length);
    ids.forEach((id, idx) => {
      const angle = (2 * Math.PI * idx) / ids.length;
      finalPos[id] = {
        cx: Math.max(4, Math.min(W - 4, center.cx + ringR * Math.cos(angle))),
        cy: Math.max(4, Math.min(H - 4, center.cy + ringR * Math.sin(angle))),
      };
    });
  });
  return finalPos;
}

function nodePos(n) {
  return nodeFinalPos[n.id] || rawPos(n);
}

function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); }

function el(tag, attrs, text) {
  const e = document.createElementNS(svgNS, tag);
  for (const k in attrs) e.setAttribute(k, attrs[k]);
  if (text !== undefined) e.textContent = text;
  return e;
}

function currentDataset() { return mode === 'classes' ? DATA.classes : DATA.packages; }

function renderStats() {
  const box = document.getElementById('statsRow');
  clear(box);
  const d = DATA.summary || {};
  const chips = [
    [numOr(d.total_classes, DATA.classes.nodes.length), 'classes analisadas'],
    [numOr(d.total_packages, DATA.packages.nodes.length), 'pacotes'],
    [numOr(d.cycles_classes, DATA.cycles_classes.length), 'ciclos (classes)'],
    [numOr(d.cycles_packages, DATA.cycles_packages.length), 'ciclos (pacotes)'],
  ];
  chips.forEach(([n, label]) => {
    const c = document.createElement('div');
    c.className = 'stat-chip';
    c.innerHTML = '<b>' + n + '</b>' + label;
    box.appendChild(c);
  });
}

let nodeElements = {};      // id -> circle DOM element (persistente entre interacoes)
let depLinesGroup = null;
let nodesGroup = null;

function renderChart() {
  clear(svg);
  svg.setAttribute('viewBox', '0 0 ' + W + ' ' + H);
  const nodes = currentDataset().nodes;

  if (mode === 'classes') {
    renderClassChartBackground(nodes);
  } else {
    renderPackageChartBackground();
  }

  nodeFinalPos = computeFinalPositions(nodes);

  // grupo para linhas de dependencia (atualizado sem tocar nos circulos)
  depLinesGroup = el('g', { id: 'depLinesGroup' });
  svg.appendChild(depLinesGroup);

  // grupo para os pontos — criados UMA vez aqui; interacoes (hover/clique)
  // nunca destroem/recriam esses elementos depois disso. Isso evita que o
  // navegador recalcule :hover sobre um elemento novo debaixo do cursor e
  // reabra o pop-up sozinho (era a causa do "travamento").
  nodesGroup = el('g', { id: 'nodesGroup' });
  svg.appendChild(nodesGroup);

  nodeElements = {};
  const sizes = nodes.map(n => n.ca + n.ce);
  const maxSize = Math.max(1, ...sizes);
  nodes.forEach(n => {
    const { cx, cy } = nodePos(n);
    const r = 5 + 11 * Math.sqrt((n.ca + n.ce) / maxSize);
    const fillColor = mode === 'classes' ? n.class_mode_color : n.color;
    const c = el('circle', {
      cx, cy, r: r.toFixed(1), fill: fillColor,
      class: 'node' + (n.is_god ? ' god' : ''),
      'data-id': n.id
    });
    c.addEventListener('mouseenter', () => {
      if (pinned !== null) return;   // um ponto fixado ignora hover em outros
      hovered = n.id;
      updateOverlay();
      showTooltipFor(n.id);
    });
    c.addEventListener('mouseleave', () => {
      if (pinned !== null) return;
      hovered = null;
      updateOverlay();
      hideTooltip();
    });
    c.addEventListener('click', (e) => {
      e.stopPropagation();
      if (pinned === n.id) {
        pinned = null; hovered = null; hideTooltip();
      } else {
        pinned = n.id; hovered = n.id;
      }
      updateOverlay();
      if (pinned) showTooltipFor(pinned);
    });
    nodeElements[n.id] = c;
    nodesGroup.appendChild(c);
  });

  updateOverlay();
}

function renderPackageChartBackground() {
  // fundos dos quadrantes
  const midX = xPix(0.5), midY = yPix(0.5);
  svg.appendChild(el('rect', { x: PAD.l, y: PAD.t, width: midX - PAD.l, height: midY - PAD.t, fill: 'var(--zona-safe-bg)' }));
  svg.appendChild(el('rect', { x: midX, y: PAD.t, width: PAD.l + plotW - midX, height: midY - PAD.t, fill: 'var(--zona-inutil-bg)' }));
  svg.appendChild(el('rect', { x: PAD.l, y: midY, width: midX - PAD.l, height: PAD.t + plotH - midY, fill: 'var(--zona-dor-bg)' }));
  svg.appendChild(el('rect', { x: midX, y: midY, width: PAD.l + plotW - midX, height: PAD.t + plotH - midY, fill: 'var(--zona-safe-bg)' }));

  // main sequence
  svg.appendChild(el('line', { x1: xPix(0), y1: yPix(1), x2: xPix(1), y2: yPix(0), class: 'ms-line' }));

  // quadrant labels
  svg.appendChild(el('text', { x: PAD.l + 8, y: PAD.t + 16, class: 'quadrant-label' }, 'estavel e abstrato'));
  svg.appendChild(el('text', { x: PAD.l + plotW - 8, y: PAD.t + 16, class: 'quadrant-label warn', 'text-anchor': 'end' }, 'zona de inutilidade'));
  svg.appendChild(el('text', { x: PAD.l + 8, y: PAD.t + plotH - 10, class: 'quadrant-label danger' }, 'zona de dor'));
  svg.appendChild(el('text', { x: PAD.l + plotW - 8, y: PAD.t + plotH - 10, class: 'quadrant-label', 'text-anchor': 'end' }, 'instavel e concreto'));

  // axis labels
  svg.appendChild(el('text', { x: PAD.l + plotW / 2, y: H - 14, class: 'axis-label', 'text-anchor': 'middle' },
    'I — instabilidade · estavel → instavel'));
  const yLabel = el('text', { x: -(PAD.t + plotH / 2), y: 16, class: 'axis-label', 'text-anchor': 'middle',
    transform: 'rotate(-90)' }, 'A — abstracao · concreto → abstrato');
  svg.appendChild(yLabel);
}

function renderClassChartBackground(nodes) {
  classScales = computeClassScales(nodes);
  const ceX = classScales.xOf(classScales.ceThreshold);
  const wmcY = classScales.yOf(classScales.wmcThreshold);

  // 4 quadrantes completos, mesmo padrao visual do "Por pacote":
  // fundo colorido + rotulo em todos, nao so no canto de risco.
  //   sup-esquerdo (Ce baixo, WMC alto)  = complexa mas isolada -- ocre
  //   sup-direito  (Ce alto,  WMC alto)  = candidata a classe-deus -- terracota
  //   inf-esquerdo (Ce baixo, WMC baixo) = simples -- verde/neutro
  //   inf-direito  (Ce alto,  WMC baixo) = orquestra/delega -- verde/neutro
  svg.appendChild(el('rect', { x: PAD.l, y: PAD.t, width: ceX - PAD.l, height: wmcY - PAD.t, fill: 'var(--zona-inutil-bg)' }));
  svg.appendChild(el('rect', { x: ceX, y: PAD.t, width: (PAD.l + plotW) - ceX, height: wmcY - PAD.t, fill: 'var(--zona-dor-bg)' }));
  svg.appendChild(el('rect', { x: PAD.l, y: wmcY, width: ceX - PAD.l, height: (PAD.t + plotH) - wmcY, fill: 'var(--zona-safe-bg)' }));
  svg.appendChild(el('rect', { x: ceX, y: wmcY, width: (PAD.l + plotW) - ceX, height: (PAD.t + plotH) - wmcY, fill: 'var(--zona-safe-bg)' }));

  // linhas de limiar -- EXATAMENTE no centro (50%), mesmo peso visual da
  // Main Sequence do "Por pacote". Se nao houver dado suficiente pra
  // calcular limiar estatistico (projeto pequeno), a linha ainda
  // aparece (usando o maior valor real como referencia), mas o rotulo
  // avisa que e so posicional, sem estatistica por tras.
  svg.appendChild(el('line', { x1: ceX, y1: PAD.t, x2: ceX, y2: PAD.t + plotH, class: 'ms-line' }));
  svg.appendChild(el('line', { x1: PAD.l, y1: wmcY, x2: PAD.l + plotW, y2: wmcY, class: 'ms-line' }));

  const ceLabel = classScales.hasCeThreshold ? 'complexa e isolada' : 'complexa e isolada (sem estatistica*)';
  svg.appendChild(el('text', { x: PAD.l + 8, y: PAD.t + 16, class: 'quadrant-label warn' }, ceLabel));
  svg.appendChild(el('text', { x: PAD.l + plotW - 8, y: PAD.t + 16, class: 'quadrant-label danger', 'text-anchor': 'end' }, 'candidata a classe-deus'));
  svg.appendChild(el('text', { x: PAD.l + 8, y: PAD.t + plotH - 10, class: 'quadrant-label' }, 'simples'));
  svg.appendChild(el('text', { x: PAD.l + plotW - 8, y: PAD.t + plotH - 10, class: 'quadrant-label', 'text-anchor': 'end' }, 'orquestra / delega'));

  // axis labels
  svg.appendChild(el('text', { x: PAD.l + plotW / 2, y: H - 14, class: 'axis-label', 'text-anchor': 'middle' },
    'Ce — acoplamento eferente (de quantas classes depende)'));
  const yLabel = el('text', { x: -(PAD.t + plotH / 2), y: 16, class: 'axis-label', 'text-anchor': 'middle',
    transform: 'rotate(-90)' }, 'WMC max — complexidade do metodo mais complexo');
  svg.appendChild(yLabel);
}

// atualiza apenas linhas de dependencia + destaque (dim) dos pontos
// existentes — nunca remove/recria os <circle>, entao nenhuma interacao
// (hover, clique, clicar fora, Esc) mexe na identidade dos elementos.
function updateOverlay() {
  if (!depLinesGroup) return;
  clear(depLinesGroup);

  const nodes = currentDataset().nodes;
  const byId = {};
  nodes.forEach(n => byId[n.id] = n);
  const active = pinned || hovered;

  nodes.forEach(n => {
    const c = nodeElements[n.id];
    if (!c) return;
    const isDim = active && active !== n.id && !(byId[active] && ((byId[active].depends_on_ids||[]).includes(n.id) || (byId[active].depended_by_ids||[]).includes(n.id)));
    c.classList.toggle('dim', !!isDim);
  });

  if (active && byId[active]) {
    const n = byId[active];
    const { cx, cy } = nodePos(n);
    (n.depends_on_ids || []).forEach(tid => {
      const t = byId[tid]; if (!t) return;
      const { cx: tx, cy: ty } = nodePos(t);
      depLinesGroup.appendChild(el('line', { x1: cx, y1: cy, x2: tx, y2: ty, class: 'dep-line out' }));
    });
    (n.depended_by_ids || []).forEach(sid => {
      const s = byId[sid]; if (!s) return;
      const { cx: sx, cy: sy } = nodePos(s);
      depLinesGroup.appendChild(el('line', { x1: sx, y1: sy, x2: cx, y2: cy, class: 'dep-line in' }));
    });
  }
}

let tooltipEl = null;
function hideTooltip() { if (tooltipEl) { tooltipEl.remove(); tooltipEl = null; } }

function explainPackage(n) {
  const zoneText = {
    'saudável': 'Perto do equilibrio ideal (A + I ≈ 1) → saudavel pela metrica.',
    'transição': 'Fora da Main Sequence, mas fora das zonas de risco — nao e prioridade.',
    'dor': 'Estavel (I baixo) e concreto (sem contrato). O quanto isso doi depende do Ca: ' + n.ca + ' depende(m) dela. Se for um pacote de dominio/modelo central, isso costuma ser esperado, nao necessariamente um problema.',
    'inutilidade': 'Abstrata (A alto), mas instavel — poucas garantias de quem depende disso ficar estavel.',
    'isolado': 'Sem dependencia de entrada nem de saida entre pacotes internos (Ca=0 e Ce=0) — instabilidade nao calculavel, posicao no grafico e so convencao.',
    'indeterminada (sem abstração)': 'Nao foi possivel detectar interface/classe abstrata neste pacote para calcular A — zona nao classificavel, posicao no grafico e so convencao.'
  };
  return zoneText[n.zone] || ('Zona "' + n.zone + '" sem descricao cadastrada.');
}

function explainClass(n) {
  if (n.is_god) {
    return 'Classe-deus: Ce e WMC max fora do padrao estatistico do projeto ao mesmo tempo (Ce=' + n.ce + ', WMC max=' + n.max_method_wmc + ', soma da classe=' + n.wmc + ' em ' + n.method_count + ' metodo(s)). As duas condicoes juntas sugerem responsabilidade demais numa classe so.';
  }
  if (n.is_concrete_hotspot) {
    return 'Implementacao concreta sobre-dependida: Ca=' + n.ca + ' esta fora do padrao estatistico entre as classes concretas do projeto, sem interface no meio. Qualquer mudanca de implementacao atinge todo mundo que depende dela diretamente.';
  }
  if (n.is_speculative) {
    return 'Abstracao especulativa: interface/classe abstrata com no maximo 1 implementacao real e nenhum consumidor alem dela mesma (Ca=' + n.ca + '). Nao esta gerando ganho real de desacoplamento.';
  }
  if (n.is_ce_outlier || n.is_wmc_outlier) {
    const which = n.is_ce_outlier && n.is_wmc_outlier ? 'Ce e WMC max' : (n.is_ce_outlier ? 'Ce' : 'WMC max');
    return which + ' fora do padrao estatistico do projeto, mas nao os dois ao mesmo tempo — nao vira classe-deus sozinho. Padrao normal de orquestrador/gateway/handler (Ce alto, complexidade baixa) ou de logica isolada complexa (WMC alto, poucas dependencias).';
  }
  return 'Sem sinais de classe-deus, hotspot de Ca ou abstracao especulativa nos limiares calculados pra este projeto.';
}

function buildTooltip(n) {
  hideTooltip();
  tooltipEl = document.createElement('div');
  tooltipEl.className = 'tooltip';

  const depOn = (n.depends_on || []).slice(0, 8).join(', ') || '(nenhuma)';
  const depBy = (n.depended_by || []).slice(0, 8).join(', ') || '(nenhuma)';

  if (mode === 'classes') {
    const flags = [];
    if (n.is_god) flags.push('classe-deus');
    if (n.is_speculative) flags.push('abstracao especulativa');
    const flagsHtml = flags.length ? '<p class="explain"><b>⚠️ ' + flags.join(' · ') + '</b></p>' : '';

    tooltipEl.innerHTML =
      '<div class="path">' + n.package + ' · ' + n.kind + (n.is_abstract ? ' (abstrata)' : '') + '</div>' +
      '<p class="title">' + n.label + '</p>' +
      '<div class="metrics">' +
        '<span>Ca (dependem dela)</span><span>' + n.ca + '</span>' +
        '<span>Ce (depende de)</span><span>' + n.ce + '</span>' +
        '<span>Metodos</span><span>' + n.method_count + '</span>' +
        '<span>WMC (soma)</span><span>' + n.wmc + '</span>' +
        '<span>WMC (max/metodo)</span><span>' + n.max_method_wmc + '</span>' +
      '</div>' +
      flagsHtml +
      '<p class="explain">' + explainClass(n) + '</p>' +
      '<p class="depline"><b>depende de:</b> ' + depOn + '</p>' +
      '<p class="depline"><b>dependem dela:</b> ' + depBy + '</p>';
  } else {
    tooltipEl.innerHTML =
      '<div class="path">' + n.class_count + ' classes neste pacote</div>' +
      '<p class="title">' + n.label + '</p>' +
      '<div class="metrics">' +
        '<span>A (abstracao)</span><span>' + fmtNum(n.a) + '</span>' +
        '<span>I (instabilidade)</span><span>' + fmtNum(n.i) + '</span>' +
        '<span>D (distancia)</span><span>' + fmtNum(n.d) + '</span>' +
        '<span>Ce (depende de)</span><span>' + n.ce + '</span>' +
        '<span>Ca (dependem dela)</span><span>' + n.ca + '</span>' +
      '</div>' +
      '<p class="explain">' + explainPackage(n) + '</p>' +
      '<p class="depline"><b>depende de:</b> ' + depOn + '</p>' +
      '<p class="depline"><b>dependem dela:</b> ' + depBy + '</p>';
  }
  document.body.appendChild(tooltipEl);
}

// posiciona o tooltip SEMPRE sobre a coluna lateral estatica (onde fica
// "Como ler" / "Dependencias circulares"), independente de onde o ponto
// clicado esteja no grafico — assim ele nunca cobre o grafico nem os
// relacionamentos (linhas de dependencia) sendo exibidos.
function positionTooltip() {
  if (!tooltipEl) return;
  const sidebar = document.getElementById('sidebarColumn');
  const rect = sidebar.getBoundingClientRect();
  const margin = 12;

  tooltipEl.style.left = Math.max(margin, rect.left) + 'px';
  tooltipEl.style.top = Math.max(margin, rect.top) + 'px';
  tooltipEl.style.width = rect.width + 'px';

  const availableHeight = window.innerHeight - Math.max(margin, rect.top) - margin;
  tooltipEl.style.maxHeight = Math.max(160, availableHeight) + 'px';
}

function showTooltipFor(id) {
  const n = currentDataset().nodes.find(x => x.id === id);
  if (!n) { hideTooltip(); return; }
  buildTooltip(n);
  positionTooltip();
}

function renderCycles() {
  const box = document.getElementById('cyclesBox');
  clear(box);
  const cycles = mode === 'classes' ? DATA.cycles_classes : DATA.cycles_packages;
  if (!cycles || cycles.length === 0) {
    const p = document.createElement('p');
    p.className = 'empty-note';
    p.textContent = 'Nenhum ciclo detectado neste nivel.';
    box.appendChild(p);
    return;
  }
  cycles.forEach((cyc, idx) => {
    const div = document.createElement('div');
    div.className = 'cycle-item';
    const parts = cyc.map(n => '<span class="node-name">' + n + '</span>');
    div.innerHTML = (idx + 1) + ') ' + parts.join(' <span class="arrow">&rarr;</span> ');
    box.appendChild(div);
  });
}

function renderSpeculative() {
  const panel = document.getElementById('speculativePanel');
  if (mode !== 'classes') { panel.style.display = 'none'; return; }
  panel.style.display = '';

  const box = document.getElementById('speculativeBox');
  clear(box);
  const items = DATA.classes.nodes.filter(n => n.is_speculative);
  if (items.length === 0) {
    const p = document.createElement('p');
    p.className = 'empty-note';
    p.textContent = 'Nenhuma abstracao especulativa encontrada.';
    box.appendChild(p);
    return;
  }
  items.forEach(n => {
    const div = document.createElement('div');
    div.className = 'cycle-item';
    div.innerHTML = '<span class="node-name">' + n.package + '.' + n.label + '</span>' +
      ' — tipo ' + n.kind + ', usada por: ' +
      (n.depended_by.join(', ') || '(nenhuma)');
    box.appendChild(div);
  });
}

function renderHotspots() {
  const panel = document.getElementById('hotspotPanel');
  if (mode !== 'classes') { panel.style.display = 'none'; return; }
  panel.style.display = '';

  const box = document.getElementById('hotspotBox');
  clear(box);
  const items = DATA.classes.nodes.filter(n => n.is_concrete_hotspot);
  if (items.length === 0) {
    const p = document.createElement('p');
    p.className = 'empty-note';
    p.textContent = 'Nenhuma implementacao concreta sobre-dependida encontrada.';
    box.appendChild(p);
    return;
  }
  items.forEach(n => {
    const div = document.createElement('div');
    div.className = 'cycle-item';
    const depBy = n.depended_by.slice(0, 8).join(', ') + (n.depended_by.length > 8 ? '...' : '');
    div.innerHTML = '<span class="node-name">' + n.package + '.' + n.label + '</span>' +
      ' — Ca=' + n.ca + ', usada por: ' + (depBy || '(nenhuma)');
    box.appendChild(div);
  });
}

function precomputeIdLinks(nodes) {
  const byLabel = {};
  nodes.forEach(n => byLabel[n.label] = n.id);
  nodes.forEach(n => {
    n.depends_on_ids = (n.depends_on || []).map(l => byLabel[l]).filter(Boolean);
    n.depended_by_ids = (n.depended_by || []).map(l => byLabel[l]).filter(Boolean);
  });
}
precomputeIdLinks(DATA.classes.nodes);
precomputeIdLinks(DATA.packages.nodes);

document.getElementById('btnByClass').addEventListener('click', () => {
  mode = 'classes'; pinned = null; hovered = null; hideTooltip();
  document.getElementById('btnByClass').classList.add('active');
  document.getElementById('btnByPackage').classList.remove('active');
  document.getElementById('legendClasses').style.display = '';
  document.getElementById('legendPackages').style.display = 'none';
  document.getElementById('countNote').textContent = DATA.classes.nodes.length + ' classes — passe o mouse / clique.';
  renderChart(); renderCycles(); renderSpeculative(); renderHotspots();
});
document.getElementById('btnByPackage').addEventListener('click', () => {
  mode = 'packages'; pinned = null; hovered = null; hideTooltip();
  document.getElementById('btnByPackage').classList.add('active');
  document.getElementById('btnByClass').classList.remove('active');
  document.getElementById('legendPackages').style.display = '';
  document.getElementById('legendClasses').style.display = 'none';
  document.getElementById('countNote').textContent = DATA.packages.nodes.length + ' pacotes — passe o mouse / clique.';
  renderChart(); renderCycles(); renderSpeculative(); renderHotspots();
});

// clicar fora de um ponto fixado (e fora do proprio tooltip) fecha o pop-up
document.addEventListener('click', (e) => {
  if (pinned === null) return;
  if (e.target.closest && (e.target.closest('.node') || e.target.closest('.tooltip'))) return;
  pinned = null;
  hovered = null;
  hideTooltip();
  updateOverlay();
});

// Esc tambem fecha
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && pinned !== null) {
    pinned = null;
    hovered = null;
    hideTooltip();
    updateOverlay();
  }
});

renderStats();
document.getElementById('countNote').textContent = DATA.classes.nodes.length + ' classes — passe o mouse / clique.';
renderChart();
renderCycles();
renderSpeculative();
renderHotspots();
</script>
</body>
</html>
"""


def _escape_for_script_tag(payload: str) -> str:
    """Impede que `</script>` dentro de uma string JSON feche a tag cedo.

    `\\/` é um escape válido de JSON para `/` (RFC 8259) — o parser devolve
    o mesmo valor de string, então isso não muda o dado, só a serialização.
    """
    return payload.replace("</", "<\\/")


def render_html(surface: dict, analysis: dict, now: str) -> str:
    """Gera o HTML interativo do motor Java. Devolve a string; não grava arquivo."""
    from .export import _repo_name

    project = _repo_name(surface)
    data = _build_data(analysis, now, project)
    payload = _escape_for_script_tag(json.dumps(data, ensure_ascii=False, sort_keys=True))

    out = _HTML_TEMPLATE.replace("__PROJECT_NAME__", html.escape(project))
    out = out.replace("__GENERATED_AT__", html.escape(now))
    out = out.replace("__DATA_JSON__", payload)
    return out
