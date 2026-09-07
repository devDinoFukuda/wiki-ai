"""Onda11 — validacao README: achados D5 e D8, unico arquivo desta micro-tarefa
(dono exclusivo `wk/cli.py`; NADA fora dele foi editado).

D5 — corrupcao silenciosa de namespace: o Git Bash/MSYS pode reescrever um
argumento com cara de caminho absoluto (`code/C:/Users/...`) para o prefixo
de instalacao do Git, produzindo algo como
`code\\C;C:\\Program Files\\Git\\Users\\...`. Sem barreira, o comando CRIAVA
esse namespace errado no `knowledge.db` sem aviso nenhum. `_validate_namespace`
(cli.py) barra ANTES de qualquer leitura/escrita — testado aqui isolado e nos
dois comandos que aceitam `--namespace` (`ingest`/`ingest2`, que compartilham
`cmd_ingest`, e `migrate`; confirmado por grep em `_build_parser` que nenhum
outro subcomando declara `--namespace`).

D8 — reingestao duplicada mascarava pendencias: o curto-circuito por `svid`
em `correlate()` (`ingestion/correlate.py` §8.2.1, NAO editado nesta
micro-tarefa) devolve `CorrelationResult` com `completa=True`/
`referencias_orfas=()` de DEFAULT quando a fonte ja foi ingerida antes — a
correlacao nao e reexecutada. Sem o campo aditivo `correlacao_reavaliada` e o
aviso literal, `fontes_incompletas: 0` e `status_geral: "completo"` da
reingestao pareciam uma avaliacao NOVA, escondendo pendencias (ex.:
referencia orfa RN-023) que a ingestao original ja tinha deixado em aberto.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import tempfile
from contextlib import redirect_stderr, redirect_stdout

import pytest

from wk import cli


# ---------------------------------------------------------------------------
# D5 — `_validate_namespace` isolada (rapido, sem I/O)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ns", [
    "wiki",
    "code/C:/tmp/x",
    "code/C:/Users/dino/projeto",
    "minha-iniciativa_2026.09",
])
def test_validate_namespace_aceita_namespace_legitimo(ns):
    assert cli._validate_namespace(ns) is None


def test_validate_namespace_rejeita_backslash():
    erro = cli._validate_namespace("code" + chr(92) + "C" + chr(92) + "tmp")
    assert erro is not None
    assert erro["causa"] == "namespace corrompido pelo shell (conversão de caminho do Git Bash)"
    assert "MSYS2_ARG_CONV_EXCL" in erro["correcao"]


def test_validate_namespace_rejeita_semicolon():
    erro = cli._validate_namespace("code;rm -rf")
    assert erro is not None
    assert "causa" in erro and "correcao" in erro


def test_validate_namespace_rejeita_newline():
    erro = cli._validate_namespace("code\nx")
    assert erro is not None


def test_validate_namespace_rejeita_mangling_msys_mesmo_sem_backslash():
    """O padrão de mangling (`Program Files/Git`) sozinho, com barras normais
    (sem `\\`/`;`), já denuncia a corrupção — não depende dos outros dois
    sinais."""
    erro = cli._validate_namespace("code/C:/Program Files/Git/Users/x")
    assert erro is not None


def test_validate_namespace_reproduz_o_mangling_real_do_git_bash():
    """Reproducao literal do exemplo do achado D5: `code/C:/Users/...` vira
    `code\\C;C:\\Program Files\\Git\\Users\\...` quando o Git Bash reescreve o
    argumento."""
    mangled = "code" + chr(92) + "C;C:" + chr(92) + "Program Files" + chr(92) + "Git" + chr(92) + "Users" + chr(92) + "x"
    erro = cli._validate_namespace(mangled)
    assert erro is not None
    assert erro["causa"] == "namespace corrompido pelo shell (conversão de caminho do Git Bash)"


# ---------------------------------------------------------------------------
# fixtures de integracao (store + fonte reais em disco)
# ---------------------------------------------------------------------------


@pytest.fixture
def store_e_fonte(tmp_path):
    store = tmp_path / "store"
    src = tmp_path / "doc.md"
    src.write_text("# Teste\n\nConteudo qualquer, sem referência especial.\n", encoding="utf-8")
    return str(store), str(src)


def _ingest_args(path, store, namespace):
    return argparse.Namespace(path=path, initiative=None, phase=None, store=store, namespace=namespace)


def _run_ingest(a):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.cmd_ingest(a)
    return code, out.getvalue(), err.getvalue()


# ---------------------------------------------------------------------------
# D5 — integração em `cmd_ingest`
# ---------------------------------------------------------------------------


def test_cmd_ingest_recusa_namespace_corrompido_exit2_sem_gravar_nada(store_e_fonte):
    store, src = store_e_fonte
    mangled = "code" + chr(92) + "C;C:" + chr(92) + "Program Files" + chr(92) + "Git" + chr(92) + "x"
    code, out, err = _run_ingest(_ingest_args(src, store, mangled))

    assert code == 2
    assert out == ""  # nada de JSON de sucesso em stdout
    diagnostico = json.loads(err)
    assert diagnostico["causa"] == "namespace corrompido pelo shell (conversão de caminho do Git Bash)"
    assert "MSYS2_ARG_CONV_EXCL" in diagnostico["correcao"]
    # recusado ANTES de abrir o repo: nenhum knowledge.db corrompido nasce.
    assert not os.path.exists(os.path.join(store, "knowledge.db"))


def test_cmd_ingest_aceita_namespace_legitimo_com_barras_normais(store_e_fonte):
    store, src = store_e_fonte
    code, out, err = _run_ingest(_ingest_args(src, store, "code/C:/tmp/x"))
    assert code == 0
    assert err == ""
    resultado = json.loads(out)
    assert resultado["fontes"][0]["status"] == "ingested"


# ---------------------------------------------------------------------------
# D5 — integração em `cmd_migrate` (mesma barreira, outro comando)
# ---------------------------------------------------------------------------


def test_cmd_migrate_recusa_namespace_corrompido_exit2_sem_criar_backup(tmp_path):
    store = tmp_path / "store"
    store.mkdir()
    mangled = "code" + chr(92) + "C;C:" + chr(92) + "Program Files" + chr(92) + "Git" + chr(92) + "x"
    a = argparse.Namespace(store=str(store), backup_dir=None, namespace=mangled)

    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.cmd_migrate(a)

    assert code == 2
    diagnostico = json.loads(err.getvalue())
    assert diagnostico["causa"] == "namespace corrompido pelo shell (conversão de caminho do Git Bash)"
    assert "MSYS2_ARG_CONV_EXCL" in diagnostico["correcao"]
    # recusado ANTES do backup obrigatório: nenhum diretório de backup nasce.
    assert not os.path.isdir(store / ".migrate-backup")


# ---------------------------------------------------------------------------
# D8 — reingestão idêntica não mascara pendências
# ---------------------------------------------------------------------------


def test_cmd_ingest_primeira_ingestao_nao_tem_campos_de_duplicata(store_e_fonte):
    store, src = store_e_fonte
    code, out, _ = _run_ingest(_ingest_args(src, store, "wiki"))
    assert code == 0
    entry = json.loads(out)["fontes"][0]
    assert "duplicada" not in entry
    assert "correlacao_reavaliada" not in entry


def test_cmd_ingest_reingestao_identica_avisa_e_marca_correlacao_nao_reavaliada(store_e_fonte):
    store, src = store_e_fonte
    a = _ingest_args(src, store, "wiki")

    code1, out1, _ = _run_ingest(a)
    assert code1 == 0

    code2, out2, _ = _run_ingest(a)  # mesmo arquivo, mesmo namespace: bytes idênticos
    assert code2 == 0
    resultado2 = json.loads(out2)

    entry = resultado2["fontes"][0]
    assert entry["duplicada"] is True
    assert entry["correlacao_reavaliada"] is False, (
        "campo aditivo (achado D8): sem ele, a leitura de `entry` engana como se "
        "fosse uma avaliação nova"
    )
    assert any(
        "reingestao identica: correlacao nao reexecutada" in aviso
        and "consulte wk status" in aviso
        for aviso in resultado2["avisos"]
    ), f"aviso literal do achado D8 ausente em {resultado2['avisos']!r}"


def test_cmd_ingest_reingestao_nao_reporta_fontes_incompletas_como_avaliacao_nova(store_e_fonte):
    """`fontes_incompletas` da reingestão vem do `CorrelationResult` desta
    execução (curto-circuito por `svid`, que nunca marca `completa=False`) —
    continua podendo ser 0. O que o achado D8 corrige não é este número (ele
    é honesto: NINGUÉM reavaliou nada agora), e sim a falta do aviso/campo
    acima que dizia isso — cobertos nos testes anteriores."""
    store, src = store_e_fonte
    a = _ingest_args(src, store, "wiki")
    _run_ingest(a)
    _, out2, _ = _run_ingest(a)
    resultado2 = json.loads(out2)
    assert resultado2["fontes_incompletas"] == 0
    assert resultado2["status_geral"] == "completo"
