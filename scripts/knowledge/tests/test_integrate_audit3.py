"""Testes permanentes do achado BLOQUEANTE nº1 da 3ª auditoria (Onda11-T1).

Buraco fechado: condição e consequência em RAMOS DIFERENTES viravam `supported`.

    def approve(total):
        if total > 1000:
            return {"status": 409}
        return {"status": 200}

O claim "quando total > 1000, retorna HTTP 200" citando a função INTEIRA saía
`supported/implemented` porque o literal `200` ESTAVA no trecho — no ramo errado.
`knowledge.integrate._branch_association` passa a exigir que o efeito esteja
DENTRO do ramo governado pela condição.

Contrato: caminho REAL (`integrate()`), nunca a função interna isolada.
"""

import os
import tempfile
import unittest

from analysis.extractors.base import SourceFile
from analysis.extractors.registry import default_registry
from analysis.snapshot import capture
from analysis.verification import Claim, ClaimEvidence, verify_claim
from knowledge import integrate as I
from knowledge.repository import Repository
from runtime import tasks as rt_tasks


#: A reprodução EXATA da auditoria: condição num ramo, `200` no outro.
REPRO = '''\
def approve(total):
    if total > 1000:
        return {"status": 409}
    return {"status": 200}
'''

#: Mesma comparação FORA de qualquer `if`: não há ramo a que vincular o efeito.
NO_BRANCH = '''\
def approve(total):
    over = total > 1000
    audit(over)
    return {"status": 409}
'''

#: Guard-rail simétrico: duas exceções nomeadas, uma em cada caminho.
TWO_RAISES = '''\
class ConflictError(Exception):
    pass


class ValidationError(Exception):
    pass


def approve(total):
    if total > 1000:
        raise ConflictError("limite")
    raise ValidationError("baixo")
'''

#: Mesma lógica em JavaScript: parseável só por heurística.
JS = '''\
function approve(total) {
  if (total > 1000) {
    return {status: 409};
  }
  return {status: 200};
}
'''


def _extraction_of(snapshot):
    registry = default_registry()
    files = []
    for entry in snapshot.files:
        with open(os.path.join(snapshot.repo, entry.path), "r", encoding="utf-8") as fh:
            files.append(SourceFile(path=entry.path, content=fh.read()))
    return registry.extract_all(files)


def _objective_dict(objective_id="obj-1", capability_id="cap-1", name="Aprovacao"):
    from analysis.investigation import CONTRACT_FIELDS, CONTRACT_LABELS, ContractField

    return {
        "objective_id": objective_id,
        "kind": "capability",
        "capability_id": capability_id,
        "name": name,
        "contract": {
            n: ContractField(name=n, label=CONTRACT_LABELS[n]).to_dict() for n in CONTRACT_FIELDS
        },
        "reading_needs": [],
        "state": "partial",
    }


