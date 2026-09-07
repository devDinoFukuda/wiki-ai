"""Testes permanentes de audit findings #1/#5/#4/#3 (Onda10-A).

Contrato: assinatura REAL de knowledge.integrate.integrate() e identity.find_by_alias().
Fixture: ConflictError(409) quando total > 1000.
"""

import json
import os
import sys
import tempfile
import unittest

from analysis.extractors.base import SourceFile
from analysis.extractors.registry import default_registry
from analysis.snapshot import capture
from analysis.verification import Claim, ClaimEvidence, verify_claim
from knowledge import identity as id_mod
from knowledge import integrate as I
from knowledge.models import EntityType, EpistemicStatus
from knowledge.repository import Repository
from runtime import tasks as rt_tasks


RULES_A = '''\
class ConflictError(Exception):
    pass


def approve_order(total):
    if total > 1000:
        raise ConflictError(409)
    return "ok"
'''

RULES_B = '''\
def compute_discount(qty):
    if qty > 10:
        return 15
    return 0
'''


def write_repo(root: str, name: str, content: str) -> str:
    path = os.path.join(root, name)
    os.makedirs(path, exist_ok=True)
    with open(os.path.join(path, "rules.py"), "w", encoding="utf-8") as fh:
        fh.write(content)
    return path


def extraction_of(snapshot):
    registry = default_registry()
    files = []
    for entry in snapshot.files:
        with open(os.path.join(snapshot.repo, entry.path), "r", encoding="utf-8") as fh:
            files.append(SourceFile(path=entry.path, content=fh.read()))
    return registry.extract_all(files)


def evidence(path="rules.py", a=5, b=8):
    return [{"path": path, "line_start": a, "line_end": b}]


def worker_output(objective_id, capability_id, afirmacoes, extra=None):
    out = {
        "objective_id": objective_id,
        "capability_id": capability_id,
        "contract": {"decisoes": {"status": "filled", "afirmacoes": afirmacoes}},
    }
    out.update(extra or {})
    return out


def accepted(objective_id, capability_id, output, input_versions, task_id="t1"):
    return I.AcceptedResult(
        task_id=task_id,
        kind="investigation",
        objective_id=objective_id,
        capability_id=capability_id,
        execution_id="exec-1",
        input_versions=input_versions,
        input_versions_hash=rt_tasks.input_versions_hash(input_versions),
        output=output,
        objective_payload={"objective_id": objective_id, "capability_id": capability_id},
        asserted_by="llm:worker",
        created_at="2026-09-06T00:00:00Z",
    )


def objective_dict(objective_id, capability_id, name, reading_needs=()):
    from analysis.investigation import (
        CONTRACT_FIELDS,
        CONTRACT_LABELS,
        ContractField,
    )

    return {
        "objective_id": objective_id,
        "kind": "capability",
        "capability_id": capability_id,
        "name": name,
        "contract": {
            n: ContractField(name=n, label=CONTRACT_LABELS[n]).to_dict() for n in CONTRACT_FIELDS
        },
        "reading_needs": list(reading_needs),
        "state": "partial",
    }


