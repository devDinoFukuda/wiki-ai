"""CLI do índice. O agente chama isto via bash; nunca escreve SQL.

  reindex   varre raw/ e wiki/, chunka, indexa o que mudou, poda o que sumiu
  search    híbrido (BM25 + vetorial) com fusão RRF e filtro de proveniência
  get       recupera documento/trecho por docid ou path
  audit     regras DETERMINÍSTICAS (L1, L2, L5 em SQL; L4 por travessia de
            frontmatter) — sem LLM, sem vetor
  status    saúde do índice e frescor
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from . import embed as embed_mod
from . import store
from .chunker import chunk
from .frontmatter import provenance_gaps, split
from .tokens import exact as tokens_exact

COLLECTIONS = ("raw", "wiki")
IGNORED_WIKI_FILES = {"_lint-report.md"}


def _walk(root: str):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for fn in sorted(filenames):
            if fn.endswith(".md") and not fn.startswith("."):
                yield os.path.join(dirpath, fn)


# ---------------- reindex ----------------


def cmd_reindex(a) -> int:
    conn = store.connect(a.db)
    try:
        return _cmd_reindex_body(a, conn)
    finally:
        conn.close()


def _cmd_reindex_body(a, conn) -> int:
    emb = embed_mod.get_embedder()
    changed = seen = 0
    seen_paths: set[str] = set()

    for coll in COLLECTIONS:
        root = os.path.join(a.store, coll)
        if not os.path.isdir(root):
            continue
        for path in _walk(root):
            if coll == "wiki" and os.path.basename(path) in IGNORED_WIKI_FILES:
                continue
            # utf-8-sig descasca BOM (comum no Windows: PowerShell 5.1, Notepad).
            # Sem isto, o BOM vai pro corpo, vira chunk, polui a busca e quebra
            # a saída da CLI (charmap codec can't encode \ufeff).
            with open(path, encoding="utf-8-sig", errors="replace") as f:
                text = f.read()
            meta, body = split(text)
            gaps = provenance_gaps(meta) if coll == "raw" else []
            # Documento sem chunks (corpo vazio, ou só frontmatter) ainda é
            # registrado: upsert() aceita `chunks=[]` (nenhuma linha em
            # `chunks`, doc normal em `documents`). Pular o upsert aqui fazia
            # o documento sumir do índice E do audit L2, e prune() o removia
            # a cada reindex seguinte por nunca entrar em seen_paths. F-13.
            chunks = chunk(body, coll)
            _, did_change = store.upsert(conn, path, coll, meta, gaps, chunks)
            seen += 1
            changed += 1 if did_change else 0
            seen_paths.add(path)

    pruned = store.prune(conn, seen_paths)
    conn.commit()

    embedded = 0
    skipped_embed = False
    space_switch = None
    if not a.lex_only and emb.available:
        embedded, space_switch = _reindex_embeddings(conn, emb)
    elif not a.lex_only:
        skipped_embed = True

    out = dict(
        documents=seen,
        changed=changed,
        pruned=pruned,
        embedded=embedded,
        embedder=emb.name,
        exact_tokens=tokens_exact(),
    )
    if space_switch:
        out["embedding_space_switch"] = space_switch
    if skipped_embed:
        out["warning"] = (
            "embeddings não configurados: índice em modo léxico. "
            "vec/hyde exigem embeddings e falham com erro (exit 2) nesse modo; "
            "apenas lex funciona. Defina AZURE_OPENAI_* ou use --lex-only."
        )
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def _reindex_embeddings(conn, emb) -> tuple[int, dict | None]:
    """Passo de embedding do reindex, com detecção de troca de espaço (F06).

    Compara a IDENTIDADE do embedder atual (provider/model/deployment/config
    — SEM dimensão, que só se sabe depois de embedar) contra o space
    atualmente ativo:

    - identidade IGUAL (ou nenhum space ativo ainda): incremental — só os
      chunks sem embedding (`chunks_without_embedding`) entram no space ativo
      (ou num novo, se este é o primeiro reindex com embeddings do índice).
    - identidade DIFERENTE (troca de modelo/deployment/config): a coleção
      INTEIRA é reembedada num space novo, que nasce inativo; só depois que
      TODOS os chunks foram escritos nele é que `activate_space` roda — e só
      então o commit torna a troca visível. Enquanto isso, nada foi commitado:
      o space antigo (ainda ativo) continua completo e é o que qualquer busca
      concorrente enxerga. Se o processo morrer no meio, o rollback implícito
      (nunca houve commit) devolve o índice ao estado anterior — nunca um
      space novo ativo pela metade.

    Devolve (quantidade embedada, dict de troca de espaço ou None).
    """
    sig = emb.signature()
    config_json = json.dumps(sig.get("config") or {}, sort_keys=True, ensure_ascii=False)
    active = store.get_active_space(conn)
    same_identity = store.space_signature_matches(
        active, sig["provider"], sig["model"], sig["deployment"], config_json
    )

    if same_identity:
        pend = store.chunks_without_embedding(conn)
        embedded = 0
        for i in range(0, len(pend), embed_mod.BATCH):
            batch = pend[i : i + embed_mod.BATCH]
            vecs = emb.embed([r["text"] for r in batch])
            for r, v in zip(batch, vecs):
                store.set_embedding(conn, r["id"], v, active["space_id"])
            embedded += len(batch)
        conn.commit()
        return embedded, None

    # Identidade mudou (ou é o primeiro reindex com embeddings): reindex
    # completo da coleção num space novo, ativado só ao final.
    all_chunks = store.chunks_all(conn)
    new_space_id = None
    embedded = 0
    for i in range(0, len(all_chunks), embed_mod.BATCH):
        batch = all_chunks[i : i + embed_mod.BATCH]
        vecs = emb.embed([r["text"] for r in batch])
        if new_space_id is None:
            dimension = len(vecs[0])
            new_space_id = store.get_or_create_space(
                conn, sig["provider"], sig["model"], sig["deployment"], dimension, sig["config"]
            )
        for r, v in zip(batch, vecs):
            store.set_embedding(conn, r["id"], v, new_space_id)
        embedded += len(batch)

    if new_space_id is None:
        # Coleção vazia (nenhum chunk pra embedar): nada a ativar ainda.
        conn.rollback()
        return 0, None

    store.activate_space(conn, new_space_id)
    conn.commit()
    return embedded, {"from": active["space_id"] if active else None, "to": new_space_id}


# ---------------- search ----------------


def parse_query_doc(text: str) -> tuple[str, list[tuple[str, str]]]:
    """Parseia o documento de query. A ORDEM importa: a primeira sub-query
    recebe peso 2x na fusão. Formato:

        intent: o que você está procurando e por quê
        lex: "termo exato" -excluido
        vec: descrição em linguagem natural
        hyde: o parágrafo que você espera encontrar
    """
    intent = ""
    subs: list[tuple[str, str]] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        kind, _, val = line.partition(":")
        kind, val = kind.strip().lower(), val.strip()
        if kind == "intent":
            intent = val
        elif kind in ("lex", "vec", "hyde"):
            if val:
                subs.append((kind, val))
        elif kind == "expand":
            raise ValueError(
                "expand: não é suportado. Você conhece o objetivo e os conceitos "
                "vizinhos-mas-errados; o modelo não. Escreva lex/vec/hyde."
            )
        else:
            raise ValueError(f"linha inválida no query document: {raw!r}")
    if not subs:
        raise ValueError("nenhuma sub-query. Use lex:, vec: ou hyde:.")
    if len(subs) > 10:
        raise ValueError("máximo de 10 sub-queries")
    return intent, subs


def parse_filters(items: list[str]) -> dict:
    f: dict = {}
    for it in items or []:
        if "=" not in it:
            raise ValueError(f"filtro inválido: {it!r} (use chave=valor)")
        k, _, v = it.partition("=")
        k, v = k.strip(), v.strip()
        if "," in v:
            f[k] = [x.strip() for x in v.split(",")]
        elif v.lower() == "null":
            f[k] = None
        else:
            f[k] = v
    return f


def cmd_search(a) -> int:
    qtext = a.query if a.query else sys.stdin.read()
    intent, subs = parse_query_doc(qtext)
    filters = parse_filters(a.filter)
    if a.collection:
        filters["collection"] = a.collection

    conn = store.connect(a.db)
    try:
        return _cmd_search_body(a, conn, intent, subs, filters)
    finally:
        conn.close()


def _cmd_search_body(a, conn, intent, subs, filters) -> int:
    emb = embed_mod.get_embedder()
    pool = max(a.n * 5, 30)

    lists, weights, used = [], [], []
    need_vec = [s for s in subs if s[0] in ("vec", "hyde")]
    vecs = {}
    space_id = None
    if need_vec:
        if not emb.available:
            print(
                json.dumps(
                    {
                        "error": "vec/hyde exigem embeddings; índice em modo léxico",
                        "hint": "use apenas lex:, ou configure AZURE_OPENAI_*",
                    },
                    ensure_ascii=False,
                ),
                file=sys.stderr,
            )
            return 2
        active = store.get_active_space(conn)
        if not active:
            print(
                json.dumps(
                    {
                        "error": "vec/hyde exigem um espaço de embeddings ativo; índice nunca reindexou com embeddings",
                        "hint": "rode reindex com AZURE_OPENAI_* configurado",
                    },
                    ensure_ascii=False,
                ),
                file=sys.stderr,
            )
            return 2
        embedded = emb.embed([v for _, v in need_vec])
        # F06: NUNCA compara vetor de espaço != do ativo, mesmo com dimensão
        # igual por coincidência — a identidade do embedder (não só a
        # dimensão) tem que bater com o space ativo do índice.
        sig = emb.signature()
        config_json = json.dumps(sig.get("config") or {}, sort_keys=True, ensure_ascii=False)
        dim = len(embedded[0]) if embedded else None
        matches_active = (
            store.space_signature_matches(
                active, sig["provider"], sig["model"], sig["deployment"], config_json
            )
            and active["dimension"] == dim
        )
        if not matches_active:
            print(
                json.dumps(
                    {
                        "error": "embedder atual não corresponde ao espaço ativo do índice",
                        "impacto": "comparar vetores de espaços diferentes não mede similaridade nenhuma",
                        "correcao": "rode reindex com este embedder para criar/ativar o espaço correspondente",
                    },
                    ensure_ascii=False,
                ),
                file=sys.stderr,
            )
            return 2
        space_id = active["space_id"]
        vecs = {id(s): e for s, e in zip(need_vec, embedded)}

    for i, s in enumerate(subs):
        kind, val = s
        w = 2.0 if i == 0 else 1.0  # primeira = melhor palpite
        if kind == "lex":
            lists.append(store.search_lex(conn, val, filters, pool))
        else:
            lists.append(store.search_vec(conn, vecs[id(s)], space_id, filters, pool))
        weights.append(w)
        used.append({"type": kind, "query": val, "weight": w})

    fused = store.rrf(lists, weights)[: a.n]
    hits = store.hydrate(conn, fused)

    if a.format == "json":
        print(
            json.dumps(
                {
                    "intent": intent,
                    "searches": used,
                    "filters": filters,
                    "results": [
                        dict(
                            docid="#" + h.docid,
                            path=h.path,
                            collection=h.collection,
                            heading=h.heading,
                            source_type=h.source_type,
                            confidence=h.confidence,
                            score=round(h.score, 5),
                            text=h.text if a.full else h.text[:400],
                        )
                        for h in hits
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        if not hits:
            print("(sem resultados)")
        for h in hits:
            flag = " [!unverified-source]" if h.source_type == "agent-output" else ""
            print(f"#{h.docid}  {h.score:.4f}  {h.collection}  {h.path}{flag}")
            if h.heading:
                print(f"    {h.heading}")
            print("    " + (h.text if a.full else h.text[:200]).replace("\n", "\n    "))
            print()
    return 0


# ---------------- get ----------------


def cmd_get(a) -> int:
    conn = store.connect(a.db)
    try:
        return _cmd_get_body(a, conn)
    finally:
        conn.close()


def _cmd_get_body(a, conn) -> int:
    ref = a.ref.lstrip("#")
    row = conn.execute(
        "SELECT path FROM documents WHERE docid=? OR path=?", (ref, a.ref)
    ).fetchone()
    if not row:
        cand = conn.execute(
            "SELECT docid, path FROM documents WHERE path LIKE ? LIMIT 5",
            (f"%{ref}%",),
        ).fetchall()
        print(
            json.dumps(
                {
                    "error": f"não encontrado: {a.ref}",
                    "sugestoes": [{"docid": "#" + c["docid"], "path": c["path"]} for c in cand],
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 1
    with open(row["path"], encoding="utf-8-sig", errors="replace") as f:
        lines = f.read().splitlines()
    start = max(0, a.line_from - 1) if a.line_from else 0
    end = start + a.count if a.count else len(lines)
    width = len(str(min(end, len(lines))))
    print(f"# {row['path']}  (linhas {start+1}-{min(end, len(lines))} de {len(lines)})")
    for i, ln in enumerate(lines[start:end], start=start + 1):
        print(f"{str(i).rjust(width)}\t{ln}")
    return 0


# ---------------- audit ----------------

AUDIT_SQL = {
    "L2_proveniencia_ausente": """
        SELECT docid, path, gaps AS detalhe FROM documents
        WHERE collection='raw' AND gaps IS NOT NULL
    """,
    "L1_canonico_so_de_agente": """
        SELECT w.docid, w.path, 'todas as fontes sao agent-output' AS detalhe
        FROM documents w
        WHERE w.collection='wiki'
          AND EXISTS (SELECT 1 FROM doc_sources ds WHERE ds.doc_id = w.id)
          AND NOT EXISTS (
                SELECT 1 FROM doc_sources ds
                JOIN documents s ON s.source_id = ds.source_id
                WHERE ds.doc_id = w.id
                  AND (s.source_type IS NULL OR s.source_type <> 'agent-output')
          )
    """,
    "L5_supersedida_ainda_citada": """
        SELECT w.docid, w.path,
               'cita ' || old.source_id || ' substituida por ' ||
               GROUP_CONCAT(DISTINCT new.source_id) AS detalhe
        FROM documents new
        JOIN documents old ON old.source_id = new.supersedes
        JOIN doc_sources ds ON ds.source_id = old.source_id
        JOIN documents w ON w.id = ds.doc_id
        WHERE new.supersedes IS NOT NULL
        GROUP BY w.docid, w.path, old.source_id
    """,
    "wiki_sem_fontes_declaradas": """
        SELECT docid, path, 'sem sources: no frontmatter' AS detalhe
        FROM documents w
        WHERE w.collection='wiki'
          AND NOT EXISTS (SELECT 1 FROM doc_sources ds WHERE ds.doc_id = w.id)
    """,
    "fonte_orfa": """
        SELECT d.docid, d.path, 'promovida mas nao citada por nenhuma pagina' AS detalhe
        FROM documents d
        WHERE d.collection='raw' AND d.promoted=1 AND d.source_id IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM doc_sources ds WHERE ds.source_id = d.source_id)
    """,
}


# ---------------- L4: realimentação (anti-contaminação) ----------------
#
# F-26. O FLUXO 4 fecha um loop perigoso: a resposta sintetizada a partir da
# wiki pode voltar como fonte (`agent-output`) e, daí em diante, o corpus cita
# a si mesmo — conhecimento sem nenhum lastro humano ou de código. O vínculo é
# declarado no frontmatter da fonte (`derived_from: id1,id2`, gravado por
# `wk ingest --derived-from`).
#
# Por que NÃO é SQL como L1/L2/L5: `derived_from` não é coluna do índice
# (o schema de `store.py` não muda neste lote), então a regra é uma travessia
# DETERMINÍSTICA de `raw/` + `wiki/` lendo o frontmatter direto do disco —
# mesmo padrão das regras W de `wk lint`. Continua sem LLM e sem vetor.

L4_RULE = "L4_realimentacao"
AUDIT_RULES = sorted(AUDIT_SQL) + [L4_RULE]


def derived_from_ids(meta: dict) -> list[str]:
    """Ids declarados em `derived_from`, normalizados.

    Aceita a forma canônica (string separada por vírgula, que é como
    `wk ingest --derived-from` grava) e também lista YAML — o frontmatter pode
    ser escrito à mão. Preserva a ordem, descarta vazios e duplicados.
    """
    raw = meta.get("derived_from")
    if raw is None:
        return []
    if isinstance(raw, (list, tuple, set)):
        bruto = [str(x) for x in raw]
    else:
        bruto = str(raw).strip().strip("[]").split(",")
    out: list[str] = []
    for item in bruto:
        v = item.strip().strip("\"'").strip()
        if v and v not in out:
            out.append(v)
    return out


def _read_text(path: str) -> str:
    # utf-8-sig pelo mesmo motivo do reindex: BOM do Windows não pode virar corpo.
    with open(path, encoding="utf-8-sig", errors="replace") as f:
        return f.read()


def derivation_graph(store_root: str) -> tuple[dict, set]:
    """(`fontes de raw/ por id`, `ids de páginas de wiki/`).

    Cada fonte vira `{path, source_type, derived_from}`. O primeiro arquivo a
    reivindicar um id vence (ordem estável de `_walk`) — id duplicado em raw/ é
    achado de outra regra (F-16b, no promote), não desta.
    """
    fontes: dict[str, dict] = {}
    raw_root = os.path.join(store_root, "raw")
    if os.path.isdir(raw_root):
        for path in _walk(raw_root):
            meta, _body = split(_read_text(path))
            doc_id = meta.get("id")
            if doc_id is None or not str(doc_id).strip():
                continue
            fontes.setdefault(
                str(doc_id).strip(),
                {
                    "path": path,
                    "source_type": meta.get("source_type"),
                    "derived_from": derived_from_ids(meta),
                },
            )
    paginas: set[str] = set()
    wiki_root = os.path.join(store_root, "wiki")
    if os.path.isdir(wiki_root):
        for path in _walk(wiki_root):
            if os.path.basename(path) in IGNORED_WIKI_FILES:
                continue
            meta, _body = split(_read_text(path))
            page_id = meta.get("id")
            if page_id is not None and str(page_id).strip():
                paginas.add(str(page_id).strip())
            # o stem também identifica a página (é como os wikilinks a citam)
            paginas.add(os.path.splitext(os.path.basename(path))[0])
    return fontes, paginas


def derivation_lastro(
    refs: list[str], fontes: dict, paginas: set, origem: str | None = None
) -> dict:
    """Percorre o grafo de derivação a partir de `refs` e classifica o que
    alcança. `lastro` = fontes de raw/ que NÃO são `agent-output` (transcrição,
    doc, repositório, web-clip): é o que ancora o conhecimento fora do próprio
    agente. Página de wiki NUNCA conta como lastro — ela é saída compilada do
    corpus, e tratá-la como origem é exatamente o loop que a regra fecha.

    A travessia é transitiva: uma fonte de agente que deriva de outra fonte de
    agente ancorada numa transcrição humana TEM lastro (indireto, mas real).
    Ids não resolvidos saem em `desconhecidos` — o chamador não deve acusar
    realimentação sem conseguir resolver a cadeia inteira.
    """
    fila = list(refs)
    visto = {origem} if origem else set()
    resultado = {"lastro": [], "agent_output": [], "wiki": [], "desconhecidos": []}
    while fila:
        ref = fila.pop(0)
        if ref in visto:
            continue
        visto.add(ref)
        info = fontes.get(ref)
        if info is None:
            resultado["wiki" if ref in paginas else "desconhecidos"].append(ref)
            continue
        if info["source_type"] != "agent-output":
            resultado["lastro"].append(ref)
            continue
        resultado["agent_output"].append(ref)
        fila.extend(info["derived_from"])
    return resultado


def is_realimentacao(lastro: dict) -> bool:
    """Verdadeiro quando a cadeia foi resolvida por inteiro e não alcançou
    nenhuma fonte humana/de código — só saída de agente e/ou página da wiki."""
    return not lastro["lastro"] and not lastro["desconhecidos"] and bool(
        lastro["agent_output"] or lastro["wiki"]
    )


def realimentacao_findings(store_root: str) -> list[dict]:
    """Achados de L4, no mesmo formato dos itens das regras SQL."""
    fontes, paginas = derivation_graph(store_root)
    itens: list[dict] = []
    for doc_id, info in sorted(fontes.items(), key=lambda kv: kv[1]["path"]):
        if info["source_type"] != "agent-output" or not info["derived_from"]:
            continue
        lastro = derivation_lastro(info["derived_from"], fontes, paginas, origem=doc_id)
        if not is_realimentacao(lastro):
            continue
        origens = []
        if lastro["agent_output"]:
            origens.append("agent-output: " + ", ".join(lastro["agent_output"]))
        if lastro["wiki"]:
            origens.append("wiki: " + ", ".join(lastro["wiki"]))
        itens.append(
            {
                "docid": "#" + store.make_docid(info["path"]),
                "path": info["path"],
                "detalhe": (
                    "derivada so de saida de agente/pagina da wiki, sem lastro "
                    "humano ou de codigo (" + "; ".join(origens) + ")"
                ),
            }
        )
    return itens


def cmd_audit(a) -> int:
    """Regras determinísticas. Não gastam LLM nem vetor.

    L1/L2/L5 saem de SQL sobre o índice; L4 (realimentação) sai da travessia de
    `derived_from` no frontmatter de raw/ — determinística também, só não
    consultável em SQL porque o campo não é coluna do índice.

    L3 (contradição) NÃO está aqui de propósito: exige julgamento semântico.
    Para ela, use `search` para gerar candidatos e leve só esses ao LLM.
    """
    conn = store.connect(a.db)
    try:
        return _cmd_audit_body(a, conn)
    finally:
        conn.close()


def _cmd_audit_body(a, conn) -> int:
    report: dict[str, list] = {}
    for rule, sql in AUDIT_SQL.items():
        if a.rule and a.rule != rule:
            continue
        rows = conn.execute(sql).fetchall()
        report[rule] = [
            {"docid": "#" + r["docid"], "path": r["path"], "detalhe": r["detalhe"]}
            for r in rows
        ]
    if not a.rule or a.rule == L4_RULE:
        report[L4_RULE] = realimentacao_findings(a.store)
    total = sum(len(v) for v in report.values())
    print(
        json.dumps(
            {"achados": total, "regras": {k: {"n": len(v), "itens": v} for k, v in report.items()}},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 1 if total else 0


# ---------------- status ----------------


def cmd_status(a) -> int:
    conn = store.connect(a.db)
    try:
        return _cmd_status_body(a, conn)
    finally:
        conn.close()


def _cmd_status_body(a, conn) -> int:
    q = lambda s, *p: conn.execute(s, p).fetchone()[0]
    stale = []
    for r in conn.execute("SELECT path, mtime FROM documents").fetchall():
        if not os.path.exists(r["path"]):
            stale.append({"path": r["path"], "motivo": "sumiu do disco"})
        elif r["mtime"] and os.path.getmtime(r["path"]) > r["mtime"] + 1e-6:
            stale.append({"path": r["path"], "motivo": "alterado apos indexacao"})
    by_type = {
        r["source_type"] or "(sem)": r["n"]
        for r in conn.execute(
            "SELECT source_type, COUNT(*) n FROM documents GROUP BY source_type"
        )
    }
    emb = embed_mod.get_embedder()
    active = store.get_active_space(conn)
    chunks_por_espaco = {
        (str(r["embedding_space_id"]) if r["embedding_space_id"] is not None else "legado"): r["n"]
        for r in conn.execute(
            "SELECT embedding_space_id, COUNT(*) n FROM chunks "
            "WHERE embedding IS NOT NULL GROUP BY embedding_space_id"
        )
    }
    out = dict(
        db=a.db,
        documentos=q("SELECT COUNT(*) FROM documents"),
        por_colecao={
            r["collection"]: r["n"]
            for r in conn.execute(
                "SELECT collection, COUNT(*) n FROM documents GROUP BY collection"
            )
        },
        por_source_type=by_type,
        chunks=q("SELECT COUNT(*) FROM chunks"),
        chunks_sem_embedding=q("SELECT COUNT(*) FROM chunks WHERE embedding IS NULL"),
        embedder=emb.name,
        contagem_de_tokens="tiktoken" if tokens_exact() else "aproximada (chars/3.6)",
        indice_sujo=stale,
        # F06/W7: espaço de embedding ativo + distribuição de chunks por
        # espaço/pendentes/legados — sem isto, uma troca de modelo/dimensão
        # incompleta ou uma mistura de gerações fica invisível no status.
        embedding_space_ativo=(
            {
                "space_id": active["space_id"],
                "provider": active["provider"],
                "model": active["model"],
                "deployment": active["deployment"],
                "dimension": active["dimension"],
            }
            if active
            else None
        ),
        chunks_por_espaco=chunks_por_espaco,
        chunks_pendentes=q("SELECT COUNT(*) FROM chunks WHERE embedding IS NULL"),
        chunks_legados=q(
            "SELECT COUNT(*) FROM chunks WHERE embedding IS NOT NULL AND embedding_space_id IS NULL"
        ),
    )
    print(json.dumps(out, ensure_ascii=False, indent=2))
    # frescor é requisito de correção: compile/lint não devem rodar sujos
    return 1 if stale else 0


# ---------------- main ----------------


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="sbindex", description="Índice do wiki-ai")
    p.add_argument("--store", default="./store", help="raiz com raw/ e wiki/")
    p.add_argument("--db", default=None, help="caminho do índice (default: <store>/index.db)")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("reindex", help="indexa o que mudou; poda o que sumiu")
    r.add_argument("--lex-only", action="store_true", help="não gerar embeddings")
    r.set_defaults(fn=cmd_reindex)

    s = sub.add_parser("search", help="busca híbrida com fusão RRF")
    s.add_argument("query", nargs="?", help="query document; omita para ler do stdin")
    s.add_argument("-c", "--collection", choices=COLLECTIONS)
    s.add_argument("--filter", action="append", default=[], help="chave=valor (repetível)")
    s.add_argument("-n", type=int, default=10)
    s.add_argument("--full", action="store_true")
    s.add_argument("--format", choices=("text", "json"), default="text")
    s.set_defaults(fn=cmd_search)

    g = sub.add_parser("get", help="recupera documento ou trecho")
    g.add_argument("ref", help="#docid ou caminho")
    g.add_argument("--from", dest="line_from", type=int, default=0)
    g.add_argument("--count", type=int, default=0)
    g.set_defaults(fn=cmd_get)

    au = sub.add_parser("audit", help="regras determinísticas (L1, L2, L4, L5)")
    au.add_argument("--rule", choices=AUDIT_RULES)
    au.set_defaults(fn=cmd_audit)

    st = sub.add_parser("status", help="saúde e frescor do índice")
    st.set_defaults(fn=cmd_status)

    a = p.parse_args(argv)
    if a.db is None:
        a.db = os.path.join(a.store, "index.db")
    try:
        return a.fn(a)
    except (ValueError, RuntimeError) as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
