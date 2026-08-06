"""Índice SQLite: BM25 (FTS5) + vetores + proveniência como COLUNAS.

O ganho estrutural sobre o QMD está aqui: lá, `source_type` é string casada
por BM25 (probabilístico, com falso positivo). Aqui é coluna:

    WHERE source_type = 'agent-output'   -> exato, determinístico

L1/L2/L5 viram SQL e não gastam vetor. L4 filtra proveniência ANTES de gastar
embedding. Isso o QMD nunca daria, porque não conhece o schema.

Não é servidor: é um arquivo. Abre, consulta, fecha.
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import struct
import time
from dataclasses import dataclass

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS documents (
  id            INTEGER PRIMARY KEY,
  path          TEXT UNIQUE NOT NULL,
  collection    TEXT NOT NULL,          -- 'raw' | 'wiki'
  docid         TEXT NOT NULL,          -- hash curto p/ referência (#abc123)
  content_hash  TEXT NOT NULL,
  mtime         REAL,
  -- proveniência: colunas, não texto
  source_id     TEXT,
  source_type   TEXT,
  origin        TEXT,
  confidence    TEXT,
  promoted      INTEGER,
  supersedes    TEXT,
  topic         TEXT,
  gaps          TEXT,                   -- violações L2 detectadas no indexer
  indexed_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_doc_st    ON documents(source_type);
CREATE INDEX IF NOT EXISTS idx_doc_coll  ON documents(collection);
CREATE INDEX IF NOT EXISTS idx_doc_topic ON documents(topic);
CREATE INDEX IF NOT EXISTS idx_doc_conf  ON documents(confidence);
CREATE INDEX IF NOT EXISTS idx_doc_docid ON documents(docid);

CREATE TABLE IF NOT EXISTS chunks (
  id          INTEGER PRIMARY KEY,
  doc_id      INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  ord         INTEGER NOT NULL,
  heading     TEXT,
  text        TEXT NOT NULL,
  token_count INTEGER,
  embedding   BLOB
);
CREATE INDEX IF NOT EXISTS idx_chunk_doc ON chunks(doc_id);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
  text, heading,
  content='chunks', content_rowid='id',
  tokenize='unicode61 remove_diacritics 2'   -- 'servico' acha 'serviço'
);

CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
  INSERT INTO chunks_fts(rowid, text, heading) VALUES (new.id, new.text, new.heading);
END;
CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
  INSERT INTO chunks_fts(chunks_fts, rowid, text, heading)
  VALUES ('delete', old.id, old.text, old.heading);
END;
CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON chunks BEGIN
  INSERT INTO chunks_fts(chunks_fts, rowid, text, heading)
  VALUES ('delete', old.id, old.text, old.heading);
  INSERT INTO chunks_fts(rowid, text, heading) VALUES (new.id, new.text, new.heading);
END;

-- Quais fontes alimentaram cada página da wiki. Sem isto, L1 (canônico só de
-- agente) e L5 (supersessão) não têm como ser verificados — seriam julgamento
-- de LLM sobre o corpus inteiro, que em escala não roda.
-- O compile passa a declarar `sources:` no frontmatter da página.
CREATE TABLE IF NOT EXISTS doc_sources (
  doc_id    INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  source_id TEXT NOT NULL,
  PRIMARY KEY (doc_id, source_id)
);
CREATE INDEX IF NOT EXISTS idx_ds_src ON doc_sources(source_id);

CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""


@dataclass
class Hit:
    chunk_id: int
    docid: str
    path: str
    collection: str
    heading: str
    text: str
    source_type: str | None
    confidence: str | None
    origin: str | None
    score: float


def connect(db_path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(os.path.abspath(db_path)) or ".", exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def make_docid(path: str) -> str:
    return hashlib.sha1(path.encode("utf-8")).hexdigest()[:6]


# ---------- escrita ----------


def _provenance_signature(meta: dict, gaps: list[str]) -> str:
    """Assinatura estável dos metadados de proveniência + gaps de L2.

    Entrar no content_hash é o que faz o reindex detectar `promote` (mudança só
    em `promoted`/`confidence`/`sources`/`supersedes`) sem depender de mudança no
    corpo. Sem isto, o hash do corpo é igual, upsert retorna cedo, e o banco fica
    com metadados velhos — promoted=false no banco mesmo após promote. Bug silencioso.
    """
    import json

    sig = {k: meta.get(k) for k in (
        "id", "source_type", "origin", "confidence", "promoted",
        "supersedes", "topic", "sources",
    )}
    sig["__gaps__"] = sorted(gaps or [])
    return json.dumps(sig, sort_keys=True, default=str, ensure_ascii=False)


def upsert(
    conn: sqlite3.Connection,
    path: str,
    collection: str,
    meta: dict,
    gaps: list[str],
    chunks,
) -> tuple[int, bool]:
    """Insere/atualiza documento. Devolve (doc_id, mudou).

    Hash cobre corpo E proveniência. Se só o frontmatter mudou (caso típico do
    `promote`), o hash difere, os chunks são re-inseridos (mesmo conteúdo), as
    colunas de metadado e `doc_sources` são atualizadas — mas os chunk_ids novos
    não têm embedding, então o passo de embedding do reindex re-embeda só eles.

    Reindex continua incremental: se nada mudou (corpo + provenância iguais),
    retorna cedo sem tocar o banco. Só os chunks cujo conteúdo mudou re-embedam.
    """
    full_text = "\n".join(c.text for c in chunks)
    chash = content_hash(full_text + "\n@@PROV@@\n" + _provenance_signature(meta, gaps))
    row = conn.execute(
        "SELECT id, content_hash FROM documents WHERE path=?", (path,)
    ).fetchone()
    mtime = os.path.getmtime(path) if os.path.exists(path) else None

    if row and row["content_hash"] == chash:
        return row["id"], False

    fields = dict(
        path=path,
        collection=collection,
        docid=make_docid(path),
        content_hash=chash,
        mtime=mtime,
        source_id=meta.get("id"),
        source_type=meta.get("source_type"),
        origin=meta.get("origin"),
        confidence=meta.get("confidence"),
        promoted=meta.get("promoted"),
        supersedes=meta.get("supersedes"),
        topic=meta.get("topic"),
        gaps="; ".join(gaps) if gaps else None,
        indexed_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    )
    if row:
        doc_id = row["id"]
        sets = ", ".join(f"{k}=?" for k in fields)
        conn.execute(
            f"UPDATE documents SET {sets} WHERE id=?", (*fields.values(), doc_id)
        )
        conn.execute("DELETE FROM chunks WHERE doc_id=?", (doc_id,))
    else:
        cols = ", ".join(fields)
        qs = ", ".join("?" * len(fields))
        cur = conn.execute(
            f"INSERT INTO documents ({cols}) VALUES ({qs})", tuple(fields.values())
        )
        doc_id = cur.lastrowid

    conn.executemany(
        "INSERT INTO chunks (doc_id, ord, heading, text, token_count) VALUES (?,?,?,?,?)",
        [(doc_id, c.ord, c.heading, c.text, c.token_count) for c in chunks],
    )

    conn.execute("DELETE FROM doc_sources WHERE doc_id=?", (doc_id,))
    srcs = meta.get("sources") or []
    if isinstance(srcs, str):
        srcs = [s.strip() for s in srcs.split(",") if s.strip()]
    conn.executemany(
        "INSERT OR IGNORE INTO doc_sources (doc_id, source_id) VALUES (?,?)",
        [(doc_id, s) for s in srcs],
    )
    return doc_id, True


def prune(conn: sqlite3.Connection, seen_paths: set[str]) -> int:
    """Remove documentos que sumiram do disco.

    Sem isso o índice cita página deletada — o mesmo problema de espelhamento
    de deleção que já discutimos no publish.
    """
    rows = conn.execute("SELECT id, path FROM documents").fetchall()
    gone = [r["id"] for r in rows if r["path"] not in seen_paths]
    for i in gone:
        conn.execute("DELETE FROM chunks WHERE doc_id=?", (i,))
        conn.execute("DELETE FROM documents WHERE id=?", (i,))
    return len(gone)


def pack(vec) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def unpack(blob: bytes):
    return struct.unpack(f"{len(blob)//4}f", blob)


def set_embedding(conn: sqlite3.Connection, chunk_id: int, vec) -> None:
    conn.execute("UPDATE chunks SET embedding=? WHERE id=?", (pack(vec), chunk_id))


def chunks_without_embedding(conn: sqlite3.Connection):
    return conn.execute(
        "SELECT id, text FROM chunks WHERE embedding IS NULL ORDER BY id"
    ).fetchall()


# ---------- filtros ----------

_FILTER_COLS = {
    "collection",
    "source_type",
    "confidence",
    "topic",
    "promoted",
    "supersedes",
    "source_id",
}


def build_where(filters: dict) -> tuple[str, list]:
    """Proveniência como coluna. Chave desconhecida é ERRO, não é ignorada.

    O QMD ignora parâmetro desconhecido em silêncio — você acha que escopou e
    buscou tudo. Aqui isso explode, de propósito.
    """
    clauses, params = [], []
    for k, v in (filters or {}).items():
        if k not in _FILTER_COLS:
            raise ValueError(
                f"filtro desconhecido: {k!r}. Válidos: {sorted(_FILTER_COLS)}"
            )
        if isinstance(v, (list, tuple)):
            clauses.append(f"d.{k} IN ({','.join('?' * len(v))})")
            params.extend(v)
        elif v is None:
            clauses.append(f"d.{k} IS NULL")
        else:
            clauses.append(f"d.{k} = ?")
            params.append(v)
    return (" AND ".join(clauses) if clauses else "1=1"), params


# ---------- busca léxica ----------

_TERM = re.compile(r'"[^"]*"|\S+')

# Operadores FTS5 reconhecidos entre termos. Sem isto, `lex: a OR b` vira
# `"a" "OR" "b"` (AND de três frases, e "OR" não existe em chunk nenhum).
# A doc (INSTALL.md, retrieval.md) promete OR; isto faz a doc ser verdade.
_FTS5_OPS = {"OR", "AND", "NOT"}


def to_fts_query(q: str) -> str:
    """Traduz a mini-sintaxe para FTS5.

      -termo      -> NOT "termo"
      "a frase"   -> "a frase"
      termo       -> "termo"    (aspas escapam caractere especial)
      OR / AND    -> operador FTS5 (case-insensitive), sem aspas
    """
    out = []
    for tok in _TERM.findall(q or ""):
        neg = tok.startswith("-") and len(tok) > 1
        if neg:
            tok = tok[1:]
        upper = tok.upper()
        if upper in _FTS5_OPS:
            out.append(upper)
            continue
        if tok.startswith('"') and tok.endswith('"') and len(tok) >= 2:
            phrase = tok
        else:
            phrase = '"' + tok.replace('"', "") + '"'
        out.append(("NOT " if neg else "") + phrase)
    return " ".join(out)


def search_lex(conn, query: str, filters: dict, limit: int) -> list[tuple[int, float]]:
    fts_q = to_fts_query(query)
    if not fts_q.strip():
        return []
    where, params = build_where(filters)
    sql = f"""
      SELECT c.id AS cid, bm25(chunks_fts) AS s
      FROM chunks_fts
      JOIN chunks c    ON c.id = chunks_fts.rowid
      JOIN documents d ON d.id = c.doc_id
      WHERE chunks_fts MATCH ? AND {where}
      ORDER BY s
      LIMIT ?
    """
    rows = conn.execute(sql, (fts_q, *params, limit)).fetchall()
    return [(r["cid"], -float(r["s"])) for r in rows]  # bm25: menor = melhor


# ---------- busca vetorial ----------


def search_vec(conn, qvec, filters: dict, limit: int) -> list[tuple[int, float]]:
    """Cosseno em memória.

    Na escala de uma wiki (dezenas de milhares de chunks) isso resolve. Só vale
    sqlite-vec/ANN acima de ~100k chunks — e aí o gargalo é outro.
    """
    import numpy as np

    where, params = build_where(filters)
    rows = conn.execute(
        f"""SELECT c.id AS cid, c.embedding AS e
            FROM chunks c JOIN documents d ON d.id = c.doc_id
            WHERE c.embedding IS NOT NULL AND {where}""",
        params,
    ).fetchall()
    if not rows:
        return []
    mat = np.array([unpack(r["e"]) for r in rows], dtype=np.float32)
    q = np.asarray(qvec, dtype=np.float32)
    denom = np.linalg.norm(mat, axis=1) * (np.linalg.norm(q) + 1e-9) + 1e-9
    sims = (mat @ q) / denom
    idx = np.argsort(-sims)[:limit]
    return [(rows[i]["cid"], float(sims[i])) for i in idx]


# ---------- fusão ----------


def rrf(ranked_lists: list[list[tuple[int, float]]], weights: list[float], k: int = 60):
    """Reciprocal Rank Fusion.

    k=60 é o valor da literatura e o que o QMD usa. A primeira sub-query recebe
    peso 2x — mesma convenção do QMD, e o motivo é o mesmo: a primeira é o seu
    melhor palpite, não uma variante gerada por modelo.
    """
    scores: dict[int, float] = {}
    for lst, w in zip(ranked_lists, weights):
        for rank, (cid, _) in enumerate(lst, start=1):
            scores[cid] = scores.get(cid, 0.0) + w * (1.0 / (k + rank))
    return sorted(scores.items(), key=lambda x: -x[1])


def hydrate(conn, scored: list[tuple[int, float]]) -> list[Hit]:
    out = []
    for cid, sc in scored:
        r = conn.execute(
            """SELECT c.id, c.heading, c.text, d.docid, d.path, d.collection,
                      d.source_type, d.confidence, d.origin
               FROM chunks c JOIN documents d ON d.id=c.doc_id WHERE c.id=?""",
            (cid,),
        ).fetchone()
        if r:
            out.append(
                Hit(
                    chunk_id=r["id"],
                    docid=r["docid"],
                    path=r["path"],
                    collection=r["collection"],
                    heading=r["heading"] or "",
                    text=r["text"],
                    source_type=r["source_type"],
                    confidence=r["confidence"],
                    origin=r["origin"],
                    score=sc,
                )
            )
    return out