class AchadoFindingOneTest(unittest.TestCase):
    """ACHADO #1: condicao aprovada, consequencia ignorada."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="onda10a-1-")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_finding_one_disputed_vs_supported_vs_unresolved(self):
        """'aprovado com HTTP 200' vira DISPUTED; 'devolve 409' SUPPORTED; 'notifica' nunca supported."""
        repo_a = write_repo(self.tmpdir, "repo_a", RULES_A)
        snap_a = capture(repo_a)
        extr_a = extraction_of(snap_a)

        afirmacoes_1 = [
            {
                "regra": "RN-023 Limite de aprovacao",
                "statement": "Quando total > 1000, o pedido e aprovado com HTTP 200.",
                "evidence_refs": evidence(),
            },
            {
                "regra": "RN-024 Conflito por limite",
                "statement": "Quando total > 1000 devolve 409",
                "evidence_refs": evidence(),
            },
            {
                "regra": "RN-025 Aviso ao gerente",
                "statement": "Quando total > 1000 e o sistema notifica o gerente",
                "evidence_refs": evidence(),
            },
        ]

        db1 = os.path.join(self.tmpdir, "knowledge.db")
        repo1 = Repository.open(db1)
        try:
            out_a = worker_output("obj-a", "cap-a", afirmacoes_1)
            res_a = accepted(
                "obj-a", "cap-a", out_a, {"snapshot_id": snap_a.snapshot_id, "source_version_ids": []}
            )
            rep1 = I.integrate(
                repo1,
                None,
                snap_a,
                extr_a,
                "code/repo-a",
                objectives=[objective_dict("obj-a", "cap-a", "Aprovacao de pedido")],
                results=[res_a],
            )
            d1 = rep1.to_dict()
            por_texto = {}
            for obj in d1["objetivos"]:
                for f in obj["fatos"]:
                    por_texto[f["claim_id"]] = f
            claims = I.results_to_claims(res_a)
            texto = {c.claim_id: c.statement for c in claims}

            by_stmt = {texto[cid]: f for cid, f in por_texto.items() if cid in texto}

            # Assertion 1a: "aprovado com HTTP 200" -> DISPUTED
            self.assertEqual(
                by_stmt["Quando total > 1000, o pedido e aprovado com HTTP 200."]["epistemic"],
                "disputed",
                "ACEITE #1a falhou: statement deve ser DISPUTED"
            )

            # Assertion 1b: "devolve 409" -> SUPPORTED
            self.assertEqual(
                by_stmt["Quando total > 1000 devolve 409"]["epistemic"],
                "supported",
                "ACEITE #1b falhou: statement deve ser SUPPORTED"
            )

            # Assertion 1c: "notifica o gerente" -> INFERRED ou UNRESOLVED (nunca supported)
            self.assertIn(
                by_stmt["Quando total > 1000 e o sistema notifica o gerente"]["epistemic"],
                ("inferred", "unresolved"),
                "ACEITE #1c falhou: statement deve ser INFERRED ou UNRESOLVED"
            )

            # Assertion 1b': nature implemented só em supported
            self.assertEqual(
                by_stmt["Quando total > 1000 devolve 409"]["nature"],
                "implemented",
                "ACEITE #1b' falhou: nature do supported deve ser implemented"
            )
            self.assertNotEqual(
                by_stmt["Quando total > 1000, o pedido e aprovado com HTTP 200."]["nature"],
                "implemented",
                "ACEITE #1b' falhou: disputed não deve ter nature=implemented"
            )
        finally:
            repo1.close()


class AchadoFindingFourTest(unittest.TestCase):
    """ACHADO #4: id explicito no nome vira ALIAS canonico."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="onda10a-4-")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_find_by_alias_resolves_to_entity(self):
        """find_by_alias(ns,'RN-023') acha a entidade criada com alias."""
        repo_a = write_repo(self.tmpdir, "repo_a", RULES_A)
        snap_a = capture(repo_a)
        extr_a = extraction_of(snap_a)

        afirmacoes = [
            {
                "regra": "RN-023 Limite de aprovacao",
                "statement": "Quando total > 1000 devolve 409",
                "evidence_refs": evidence(),
            },
        ]

        db = os.path.join(self.tmpdir, "knowledge.db")
        repo = Repository.open(db)
        try:
            out = worker_output("obj-a", "cap-a", afirmacoes)
            res = accepted("obj-a", "cap-a", out, {"snapshot_id": snap_a.snapshot_id, "source_version_ids": []})
            rep = I.integrate(
                repo,
                None,
                snap_a,
                extr_a,
                "code/repo-a",
                objectives=[objective_dict("obj-a", "cap-a", "Aprovacao de pedido")],
                results=[res],
            )

            # ACEITE #4a: find_by_alias acha a entidade
            ent_rn023 = id_mod.find_by_alias(repo, "code/repo-a", "RN-023")
            self.assertIsNotNone(ent_rn023, "ACEITE #4a falhou: find_by_alias('RN-023') deve achar entidade")

            # ACEITE #4b: alias registrado com tipo business_rule
            ent_typed = id_mod.find_by_alias(repo, "code/repo-a", "RN-023", EntityType.BUSINESS_RULE)
            self.assertEqual(ent_typed, ent_rn023, "ACEITE #4b falhou: alias deve estar registrado como business_rule")

            # ACEITE #4c: alias isolado por namespace
            ent_other_ns = id_mod.find_by_alias(repo, "code/outro", "RN-023")
            self.assertIsNone(ent_other_ns, "ACEITE #4c falhou: alias deve estar isolado por namespace")
        finally:
            repo.close()

    def test_reintegration_reuses_entity(self):
        """Reintegracao identica REUSA a entidade (criacao repetida nao gera mudanca)."""
        repo_a = write_repo(self.tmpdir, "repo_a", RULES_A)
        snap_a = capture(repo_a)
        extr_a = extraction_of(snap_a)

        afirmacoes = [
            {
                "regra": "RN-023 Limite de aprovacao",
                "statement": "Quando total > 1000 devolve 409",
                "evidence_refs": evidence(),
            },
        ]

        db = os.path.join(self.tmpdir, "knowledge.db")
        repo = Repository.open(db)
        try:
            out = worker_output("obj-a", "cap-a", afirmacoes)
            res = accepted("obj-a", "cap-a", out, {"snapshot_id": snap_a.snapshot_id, "source_version_ids": []})

            # Primeira integracao
            rep1 = I.integrate(
                repo,
                None,
                snap_a,
                extr_a,
                "code/repo-a",
                objectives=[objective_dict("obj-a", "cap-a", "Aprovacao de pedido")],
                results=[res],
            )
            ent_first = id_mod.find_by_alias(repo, "code/repo-a", "RN-023")

            # Segunda integracao (reintegracao identica)
            rep1b = I.integrate(
                repo,
                None,
                snap_a,
                extr_a,
                "code/repo-a",
                objectives=[objective_dict("obj-a", "cap-a", "Aprovacao de pedido")],
                results=[res],
                reason="reintegracao identica",
            )
            ent_second = id_mod.find_by_alias(repo, "code/repo-a", "RN-023")

            # ACEITE #4d: criacao repetida REUSA a entidade
            self.assertEqual(ent_second, ent_first, "ACEITE #4d falhou: reintegracao deve reusar entidade")

            # ACEITE #4e: reintegracao identica nao gera mudanca
            self.assertEqual(
                rep1b.to_dict()["mudancas"],
                0,
                "ACEITE #4e falhou: reintegracao identica deve ter 0 mudancas"
            )
        finally:
            repo.close()