class BranchAssociationTest(unittest.TestCase):
    """Condição -> ramo -> efeito, pelo caminho real de integração."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="onda11t1-")

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # -- infraestrutura -----------------------------------------------------

    def _integrate(self, filename, content, statement, start, end):
        """(epistemic, nature, escopo) do claim, gravados por `integrate()`."""
        repo_dir = os.path.join(self.tmpdir, "repo-" + str(abs(hash(statement)) % 10**8))
        os.makedirs(repo_dir, exist_ok=True)
        with open(os.path.join(repo_dir, filename), "w", encoding="utf-8") as fh:
            fh.write(content)
        snapshot = capture(repo_dir)
        extraction = _extraction_of(snapshot)
        afirmacao = {
            "regra": "RN-900 Limite de aprovacao",
            "statement": statement,
            "evidence_refs": [{"path": filename, "line_start": start, "line_end": end}],
        }
        output = {
            "objective_id": "obj-1",
            "capability_id": "cap-1",
            "contract": {"decisoes": {"status": "filled", "afirmacoes": [afirmacao]}},
        }
        versions = {"snapshot_id": snapshot.snapshot_id, "source_version_ids": []}
        result = I.AcceptedResult(
            task_id="t1",
            kind="investigation",
            objective_id="obj-1",
            capability_id="cap-1",
            execution_id="exec-1",
            input_versions=versions,
            input_versions_hash=rt_tasks.input_versions_hash(versions),
            output=output,
            objective_payload={"objective_id": "obj-1", "capability_id": "cap-1"},
            asserted_by="llm:worker",
            created_at="2026-09-07T00:00:00Z",
        )
        repo = Repository.open(os.path.join(repo_dir, "knowledge.db"))
        try:
            report = I.integrate(
                repo,
                None,
                snapshot,
                extraction,
                "code/repro",
                objectives=[_objective_dict()],
                results=[result],
            )
            fatos = [f for obj in report.to_dict()["objetivos"] for f in obj["fatos"]]
            self.assertEqual(len(fatos), 1, f"esperado 1 fato gravado, veio {fatos}")
            fato = fatos[0]
            return fato["epistemic"], fato.get("nature"), fato.get("escopo_sustentado", "")
        finally:
            repo.close()

    def _association(self, filename, content, statement, start, end):
        """Veredito CRU da associação, para provar o mecanismo (não só o efeito)."""
        repo_dir = os.path.join(self.tmpdir, "assoc-" + str(abs(hash(statement)) % 10**8))
        os.makedirs(repo_dir, exist_ok=True)
        with open(os.path.join(repo_dir, filename), "w", encoding="utf-8") as fh:
            fh.write(content)
        snapshot = capture(repo_dir)
        extraction = _extraction_of(snapshot)
        fields, consequence = I._statement_parse(statement)
        claim = Claim(
            claim_id="c1",
            subject="RN-900",
            predicate_kind="behavior",
            statement=statement,
            statement_fields=I._derive_statement_fields(statement, "decisoes"),
            asserted_by="llm:worker",
            evidence_refs=(ClaimEvidence(path=filename, start_line=start, end_line=end),),
        )
        verdict = verify_claim(claim, snapshot, extraction)
        condition = I._condition_of(fields)
        self.assertIsNotNone(condition, "condição do enunciado deveria ser mecânica")
        return I._branch_association(consequence, condition, verdict)

    # -- aceites ------------------------------------------------------------

    def test_repro_exata_condicao_e_efeito_em_ramos_diferentes(self):
        """ACEITE 1: '>1000 retorna 200' citando a função INTEIRA -> DISPUTED."""
        epistemic, nature, escopo = self._integrate(
            "rules.py", REPRO, "Quando total > 1000, o pedido retorna HTTP 200.", 1, 4
        )
        self.assertEqual(
            epistemic,
            "disputed",
            "ACEITE 1 falhou: efeito no ramo ERRADO nunca pode sustentar a frase",
        )
        self.assertNotEqual(nature, "implemented", "ACEITE 1 falhou: disputed não é implemented")

        assoc, motivo = self._association(
            "rules.py", REPRO, "Quando total > 1000, o pedido retorna HTTP 200.", 1, 4
        )
        self.assertEqual(assoc, I._ASSOC_CONTRARY, f"associação deveria ser contrária: {motivo}")
        self.assertIn("409", motivo, "o motivo deve citar o que o ramo REALMENTE faz")

    def test_efeito_no_ramo_certo_continua_supported(self):
        """ACEITE 2: '>1000 retorna 409' citando a função INTEIRA -> SUPPORTED."""
        epistemic, nature, _ = self._integrate(
            "rules.py", REPRO, "Quando total > 1000, o pedido retorna HTTP 409.", 1, 4
        )
        self.assertEqual(epistemic, "supported", "ACEITE 2 falhou: efeito está no ramo certo")
        self.assertEqual(nature, "implemented", "ACEITE 2 falhou: supported executável é implemented")

        assoc, _ = self._association(
            "rules.py", REPRO, "Quando total > 1000, o pedido retorna HTTP 409.", 1, 4
        )
        self.assertEqual(assoc, I._ASSOC_INSIDE)

    def test_condicao_complementar_vincula_ao_fallthrough(self):
        """ACEITE 3: '<=1000 retorna 200' -> vínculo mecânico com o fallthrough.

        A associação RESOLVE (o `return` complementar é o fallthrough imediato do
        mesmo `if`, cujo corpo sempre retorna). O veredito final continua vindo do
        eixo de CONDIÇÃO em `analysis.verification`, que trata operador oposto no
        trecho como divergência — o guard nunca promove nada.
        """
        assoc, motivo = self._association(
            "rules.py", REPRO, "Quando total <= 1000, o pedido retorna HTTP 200.", 1, 4
        )
        self.assertEqual(
            assoc,
            I._ASSOC_COMPLEMENT,
            f"ACEITE 3 falhou: fallthrough imediato deveria ser vinculável ({motivo})",
        )
        epistemic, _, _ = self._integrate(
            "rules.py", REPRO, "Quando total <= 1000, o pedido retorna HTTP 200.", 1, 4
        )
        self.assertNotEqual(epistemic, "inferred_by_branch_guard_promotion", "guard nunca promove")
        self.assertIn(epistemic, ("disputed", "supported", "inferred"))

    def test_condicao_sem_ramo_nunca_e_supported(self):
        """ACEITE 4: comparação fora de `if` -> vínculo indeterminado -> nunca supported."""
        statement = "Quando total > 1000, o pedido retorna HTTP 409."
        assoc, motivo = self._association("rules.py", NO_BRANCH, statement, 1, 4)
        self.assertEqual(assoc, I._ASSOC_UNDETERMINED, motivo)

        epistemic, nature, escopo = self._integrate("rules.py", NO_BRANCH, statement, 1, 4)
        self.assertNotEqual(
            epistemic, "supported", "ACEITE 4 falhou: sem ramo não há vínculo condição->efeito"
        )
        self.assertEqual(epistemic, "inferred")
        self.assertNotEqual(nature, "implemented")
        self.assertIn("não estabelecido", escopo, "o escopo deve dizer que o vínculo falhou")

    def test_trecho_nao_python_tem_teto_inferred(self):
        """ACEITE 5: trecho não parseável como Python -> heurística, nunca supported."""
        statement = "Quando total > 1000, o pedido retorna HTTP 409."
        assoc, motivo = self._association("rules.js", JS, statement, 1, 6)
        self.assertEqual(assoc, I._ASSOC_HEURISTIC, motivo)

        epistemic, nature, _ = self._integrate("rules.js", JS, statement, 1, 6)
        self.assertNotEqual(epistemic, "supported", "ACEITE 5 falhou: heurística não sustenta frase")
        self.assertNotEqual(nature, "implemented")

    def test_excecao_nomeada_segue_a_regra_de_ramo(self):
        """ACEITE 6 (guard-rail simétrico): exceção do OUTRO caminho não sustenta."""
        errada = "Quando total > 1000, o pedido levanta ValidationError."
        assoc, motivo = self._association("rules.py", TWO_RAISES, errada, 9, 12)
        self.assertEqual(assoc, I._ASSOC_CONTRARY, motivo)
        self.assertIn("ConflictError", motivo, "o motivo deve citar a exceção REAL do ramo")

        epistemic, nature, _ = self._integrate("rules.py", TWO_RAISES, errada, 9, 12)
        self.assertNotEqual(
            epistemic, "supported", "ACEITE 6 falhou: exceção do outro caminho não sustenta"
        )
        self.assertNotEqual(nature, "implemented")

        certa = "Quando total > 1000, o pedido levanta ConflictError."
        assoc_ok, _ = self._association("rules.py", TWO_RAISES, certa, 9, 12)
        self.assertEqual(assoc_ok, I._ASSOC_INSIDE, "a exceção DO ramo continua sustentando")
        self.assertEqual(self._integrate("rules.py", TWO_RAISES, certa, 9, 12)[0], "supported")


if __name__ == "__main__":
    unittest.main()
