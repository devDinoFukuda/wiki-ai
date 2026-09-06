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
import json
import os
import re
import sqlite3
import struct
import time
import unicodedata
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

-- F06/W7: schema versionado de embeddings. Um `space` é a assinatura exata do
-- embedder (provider+model+deployment+dimension+config) que produziu um lote
-- de vetores. Sem isto, trocar de modelo/dimensão mistura gerações de vetor
-- no mesmo índice sem detecção — cosseno entre espaços diferentes é número
-- sem significado, mas SQLite não recusa a conta sozinho.
-- `chunks.embedding_space_id` (coluna, migrada via ALTER TABLE para bancos
-- existentes — ver _migrate_schema) aponta para o space que gerou o vetor
-- daquele chunk; NULL = legado (vetor de antes desta migração, nunca
-- validado contra um space, e por isso nunca entra em busca vetorial nova).
CREATE TABLE IF NOT EXISTS embedding_spaces (
  space_id     INTEGER PRIMARY KEY,
  provider     TEXT NOT NULL,
  model        TEXT NOT NULL,
  deployment   TEXT,
  dimension    INTEGER NOT NULL,
  config_json  TEXT NOT NULL DEFAULT '{}',
  created_at   TEXT NOT NULL,
  active       INTEGER NOT NULL DEFAULT 0    -- no máximo 1 linha com active=1
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_espace_sig
  ON embedding_spaces(provider, model, deployment, dimension, config_json);
CREATE INDEX IF NOT EXISTS idx_espace_active ON embedding_spaces(active);
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
    _migrate_schema(conn)
    return conn


def _migrate_schema(conn: sqlite3.Connection) -> None:
    """Migração aditiva para bancos existentes anteriores ao F06/W7.

    `CREATE TABLE IF NOT EXISTS chunks (...)` no SCHEMA não adiciona coluna a
    uma tabela `chunks` que já existia sem `embedding_space_id` — só
    `ALTER TABLE` faz isso. Idempotente: só roda se a coluna ainda não existe.
    Sem FK inline (`REFERENCES embedding_spaces`) de propósito — ALTER TABLE
    ADD COLUMN com default NULL preserva os vetores legados como estão
    (NULL = legado), sem exigir que já exista uma linha em embedding_spaces.
    """
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(chunks)").fetchall()}
    if "embedding_space_id" not in cols:
        conn.execute("ALTER TABLE chunks ADD COLUMN embedding_space_id INTEGER")
        conn.commit()


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
    não re-insere chunks nem colunas de metadado. Mas `mtime`/`indexed_at` SÃO
    atualizados mesmo nesse caminho — senão um `touch`/checkout que muda o
    mtime em disco sem mudar o conteúdo deixa `index status` "sujo" para
    sempre (o mtime salvo nunca alcança o do disco, porque este early-return
    nunca escrevia no banco). F-12.
    """
    full_text = "\n".join(c.text for c in chunks)
    chash = content_hash(full_text + "\n@@PROV@@\n" + _provenance_signature(meta, gaps))
    row = conn.execute(
        "SELECT id, content_hash FROM documents WHERE path=?", (path,)
    ).fetchone()
    mtime = os.path.getmtime(path) if os.path.exists(path) else None

    if row and row["content_hash"] == chash:
        conn.execute(
            "UPDATE documents SET mtime=?, indexed_at=? WHERE id=?",
            (mtime, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), row["id"]),
        )
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


def set_embedding(conn: sqlite3.Connection, chunk_id: int, vec, space_id: int) -> None:
    """Grava o vetor de um chunk, sempre associado a um `space` (F06/W7).

    `space_id` é obrigatório: um vetor sem espaço declarado é exatamente o
    estado legado que a regra F06 quer evitar daqui pra frente. A dimensão
    REAL do vetor é validada contra a dimensão registrada do space — mismatch
    é erro, não silenciosamente aceito (misturar gerações de embedding no
    mesmo índice é o defeito que este schema existe para impedir).
    """
    row = conn.execute(
        "SELECT dimension FROM embedding_spaces WHERE space_id=?", (space_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"embedding_space_id desconhecido: {space_id!r}")
    if len(vec) != row["dimension"]:
        raise ValueError(
            f"dimensão do vetor ({len(vec)}) não bate com a do espaço "
            f"{space_id} ({row['dimension']}): reindex parcial misturaria gerações"
        )
    conn.execute(
        "UPDATE chunks SET embedding=?, embedding_space_id=? WHERE id=?",
        (pack(vec), space_id, chunk_id),
    )


def chunks_without_embedding(conn: sqlite3.Connection):
    return conn.execute(
        "SELECT id, text FROM chunks WHERE embedding IS NULL ORDER BY id"
    ).fetchall()


def chunks_all(conn: sqlite3.Connection):
    """Todos os chunks (id, text) — usado no reindex completo que precede a
    ativação de um espaço de embedding novo (troca de modelo/dimensão)."""
    return conn.execute("SELECT id, text FROM chunks ORDER BY id").fetchall()


# ---------- espaços de embedding (F06/W7) ----------


def get_or_create_space(
    conn: sqlite3.Connection,
    provider: str,
    model: str,
    deployment: str | None,
    dimension: int,
    config: dict | None = None,
) -> int:
    """Devolve o `space_id` da assinatura (provider, model, deployment,
    dimension, config); cria se ainda não existir. NUNCA ativa sozinho — um
    space novo nasce inativo (`active=0`); ativação é sempre explícita via
    `activate_space`, e só depois que a coleção inteira foi reembedada nele
    (ver cli._cmd_reindex_body). É isto que garante a troca atômica do F06:
    nunca existe um estado em que buscas vejam um space parcialmente populado
    como se fosse o ativo.
    """
    config_json = json.dumps(config or {}, sort_keys=True, ensure_ascii=False)
    row = conn.execute(
        """SELECT space_id FROM embedding_spaces
           WHERE provider=? AND model=? AND deployment IS ?
             AND dimension=? AND config_json=?""",
        (provider, model, deployment, dimension, config_json),
    ).fetchone()
    if row:
        return row["space_id"]
    cur = conn.execute(
        """INSERT INTO embedding_spaces
             (provider, model, deployment, dimension, config_json, created_at, active)
           VALUES (?,?,?,?,?,?,0)""",
        (
            provider,
            model,
            deployment,
            dimension,
            config_json,
            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        ),
    )
    return cur.lastrowid


def get_active_space(conn: sqlite3.Connection):
    """A linha do space ativo (no máximo uma), ou None se o índice nunca
    completou um reindex com embeddings."""
    return conn.execute(
        "SELECT * FROM embedding_spaces WHERE active=1 LIMIT 1"
    ).fetchone()


def activate_space(conn: sqlite3.Connection, space_id: int) -> None:
    """Troca atômica de espaço ativo: desativa todos, ativa só `space_id`.

    Chamado SOMENTE depois que a coleção inteira já foi reembedada no space
    novo (ver cli._cmd_reindex_body) — nunca antes, e nunca sem commit logo em
    seguida, senão a troca deixa de ser atômica.
    """
    conn.execute("UPDATE embedding_spaces SET active=0 WHERE space_id != ?", (space_id,))
    conn.execute("UPDATE embedding_spaces SET active=1 WHERE space_id=?", (space_id,))


def space_signature_matches(space_row, provider: str, model: str, deployment, config_json: str) -> bool:
    """Compara a IDENTIDADE do embedder (provider/model/deployment/config) —
    sem dimensão — contra um space existente. Usado para decidir, ANTES de
    embedar qualquer coisa, se o reindex é incremental (mesma identidade do
    space ativo) ou exige espaço novo (identidade mudou: outro modelo/
    deployment/config, ainda que a dimensão viesse a coincidir por acaso)."""
    return (
        space_row is not None
        and space_row["provider"] == provider
        and space_row["model"] == model
        and space_row["deployment"] == deployment
        and space_row["config_json"] == config_json
    )


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


_STEM_MIN_LEN = 4  # abaixo disso o wildcard fica curto demais (ex.: "a*") e explode em ruído

# Sufixos de flexão/plural PT-BR, do mais específico para o mais genérico —
# checados nessa ordem para que "-ões"/"-ns"/"-ão"/"-is" não sejam engolidos
# pela regra genérica de "-s" antes de terem chance de casar. `cut` é quantos
# caracteres finais são removidos; `add` é o que entra no lugar (pode ser "").
# Operam SEM acento: o índice normaliza via `remove_diacritics 2`
# (unicode61), então tanto a base quanto o wildcard têm que ir sem acento —
# senão o wildcard nunca casa com o token indexado.
_PT_SUFFIX_RULES: tuple[tuple[str, int, str], ...] = (
    ("oes", 3, ""),   # integrações -> integrac* (casa com integração -> integrac*)
    ("ns", 2, "m"),   # homens -> homem
    ("ao", 2, ""),    # integracao -> integrac* (casa com integrações -> integrac*)
    ("is", 2, "l"),   # papeis -> papel
    ("es", 2, ""),    # professores -> professor
    ("s", 1, ""),     # servicos -> servico
)


def _strip_diacritics(s: str) -> str:
    nfd = unicodedata.normalize("NFD", s)
    return "".join(ch for ch in nfd if unicodedata.category(ch) != "Mn")


def _pt_stem(word: str) -> str | None:
    """Stem morfológico leve PT-BR para expansão de recall no FTS5 (F-34).

    FTS5 (unicode61 + remove_diacritics 2, sem stemming) trata "integração" e
    "integrações" como tokens totalmente diferentes — a frase exata não acha
    a flexão. Sem mudar o schema (sem migração/reindex), a expansão acontece
    do lado da query: gera um stem conservador + wildcard de prefixo
    (`stem*`), casando com qualquer flexão que compartilhe o mesmo radical no
    índice.

    Sanitiza para alfanumérico sem acento (o wildcard não pode carregar aspas
    nem os caracteres especiais do FTS5 — a query resultante continua tendo
    que passar pelo saneamento F-20). Aplica no máximo UMA regra — a primeira
    que casar, da mais específica à mais genérica — e só devolve stem se o
    resultado tiver pelo menos `_STEM_MIN_LEN` caracteres e for diferente da
    palavra original (senão a expansão é redundante com o termo exato).
    """
    base = re.sub(r"[^0-9a-z]", "", _strip_diacritics(word.lower()))
    if len(base) < _STEM_MIN_LEN:
        return None
    for suf, cut, add in _PT_SUFFIX_RULES:
        if base.endswith(suf) and len(base) > len(suf):
            stem = base[:-cut] + add
            if len(stem) >= _STEM_MIN_LEN and stem != base:
                return stem
    return None


def to_fts_query(q: str) -> str:
    """Traduz a mini-sintaxe para FTS5.

      -termo      -> NOT "termo"
      "a frase"   -> "a frase"
      termo       -> "termo"    (aspas escapam caractere especial)
      OR / AND    -> operador FTS5 (case-insensitive), sem aspas

    FTS5 é binário: `a OR b`, `a NOT b`. Um operador SEM operando de um dos
    lados (`python OR` no fim, `-x` sozinho -> `NOT "x"` sem operando à
    esquerda, `OR foo` no início) não é "OR/NOT vazio" — é
    sqlite3.OperationalError não tratada (F-20). Em vez de emitir o
    operador pendente, ele é degradado para termo literal: perde-se a
    semântica do operador, mas a query nunca quebra o parser do FTS5.

    F-34: todo termo positivo avulso (não frase entre aspas, não operador,
    não negado) com >= 4 caracteres ganha uma alternativa morfológica —
    `("termo" OR stem*)` — para recall de flexões PT-BR (plural, "-ção"/
    "-ções" etc.) que o FTS5 sem stemming não recupera sozinho. Frase entre
    aspas e termo negado (`-termo`) NUNCA expandem: frase é busca literal
    exata por contrato, e negar uma família morfológica inteira em vez do
    termo exato seria surpreendente (excluiria mais do que o usuário pediu).
    """
    tokens = _TERM.findall(q or "")
    items: list[tuple] = []  # ("op", "OR"/"AND"/"NOT") | ("term", neg, phrase, stem)
    for tok in tokens:
        neg = tok.startswith("-") and len(tok) > 1
        if neg:
            tok = tok[1:]
        upper = tok.upper()
        if not neg and upper in _FTS5_OPS:
            items.append(("op", upper))
            continue
        is_phrase = tok.startswith('"') and tok.endswith('"') and len(tok) >= 2
        if is_phrase:
            phrase = tok
        else:
            phrase = '"' + tok.replace('"', "") + '"'
        stem = _pt_stem(tok) if (not neg and not is_phrase and len(tok) >= _STEM_MIN_LEN) else None
        items.append(("term", neg, phrase, stem))

    out: list[str] = []
    have_operand = False  # há um operando à esquerda pronto para casar
    pending_op: str | None = None  # OR/AND esperando o operando à direita
    n = len(items)
    for i, item in enumerate(items):
        if item[0] == "op":
            word = item[1]
            has_right = i + 1 < n
            if not have_operand or not has_right:
                # operador sem operando (início ou fim da query): termo literal
                out.append('"' + word + '"')
                have_operand = True
                pending_op = None
            else:
                pending_op = word
                have_operand = False
        else:
            _, neg, phrase, stem = item
            operand = f"({phrase} OR {stem}*)" if stem else phrase
            if neg and not have_operand:
                # `-termo` isolado / sem operando à esquerda para o NOT:
                # negação sem alvo não tem como virar FTS5 válido -> termo
                # literal (positivo), em vez de "NOT" pendurado sem operando.
                out.append(phrase)
            elif neg:
                out.append("NOT " + phrase)
            else:
                if pending_op:
                    out.append(pending_op)
                    pending_op = None
                out.append(operand)
            have_operand = True
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
    try:
        rows = conn.execute(sql, (fts_q, *params, limit)).fetchall()
    except sqlite3.OperationalError as e:
        # Rede de segurança além da sanitização em to_fts_query: qualquer
        # outra forma de query que o FTS5 rejeite vira erro claro (com a
        # query ofensiva), não um traceback cru até o operador. F-20.
        raise ValueError(f"query léxica inválida: {e} (query={fts_q!r})") from e
    return [(r["cid"], -float(r["s"])) for r in rows]  # bm25: menor = melhor


# ---------- busca vetorial ----------


def search_vec(conn, qvec, space_id: int, filters: dict, limit: int) -> list[tuple[int, float]]:
    """Cosseno em memória, restrito ao `space_id` da query (F06/W7).

    `space_id` é obrigatório e nunca inferido: comparar vetores de espaços
    diferentes (ou legados, `embedding_space_id IS NULL`) é número sem
    significado — cosseno entre gerações de embedding distintas não mede
    similaridade nenhuma, mesmo quando as dimensões batem por coincidência.
    O chamador (cli.py) resolve o space da query e barra ANTES de chegar
    aqui se o embedder atual não bater com o space ativo do índice.

    Na escala de uma wiki (dezenas de milhares de chunks) isso resolve. Só vale
    sqlite-vec/ANN acima de ~100k chunks — e aí o gargalo é outro.
    """
    import numpy as np

    where, params = build_where(filters)
    rows = conn.execute(
        f"""SELECT c.id AS cid, c.embedding AS e
            FROM chunks c JOIN documents d ON d.id = c.doc_id
            WHERE c.embedding_space_id = ? AND {where}""",
        (space_id, *params),
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