class AchadoFindingFiveTest(unittest.TestCase):
    """ACHADO #5: dois repositorios no mesmo store."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="onda10a-5-")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_two_repos_in_same_store(self):
        """Integracao de B em store com tarefas de A — SO o objetivo de B gravado."""
        ra = write_repo(self.tmpdir, "repo_a", RULES_A)
        rb = write_repo(self.tmpdir, "repo_b", RULES_B)
        snap_A, snap_B = capture(ra), capture(rb)
        extr_B = extraction_of(snap_B)
        sha_a = {f.path: f.sha256 for f in snap_A.files}["rules.py"]
        sha_b = {f.path: f.sha256 for f in snap_B.files}["rules.py"]

        store_path = os.path.join(self.tmpdir, "runtime.db")
        store = rt_tasks.TaskStore.open(store_path)
        try:
            out_A = worker_output(
                "obj-a",
                "cap-a",
                [
                    {
                        "regra": "RN-023 Limite de aprovacao",
                        "statement": "Quando total > 1000 devolve 409",
                        "evidence_refs": evidence(),
                    }
                ],
            )
            out_B = worker_output(
                "obj-b",
                "cap-b",
                [
                    {
                        "regra": "RN-050 Desconto por quantidade",
                        "statement": "Quando qty > 10 devolve 15",
                        "evidence_refs": evidence("rules.py", 1, 4),
                    }
                ],
            )
            for oid, cid, out, sha in (("obj-a", "cap-a", out_A, sha_a), ("obj-b", "cap-b", out_B, sha_b)):
                task = store.create_task(
                    rt_tasks.TaskKind.INVESTIGATION,
                    {"objective_id": oid, "capability_id": cid},
                    {"snapshot_id": f"scope:{oid}", "source_version_ids": [f"rules.py@{sha}"]},
                )
                store.conn.execute(
                    "UPDATE tasks SET state='done', result_json=?, termination_reason='completed' "
                    "WHERE task_id=?",
                    (rt_tasks.canonical_json(out), task.task_id),
                )
            store.conn.commit()

            db5 = os.path.join(self.tmpdir, "knowledge.db")
            repo5 = Repository.open(db5)
            try:
                rep5 = I.integrate(
                    repo5,
                    store,
                    snap_B,
                    extr_B,
                    "code/repo-b",
                    objectives=[objective_dict("obj-b", "cap-b", "Desconto")],
                )
                d5 = rep5.to_dict()

                # ACEITE #5a: integracao de B grava SO o objetivo de B
                self.assertEqual(
                    [o["objective_id"] for o in d5["objetivos"]],
                    ["obj-b"],
                    "ACEITE #5a falhou: SO o objetivo de B deve estar gravado"
                )

                # ACEITE #5b: resultado de A aparece em descartados com motivo
                self.assertTrue(
                    any(x["objective_id"] == "obj-a" and x["motivo"] for x in d5["descartados"]),
                    "ACEITE #5b falhou: resultado de A deve aparecer em descartados com motivo"
                )

                # ACEITE #5c: nenhuma entidade fora do namespace de B
                ns_b = "code/repo-b"
                cross = [
                    e for e in repo5.conn.execute("SELECT entity_id, namespace FROM entities").fetchall()
                    if e[1] != ns_b
                ]
                self.assertEqual(
                    len(cross),
                    0,
                    f"ACEITE #5c falhou: entidades fora do namespace: {cross}"
                )

                # ACEITE #5d: todo fato no namespace de B
                facts_ns = repo5.conn.execute("SELECT DISTINCT namespace FROM facts").fetchall()
                self.assertEqual(
                    [r[0] for r in facts_ns],
                    [ns_b],
                    "ACEITE #5d falhou: fatos devem estar isolados no namespace de B"
                )
            finally:
                repo5.close()
        finally:
            store.close()

    def test_integrate_without_objectives_is_blocked(self):
        """Integracao sem objectives e BLOQUEADA."""
        rb = write_repo(self.tmpdir, "repo_b", RULES_B)
        snap_B = capture(rb)
        extr_B = extraction_of(snap_B)

        db5 = os.path.join(self.tmpdir, "knowledge.db")
        repo5 = Repository.open(db5)
        try:
            rep5b = I.integrate(repo5, None, snap_B, extr_B, "code/repo-b")
            d5b = rep5b.to_dict()

            self.assertEqual(
                d5b["objetivos"],
                [],
                "Integracao sem objectives deve ter lista de objetivos vazia"
            )
            self.assertTrue(
                d5b["bloqueios"],
                "Integracao sem objectives deve ter bloqueios"
            )
        finally:
            repo5.close()

    def test_divergent_sha_rejected(self):
        """Resultado com sha divergente do snapshot vai para descartados."""
        rb = write_repo(self.tmpdir, "repo_b", RULES_B)
        snap_B = capture(rb)
        extr_B = extraction_of(snap_B)
        sha_b = {f.path: f.sha256 for f in snap_B.files}["rules.py"]

        db5 = os.path.join(self.tmpdir, "knowledge.db")
        repo5 = Repository.open(db5)
        store_path = os.path.join(self.tmpdir, "runtime.db")
        store = rt_tasks.TaskStore.open(store_path)
        try:
            # Cria tarefa com sha_b no store
            out_B = worker_output(
                "obj-b",
                "cap-b",
                [
                    {
                        "regra": "RN-050 Desconto por quantidade",
                        "statement": "Quando qty > 10 devolve 15",
                        "evidence_refs": evidence("rules.py", 1, 4),
                    }
                ],
            )
            task = store.create_task(
                rt_tasks.TaskKind.INVESTIGATION,
                {"objective_id": "obj-b", "capability_id": "cap-b"},
                {"snapshot_id": f"scope:obj-b", "source_version_ids": [f"rules.py@{sha_b}"]},
            )
            store.conn.execute(
                "UPDATE tasks SET state='done', result_json=?, termination_reason='completed' "
                "WHERE task_id=?",
                (rt_tasks.canonical_json(out_B), task.task_id),
            )
            store.conn.commit()

            # Integra primeira vez (OK)
            rep5a = I.integrate(
                repo5,
                store,
                snap_B,
                extr_B,
                "code/repo-b",
                objectives=[objective_dict("obj-b", "cap-b", "Desconto")],
            )

            # Modifica arquivo e captura novo snapshot
            with open(os.path.join(rb, "rules.py"), "a", encoding="utf-8") as fh:
                fh.write("\n# mudou\n")
            snap_B2 = capture(rb)
            extr_B2 = extraction_of(snap_B2)

            # Tenta integrar com novo snapshot (sha divergente)
            rep5c = I.integrate(
                repo5,
                store,
                snap_B2,
                extr_B2,
                "code/repo-b",
                objectives=[objective_dict("obj-b", "cap-b", "Desconto")],
            )
            d5c = rep5c.to_dict()

            # Deve haver rejeicao por sha divergente
            self.assertTrue(
                any("sha divergente" in x["motivo"] for x in d5c["descartados"]),
                "ACEITE falhou: entrada com sha divergente deve ser descartada"
            )
        finally:
            repo5.close()
            store.close()


class AchadoFindingThreeTest(unittest.TestCase):
    """ACHADO #3: reading_satisfied so fecha obrigacao COM evidencia."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="onda10a-3-")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_reading_satisfied_with_evidence_closes_obligation(self):
        """Obrigacao COM evidencia sai de unmet."""
        r3 = write_repo(self.tmpdir, "repo_a", RULES_A)
        snap3 = capture(r3)
        extr3 = extraction_of(snap3)

        needs = [
            {
                "need_id": "need-1",
                "kind": "symbol",
                "target": "approve_order",
                "motivo": "comportamento do limite",
                "trigger": "predicate",
            },
            {"need_id": "need-2", "kind": "symbol", "target": "ConflictError", "motivo": "excecao levantada", "trigger": "predicate"},
        ]
        obj3 = objective_dict("obj-c", "cap-c", "Aprovacao", needs)
        base_afirm = [
            {
                "regra": "RN-030 Limite",
                "statement": "Quando total > 1000 devolve 409",
                "evidence_refs": evidence(),
            }
        ]

        db3 = os.path.join(self.tmpdir, "knowledge.db")
        repo3 = Repository.open(db3)
        try:
            out3 = worker_output(
                "obj-c",
                "cap-c",
                base_afirm,
                {
                    "reading_satisfied": [
                        {"need_id": "need-1", "evidence_refs": evidence("rules.py", 5, 8)},
                        {"need_id": "need-2"},
                    ],
                },
            )
            res3 = accepted("obj-c", "cap-c", out3, {"snapshot_id": snap3.snapshot_id, "source_version_ids": []})
            rep3 = I.integrate(
                repo3, None, snap3, extr3, "code/repo-c", objectives=[obj3], results=[res3],
            )
            d3 = rep3.to_dict()["objetivos"][0]

            unmet_txt = " ".join(d3["unmet"])

            # ACEITE #3a: obrigacao COM evidencia sai de unmet
            self.assertNotIn(
                "approve_order",
                unmet_txt,
                "ACEITE #3a falhou: obrigacao COM evidencia deve sair de unmet"
            )

            # ACEITE #3b: obrigacao SEM evidencia permanece
            self.assertIn(
                "ConflictError",
                unmet_txt,
                "ACEITE #3b falhou: obrigacao SEM evidencia deve permanecer em unmet"
            )
        finally:
            repo3.close()

    def test_reading_satisfied_with_wrong_hash_does_not_close(self):
        """Evidencia com hash que nao bate NAO fecha obrigacao."""
        r3 = write_repo(self.tmpdir, "repo_a", RULES_A)
        snap3 = capture(r3)
        extr3 = extraction_of(snap3)

        needs = [
            {
                "need_id": "need-1",
                "kind": "symbol",
                "target": "approve_order",
                "motivo": "comportamento do limite",
                "trigger": "predicate",
            },
        ]
        obj3 = objective_dict("obj-c", "cap-c", "Aprovacao", needs)
        base_afirm = [
            {
                "regra": "RN-030 Limite",
                "statement": "Quando total > 1000 devolve 409",
                "evidence_refs": evidence(),
            }
        ]

        db3 = os.path.join(self.tmpdir, "knowledge.db")
        repo3 = Repository.open(db3)
        try:
            out3b = worker_output(
                "obj-c",
                "cap-c",
                base_afirm,
                {
                    "reading_satisfied": [
                        {
                            "need_id": "need-1",
                            "evidence_refs": [
                                {
                                    "path": "rules.py",
                                    "line_start": 5,
                                    "line_end": 8,
                                    "snippet_hash": "0" * 64,
                                }
                            ],
                        },
                    ],
                },
            )
            res3b = accepted("obj-c", "cap-c", out3b, {"snapshot_id": snap3.snapshot_id, "source_version_ids": []}, "t2")
            rep3b = I.integrate(
                repo3, None, snap3, extr3, "code/repo-c", objectives=[obj3], results=[res3b],
            )
            d3b = rep3b.to_dict()["objetivos"][0]

            unmet = " ".join(d3b["unmet"])

            # ACEITE #3c: evidencia com hash que nao bate NAO fecha obrigacao
            self.assertIn(
                "approve_order",
                unmet,
                "ACEITE #3c falhou: hash divergente deve nao fechar obrigacao"
            )
        finally:
            repo3.close()

    def test_claim_outside_snapshot_rejected(self):
        """Claim com citacao fora do snapshot e rejeitado com motivo."""
        r3 = write_repo(self.tmpdir, "repo_a", RULES_A)
        snap3 = capture(r3)
        extr3 = extraction_of(snap3)

        needs = [
            {
                "need_id": "need-1",
                "kind": "symbol",
                "target": "approve_order",
                "motivo": "comportamento do limite",
                "trigger": "predicate",
            },
        ]
        obj3 = objective_dict("obj-c", "cap-c", "Aprovacao", needs)

        db3 = os.path.join(self.tmpdir, "knowledge.db")
        repo3 = Repository.open(db3)
        try:
            out3c = worker_output(
                "obj-c",
                "cap-c",
                [
                    {
                        "regra": "RN-031 Regra do outro repo",
                        "statement": "Quando total > 1000 devolve 409",
                        "evidence_refs": evidence("../repo_z/outra.py", 1, 3),
                    },
                ],
            )
            res3c = accepted("obj-c", "cap-c", out3c, {"snapshot_id": snap3.snapshot_id, "source_version_ids": []}, "t3")
            rep3c = I.integrate(
                repo3, None, snap3, extr3, "code/repo-c", objectives=[obj3], results=[res3c],
            )
            d3c = rep3c.to_dict()["objetivos"][0]

            # Deve haver rejeicao e nao deve gravar fatos
            self.assertTrue(
                d3c["rejeitados"],
                "ACEITE falhou: citacao fora do snapshot deve ser rejeitada"
            )
            self.assertEqual(
                d3c["fatos_gravados"],
                0,
                "ACEITE falhou: rejeitacao deve impedir gravacao de fatos"
            )
        finally:
            repo3.close()


if __name__ == "__main__":
    unittest.main()
