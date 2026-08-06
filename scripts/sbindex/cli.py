"""CLI do índice. O agente chama isto via bash; nunca escreve SQL.

  reindex   varre raw/ e wiki/, chunka, indexa o que mudou, poda o que sumiu
  search    híbrido (BM25 + vetorial) com fusão RRF e filtro de proveniência
  get       recupera documento/trecho por docid ou path
  audit     regras DETERMINÍSTICAS (L1, L2, L5) em SQL — sem LLM, sem vetor
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
            text = open(path, encoding="utf-8-sig", errors="replace").read()
            meta, body = split(text)
            gaps = provenance_gaps(meta) if coll == "raw" else []
            chunks = chunk(body, coll)
            if not chunks:
                continue
            _, did_change = store.upsert(conn, path, coll, meta, gaps, chunks)
            seen += 1
            changed += 1 if did_change else 0
            seen_paths.add(path)

    pruned = store.prune(conn, seen_paths)
    conn.commit()

    embedded = 0
    skipped_embed = False
    if not a.lex_only and emb.available:
        pend = store.chunks_without_embedding(conn)
        for i in range(0, len(pend), embed_mod.BATCH):
            batch = pend[i : i + embed_mod.BATCH]
            vecs = emb.embed([r["text"] for r in batch])
            for r, v in zip(batch, vecs):
                store.set_embedding(conn, r["id"], v)
            embedded += len(batch)
        conn.commit()
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
    if skipped_embed:
        out["warning"] = (
            "embeddings não configurados: índice em modo LÉXICO. "
            "vec/hyde retornam vazio. Defina AZURE_OPENAI_* ou use --lex-only."
        )
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


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
    emb = embed_mod.get_embedder()
    pool = max(a.n * 5, 30)

    lists, weights, used = [], [], []
    need_vec = [s for s in subs if s[0] in ("vec", "hyde")]
    vecs = {}
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
        embedded = emb.embed([v for _, v in need_vec])
        vecs = {id(s): e for s, e in zip(need_vec, embedded)}

    for i, s in enumerate(subs):
        kind, val = s
        w = 2.0 if i == 0 else 1.0  # primeira = melhor palpite
        if kind == "lex":
            lists.append(store.search_lex(conn, val, filters, pool))
        else:
            lists.append(store.search_vec(conn, vecs[id(s)], filters, pool))
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
    lines = open(row["path"], encoding="utf-8-sig", errors="replace").read().splitlines()
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
               'cita ' || old.source_id || ' substituida por ' || new.source_id AS detalhe
        FROM documents new
        JOIN documents old ON old.source_id = new.supersedes
        JOIN doc_sources ds ON ds.source_id = old.source_id
        JOIN documents w ON w.id = ds.doc_id
        WHERE new.supersedes IS NOT NULL
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


def cmd_audit(a) -> int:
    """Regras determinísticas. Não gastam LLM nem vetor.

    L3 (contradição) e L4 (realimentação) NÃO estão aqui de propósito: exigem
    julgamento semântico. Para elas, use `search` para gerar candidatos e leve
    só esses ao LLM.
    """
    conn = store.connect(a.db)
    report: dict[str, list] = {}
    for rule, sql in AUDIT_SQL.items():
        if a.rule and a.rule != rule:
            continue
        rows = conn.execute(sql).fetchall()
        report[rule] = [
            {"docid": "#" + r["docid"], "path": r["path"], "detalhe": r["detalhe"]}
            for r in rows
        ]
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

    au = sub.add_parser("audit", help="regras determinísticas (L1, L2, L5)")
    au.add_argument("--rule", choices=sorted(AUDIT_SQL))
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
