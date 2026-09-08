"""`wk delivery prepare`/`wk delivery confirm` — CLI (§9.4/§10/§11).

Fixture: `knowledge.db` real seedado direto via `Repository` (mesmo padrão de
`publishing/tests/test_e2e_publishing.py` e `test_delivery.py`), publicado
com `publishing.release`/`markdown`/`word`/`validate` reais em
`<store>/publicacoes/`. Os comandos são exercitados via `cli.main(argv)`
(mesmo padrão de `test_fix_w0_wk.py`), com stdout/stderr capturados — cobre
o parser, o envelope comum (`_common_payload`/`_render_common`) e os exit
codes de verdade, não só as funções internas de `publishing.delivery`.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest

from knowledge import evidence as ev_mod
from knowledge.models import (
    Alias,
    AliasOrigin,
    ContentKind,
    EntityDraft,
    EntityType,
    EpistemicStatus,
    FactDraft,
    FactNature,
    LifecycleStatus,
    RelationDraft,
    RelationType,
    SourceKind,
)
from knowledge.repository import Repository

from publishing import markdown as md_mod
from publishing import release
from publishing import validate as validate_mod
from publishing import word as word_mod
from publishing.planner import plan as plan_fn

from wk import cli

NS = "code/repo-delivery-cli"


def _run(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


def _seed(repo):
    """SYS-PAG/CAP-023 com comportamento real evidenciado — o suficiente para
    sair `complete` sem nenhuma lacuna (o caminho feliz da CLI)."""
    src = repo.register_source(NS, SourceKind.CODE, "git://app")
    ver = repo.register_source_version(src, "commit:abc123", "de" * 32)
    ids: dict = {}
    with repo.revision(author="pipeline:codescan", reason="snapshot") as rev:
        ev = rev.add_evidence(ev_mod.make_evidence(
            NS, SourceKind.CODE, ContentKind.EXECUTABLE, ver.source_version_id,
            {"repo": "app", "commit": "abc123", "path": "src/rules.py",
             "start_line": 1, "end_line": 9, "snippet_hash": "h1"},
        ))

        def ent(etype, key, title):
            r = rev.put_entity(EntityDraft(
                NS, etype, key, title, source_version_id=ver.source_version_id,
                aliases=(Alias(key, AliasOrigin.METADATA_ID),),
            ))
            ids[key] = r.target_id
            return r.target_id

        sysid = ent(EntityType.SYSTEM, "SYS-PAG", "Plataforma de pagamentos")
        cap = ent(EntityType.CAPABILITY, "CAP-023", "CAP-023 Autenticação de parceiro")
        rn = ent(EntityType.BUSINESS_RULE, "RN-023", "RN-023 Limite de tentativas")

        for s, t, target in ((sysid, RelationType.CONTAINS, cap), (cap, RelationType.CONTAINS, rn)):
            rev.put_relation(RelationDraft(
                NS, s, t, target, scope="src", epistemic_status=EpistemicStatus.SUPPORTED,
                lifecycle_status=LifecycleStatus.CURRENT, asserted_by="extractor:codescan",
                evidence_refs=(ev,), support_recorded_by="pipeline:verify-static",
                source_version_id=ver.source_version_id,
            ))

        def fact(subject, pred, value):
            return rev.put_fact(FactDraft(
                NS, subject, pred, value, "src/rules.py", FactNature.IMPLEMENTED,
                EpistemicStatus.SUPPORTED, LifecycleStatus.CURRENT, asserted_by="extractor:codescan",
                evidence_refs=(ev,), source_version_id=ver.source_version_id,
                support_recorded_by="pipeline:verify-static",
            )).target_id

        fact(rn, "condition", "a tentativa de autenticacao falha por credencial invalida")
        fact(rn, "behavior", "bloqueia o parceiro apos 3 tentativas malsucedidas em 10 minutos")
        ids["pub_revision"] = rev.revision_id
    return ids


class _RealRenderers:
    markdown = md_mod
    word = word_mod


class TestDeliveryCLI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="wiki-ai-delivery-cli-")
        cls.store_root = os.path.join(cls.tmp, "store")
        os.makedirs(cls.store_root)
        cls.db_path = os.path.join(cls.store_root, "knowledge.db")
        cls.repo = Repository.open(cls.db_path)
        cls.ids = _seed(cls.repo)
        publication_root = os.path.join(cls.store_root, "publicacoes")
        plan_obj = plan_fn(cls.repo, cls.ids["pub_revision"], namespace=None)
        result = release.publish_revision(
            plan_obj, publication_root, _RealRenderers(), validate_mod.StagedValidators,
            revision_id=cls.ids["pub_revision"],
        )
        assert result.ok, f"publicação de fixture falhou: {result.blocked_by}"

    @classmethod
    def tearDownClass(cls):
        cls.repo.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _out(self, name):
        return os.path.join(self.tmp, "cli-out-" + name)

    # -- prepare: caminho feliz, humano e --json -----------------------------

    def test_prepare_humano_mostra_resultado_conhecimento_entrega_impedimento_proxima_acao(self):
        out = self._out("humano")
        code, stdout, stderr = _run([
            "delivery", "prepare", "--store", self.store_root,
            "--destination", "dest-cli-1", "--out", out,
        ])
        # Achado do próprio pipeline real (não deste comando): CAP-023 tem
        # uma unidade PRÓPRIA de identidade/composição (estrutural, além da
        # unidade behavioral de RN-023) — o documento de capacidade sai
        # `parcial` (§9.4: lacuna requerida impede COMPLETO, não impede a
        # entrega). `operation_status=partial` -> exit 3, nunca bloqueado.
        self.assertEqual(code, 3, stderr)
        for label in ("resultado:", "conhecimento:", "entrega:", "impedimento:", "próxima ação"):
            self.assertIn(label, stdout, stdout)
        self.assertTrue(os.path.isfile(os.path.join(out, "delivery.json")))

    def test_prepare_json_traz_campos_comuns_sempre_presentes(self):
        out = self._out("json1")
        code, stdout, stderr = _run([
            "delivery", "prepare", "--store", self.store_root,
            "--destination", "dest-cli-2", "--out", out, "--json",
        ])
        self.assertEqual(code, 3, stderr)
        payload = json.loads(stdout)
        for key in (
            "schema_version", "command", "operation_status", "knowledge_status",
            "delivery_status", "scope", "agent", "summary", "pending", "next_actions",
            "publication", "delivery",
        ):
            self.assertIn(key, payload, f"campo comum ausente: {key}")
        self.assertEqual(payload["command"], "delivery prepare")
        self.assertEqual(payload["operation_status"], "partial")
        self.assertEqual(payload["knowledge_status"], "partial")
        self.assertEqual(payload["delivery_status"], "ready")  # elegível, mesmo não sendo "completo"
        self.assertEqual(len(payload["pending"]), 1)
        pend = payload["pending"][0]
        for key in ("id", "tipo", "alvo", "causa", "impacto", "recuperacao_automatica", "acoes"):
            self.assertIn(key, pend)
        self.assertIsInstance(payload["next_actions"], list)
        self.assertTrue(payload["next_actions"])
        na = payload["next_actions"][0]
        self.assertIsInstance(na["argv"], list)
        self.assertIn("--delivery", na["argv"])
        # agent: `BindingStore.to_public_dict` real (runtime/bindings.py) —
        # sem preferência/binding registrados para este store de teste.
        self.assertIsNone(payload["agent"]["binding_id"])
        self.assertIsNone(payload["agent"]["agent_id"])
        self.assertIsNone(payload["agent"]["capabilities"])
        self.assertEqual(payload["agent"]["connection_status"], "unselected")

    def test_prepare_json_next_action_argv_e_quoting_por_shell(self):
        """§10.2: o vetor de argumentos guarda store/repo/args completos; o
        texto copiável é RENDERIZADO A PARTIR do vetor, com o shell declarado
        (nunca um texto solto divergente do vetor)."""
        out = self._out("json-argv")
        code, stdout, stderr = _run([
            "delivery", "prepare", "--store", self.store_root,
            "--destination", "dest-cli-argv", "--out", out, "--json",
        ])
        self.assertIn(code, (0, 3), stderr)
        payload = json.loads(stdout)
        na = payload["next_actions"][0]
        self.assertEqual(na["argv"][:3], ["wk", "delivery", "confirm"])
        self.assertIn(self.store_root, na["argv"])
        self.assertIn(na["shell"], ("powershell", "posix"))
        rendered = cli._quote_argv(na["argv"], na["shell"])
        self.assertIn("delivery", rendered)
        self.assertIn("confirm", rendered)
        # store_root (path com espaços/backslash em Windows) sobrevive ao quoting
        self.assertIn(os.path.basename(self.store_root), rendered)

    def test_prepare_com_repo_inexistente_e_bloqueado_exit_2(self):
        out = self._out("repo-bad")
        code, stdout, stderr = _run([
            "delivery", "prepare", "--store", self.store_root,
            "--repo", os.path.join(self.tmp, "nao-existe"),
            "--destination", "dest-cli-3", "--out", out, "--json",
        ])
        self.assertEqual(code, 2)
        payload = json.loads(stdout)
        self.assertEqual(payload["operation_status"], "blocked")
        self.assertEqual(payload["delivery_status"], "not_ready")
        self.assertTrue(payload["pending"])
        self.assertFalse(os.path.exists(out))

    def test_prepare_selecao_vazia_e_bloqueado(self):
        out = self._out("empty-sel")
        code, stdout, stderr = _run([
            "delivery", "prepare", "--store", self.store_root,
            "--initiative", "ent_inexistente_de_verdade",
            "--destination", "dest-cli-4", "--out", out, "--json",
        ])
        self.assertEqual(code, 2)
        payload = json.loads(stdout)
        self.assertEqual(payload["operation_status"], "blocked")

    # -- confirm: caminho feliz, idempotência, erro ---------------------------

    def test_confirm_apos_prepare_succeeded_e_noop_na_repeticao(self):
        out = self._out("confirm-1")
        code, stdout, _ = _run([
            "delivery", "prepare", "--store", self.store_root,
            "--destination", "dest-cli-5", "--out", out, "--json",
        ])
        self.assertIn(code, (0, 3))  # elegível (`ready`) mesmo quando `partial`
        delivery_id = json.loads(stdout)["delivery"]["delivery_id"]

        code1, stdout1, stderr1 = _run([
            "delivery", "confirm", "--store", self.store_root, "--delivery", delivery_id,
            "--destination", "dest-cli-5", "--verified-by", "operador.cli", "--json",
        ])
        self.assertEqual(code1, 0, stderr1)
        p1 = json.loads(stdout1)
        self.assertEqual(p1["operation_status"], "succeeded")
        self.assertEqual(p1["delivery"]["remote_verification"], "declared_by_operator")

        code2, stdout2, stderr2 = _run([
            "delivery", "confirm", "--store", self.store_root, "--delivery", delivery_id,
            "--destination", "dest-cli-5", "--verified-by", "operador.cli", "--json",
        ])
        self.assertEqual(code2, 0, stderr2)
        p2 = json.loads(stdout2)
        self.assertEqual(p2["operation_status"], "noop")

    def test_confirm_delivery_id_desconhecido_e_bloqueado_exit_2(self):
        code, stdout, stderr = _run([
            "delivery", "confirm", "--store", self.store_root, "--delivery", "dlv_nao_existe",
            "--destination", "dest-cli-6", "--verified-by", "operador.cli", "--json",
        ])
        self.assertEqual(code, 2)
        payload = json.loads(stdout)
        self.assertEqual(payload["operation_status"], "blocked")
        self.assertTrue(payload["pending"])

    # -- status: delivery_status aditivo --------------------------------------

    def test_status_delivery_status_e_a_mesma_funcao_de_elegibilidade(self):
        """§10.3: `status --json` ganha `delivery_status` calculado pela MESMA
        função de `delivery prepare` (`publishing.delivery.eligibility`).
        `cmd_status` inteiro depende de `runtime.db` (pré-existente, fora do
        escopo desta tarefa); aqui o hook aditivo é exercitado diretamente
        sobre o store real da fixture, sem duplicar aquele requisito."""
        status = cli._status_delivery_status(self.store_root, None)
        self.assertEqual(status, "ready")  # publicação completa desta fixture
        self.assertEqual(
            cli._status_delivery_status(os.path.join(self.tmp, "store-sem-publicacao"), None),
            "not_applicable",
        )

    # -- sem alias para os comandos removidos --------------------------------

    def test_delivery_sem_subcomando_e_erro_de_uso(self):
        with self.assertRaises(SystemExit) as ctx:
            _run(["delivery"])
        self.assertNotEqual(ctx.exception.code, 0)


if __name__ == "__main__":
    unittest.main()
