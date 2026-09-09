"""Pacote de contexto de objetivo de DESCOBERTA (módulo sem extrator).

O que estes testes provam, sempre pelo caminho real (`context.build_package`
com o resolvedor de `analysis.snapshot`, nunca um stub que devolva texto
inventado):

| Aceite                                                            | Teste |
|-------------------------------------------------------------------|-------|
| `reading_need` com `trigger=source_file` empacota o ARQUIVO INTEIRO | `test_repo_go_com_dois_arquivos_*` |
| Faixa descoberta pelo resolvedor quando a obrigação só traz o alvo | `test_source_file_sem_localizador_*` |
| Não coube ⇒ particiona por FAIXA DE LINHAS e deixa `remaining_needs` | `test_orcamento_apertado_*` |
| Faixa adiada nunca vira ponteiro órfão no pacote                    | `test_orcamento_apertado_marca_ref_adiada` |
| `analysis_directives` chega ao worker (pacote e envelope)           | `AnalysisDirectivesTest` |

`trigger` viaja como STRING (`"source_file"`) porque `build_package` opera
sobre o `dict` do objetivo — o valor vem de `analysis.investigation
.ReadingTrigger.SOURCE_FILE`, mas este módulo nunca importa a enum.
"""

import os
import shutil
import tempfile
import unittest

from analysis.snapshot import capture, resolve_evidence
from runtime import context as CTX
from runtime import coordinator as C
from runtime import tasks as T


GO_MAIN = """package main

import "fmt"

func main() {
\tfmt.Println(Total(3, 4))
}
"""

GO_CALC = """package main

// Total soma dois valores.
func Total(a int, b int) int {
\tif a < 0 || b < 0 {
\t\tpanic("negativo")
\t}
\treturn a + b
}
"""


def _write_repo(root: str, files: dict) -> None:
    for name, content in files.items():
        full = os.path.join(root, name)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8", newline="") as fh:
            fh.write(content)


class SourceFilePackageTest(unittest.TestCase):
    """Objetivo de descoberta: `reading_needs` são ARQUIVOS INTEIROS."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="descoberta-ctx-")
        self.repo = os.path.join(self.tmpdir, "go-repo")
        os.makedirs(self.repo, exist_ok=True)
        _write_repo(self.repo, {"main.go": GO_MAIN, "calc.go": GO_CALC})
        self.snapshot = capture(self.repo)
        self.calls = []

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _resolver(self, path, start, end):
        self.calls.append((path, start, end))
        return resolve_evidence(self.snapshot, path, start, end)

    def _objective(self, *, com_localizador: bool):
        needs = []
        for idx, name in enumerate(("main.go", "calc.go")):
            need = {
                "need_id": f"need-{name}",
                "kind": "range",
                "target": name,
                "motivo": "modulo sem extrator: leitura integral para descoberta",
                "trigger": "source_file",
                "priority": 10 + idx,
                "satisfied": False,
                "waived_reason": "",
            }
            if com_localizador:
                total = len(open(os.path.join(self.repo, name), encoding="utf-8").read().splitlines())
                need["evidence"] = {"path": name, "line_start": 1, "line_end": total}
            needs.append(need)
        return {
            "objective_id": "obj-descoberta",
            "kind": "discovery",
            "capability_id": "mod:go-repo",
            "name": "Descoberta do modulo go-repo",
            "contract": {},
            "evidence_refs": [],
            "reading_needs": needs,
            "state": "partial",
        }

    def _conteudo(self, name):
        with open(os.path.join(self.repo, name), encoding="utf-8") as fh:
            return fh.read()

    # -- arquivo inteiro ----------------------------------------------------

    def test_repo_go_com_dois_arquivos_gera_pacote_com_os_dois_conteudos(self):
        """ACEITE: 2 arquivos Go ⇒ pacote com o conteúdo dos 2, integral."""
        package = CTX.build_package(
            self._objective(com_localizador=True),
            CTX.Budget(max_bytes=200_000, max_tokens=100_000),
            self._resolver,
        )
        por_path = {p.path: p for p in package.parts}
        self.assertEqual(sorted(por_path), ["calc.go", "main.go"])
        for name in ("main.go", "calc.go"):
            self.assertEqual(
                por_path[name].snippet,
                self._conteudo(name),
                f"{name} deveria vir INTEIRO no pacote",
            )
            self.assertFalse(por_path[name].truncated, f"{name} não deveria vir cortado")
        self.assertEqual(package.remaining_needs, [], "tudo coube: nada a continuar")

    def test_source_file_sem_localizador_descobre_a_faixa_no_resolvedor(self):
        """Obrigação só com o ALVO: o fim do arquivo sai do próprio resolvedor."""
        package = CTX.build_package(
            self._objective(com_localizador=False),
            CTX.Budget(max_bytes=200_000, max_tokens=100_000),
            self._resolver,
        )
        por_path = {p.path: p for p in package.parts}
        self.assertEqual(sorted(por_path), ["calc.go", "main.go"])
        for name in ("main.go", "calc.go"):
            esperado = self._conteudo(name)
            self.assertEqual(por_path[name].snippet, esperado)
            self.assertEqual(por_path[name].line_start, 1)
            self.assertEqual(
                por_path[name].line_end,
                len(esperado.splitlines()),
                "a faixa sondada tem de ser exatamente o arquivo",
            )

    def test_need_source_file_satisfeita_nao_entra_no_pacote(self):
        """Leitura já satisfeita não volta a custar orçamento."""
        objective = self._objective(com_localizador=True)
        objective["reading_needs"][0]["satisfied"] = True
        objective["reading_needs"][0]["satisfied_note"] = "lido na rodada 1"
        package = CTX.build_package(
            objective, CTX.Budget(max_bytes=200_000, max_tokens=100_000), self._resolver
        )
        self.assertEqual([p.path for p in package.parts], ["calc.go"])

    def test_source_file_fora_do_snapshot_vira_motivo_e_nao_trecho(self):
        """Alvo inexistente: `unavailable_reason`, nunca trecho fabricado."""
        objective = self._objective(com_localizador=False)
        objective["reading_needs"] = [
            {
                "need_id": "need-fantasma",
                "kind": "range",
                "target": "nao-existe.go",
                "motivo": "modulo sem extrator",
                "trigger": "source_file",
                "satisfied": False,
                "waived_reason": "",
            }
        ]
        package = CTX.build_package(
            objective, CTX.Budget(max_bytes=200_000, max_tokens=100_000), self._resolver
        )
        self.assertEqual(package.parts, [])
        ref = [r for r in package.refs if r.get("ref_id") == "need-fantasma"][0]
        self.assertIsNone(ref["part_id"])
        self.assertTrue(ref.get("unavailable_reason"))

    # -- partição por orçamento --------------------------------------------

    def _teto_apertado(self):
        """Teto medido: cabe tudo MENOS metade do segundo arquivo (calc.go).

        Derivado do pacote real (nunca de um número mágico), para que o teste
        continue exercendo a PARTIÇÃO — e não virar um `BudgetExceeded` — se o
        formato do payload mudar.
        """
        inteiro = CTX.build_package(
            self._objective(com_localizador=True),
            CTX.Budget(max_bytes=10_000_000, max_tokens=10_000_000),
            self._resolver,
        )
        return inteiro.payload_bytes - len(GO_CALC) // 2

    def _pacote_apertado(self):
        return CTX.build_package(
            self._objective(com_localizador=True),
            CTX.Budget(max_bytes=self._teto_apertado(), max_tokens=10_000_000),
            self._resolver,
        )

    def test_orcamento_apertado_particiona_por_faixa_de_linhas(self):
        """Não coube ⇒ faixa restante em `remaining_needs`, sem BudgetExceeded."""
        package = self._pacote_apertado()
        self.assertTrue(package.remaining_needs, "o que não coube tem de sobrar declarado")
        for need in package.remaining_needs:
            self.assertEqual(need["trigger"], "source_file")
            ev = need["evidence"][0]
            self.assertGreaterEqual(ev["line_start"], 1)
            self.assertGreaterEqual(ev["line_end"], ev["line_start"])
            self.assertIn(ev["path"], ("main.go", "calc.go"))
        # Prioridade 10 (main.go) vem antes: é calc.go que é cortado.
        alvos = {n["target"] for n in package.remaining_needs}
        self.assertEqual(alvos, {"calc.go"})
        entregues = {p.path for p in package.parts}
        self.assertIn("main.go", entregues, "o de maior prioridade vai inteiro")
        self.assertLessEqual(
            package.payload_bytes,
            self._teto_apertado(),
            "o pacote entregue precisa caber no teto, sem exceção",
        )

    def test_orcamento_apertado_nao_perde_nenhuma_linha(self):
        """Faixa entregue + faixa restante = arquivo inteiro (nada some)."""
        package = self._pacote_apertado()
        entregues = {}
        for part in package.parts:
            entregues.setdefault(part.path, []).append((part.line_start, part.line_end))
        restantes = {}
        for need in package.remaining_needs:
            ev = need["evidence"][0]
            restantes.setdefault(ev["path"], []).append((ev["line_start"], ev["line_end"]))
        for name in ("main.go", "calc.go"):
            faixas = sorted(entregues.get(name, []) + restantes.get(name, []))
            total = len(self._conteudo(name).splitlines())
            self.assertEqual(faixas[0][0], 1, f"{name}: cobertura começa na linha 1")
            self.assertEqual(faixas[-1][1], total, f"{name}: cobertura termina no fim")
            for anterior, seguinte in zip(faixas, faixas[1:]):
                self.assertEqual(
                    seguinte[0],
                    anterior[1] + 1,
                    f"{name}: faixas contíguas, sem buraco nem sobreposição",
                )

    def test_orcamento_apertado_marca_ref_adiada(self):
        """Referência sem trecho neste pacote não aponta `part_id` órfão."""
        package = self._pacote_apertado()
        ids = {p.part_id for p in package.parts}
        for ref in package.refs:
            pid = ref.get("part_id")
            if pid is None:
                continue
            self.assertIn(pid, ids, "todo part_id citado precisa viajar no pacote")
        adiadas = [
            r for r in package.refs if r.get("part_id") is None and r.get("unavailable_reason")
        ]
        self.assertTrue(adiadas, "a leitura adiada precisa dizer POR QUE não veio")

    def test_particao_e_deterministica(self):
        """Mesma entrada + mesmo teto ⇒ mesma partição (identidade estável)."""
        a = self._pacote_apertado()
        b = self._pacote_apertado()
        self.assertEqual(a.remaining_needs, b.remaining_needs)
        self.assertEqual(
            [(p.path, p.line_start, p.line_end) for p in a.parts],
            [(p.path, p.line_start, p.line_end) for p in b.parts],
        )

    def test_remaining_needs_viaja_em_to_json(self):
        """O CLI/coordenador lê `remaining_needs` do pacote serializado."""
        package = self._pacote_apertado()
        self.assertEqual(package.to_json()["remaining_needs"], package.remaining_needs)


class AnalysisDirectivesTest(unittest.TestCase):
    """`analysis_directives` do objetivo chega ao worker por dois caminhos."""

    DIRETIVAS = {
        "profundidade": "apurar regras, invariantes e edge cases mesmo NÃO declarados",
        "evidencia": "somente código executável; README/comentário/docstring não sustentam",
    }

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="descoberta-dir-")
        self.repo = os.path.join(self.tmpdir, "go-repo")
        os.makedirs(self.repo, exist_ok=True)
        _write_repo(self.repo, {"calc.go": GO_CALC})
        self.snapshot = capture(self.repo)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _objective(self):
        return {
            "objective_id": "obj-descoberta",
            "kind": "discovery",
            "capability_id": "mod:go-repo",
            "name": "Descoberta",
            "contract": {},
            "analysis_directives": dict(self.DIRETIVAS),
            "reading_needs": [
                {
                    "need_id": "need-calc",
                    "kind": "range",
                    "target": "calc.go",
                    "motivo": "modulo sem extrator",
                    "trigger": "source_file",
                    "satisfied": False,
                    "waived_reason": "",
                }
            ],
            "state": "partial",
        }

    def test_pacote_carrega_analysis_directives_no_envelope_do_objetivo(self):
        package = CTX.build_package(
            self._objective(),
            CTX.Budget(max_bytes=200_000, max_tokens=100_000),
            lambda p, s, e: resolve_evidence(self.snapshot, p, s, e),
        )
        envelope = package.refs[0]
        self.assertEqual(envelope["kind"], "objective")
        self.assertEqual(envelope["analysis_directives"], self.DIRETIVAS)

    def test_objetivo_sem_diretivas_nao_ganha_chave_nula(self):
        objective = self._objective()
        objective.pop("analysis_directives")
        package = CTX.build_package(
            objective,
            CTX.Budget(max_bytes=200_000, max_tokens=100_000),
            lambda p, s, e: resolve_evidence(self.snapshot, p, s, e),
        )
        self.assertNotIn("analysis_directives", package.refs[0])

    def test_envelope_de_tarefa_preserva_analysis_directives(self):
        """`envelope_for_task` entrega o objetivo COM as diretivas ao adaptador."""
        db = os.path.join(self.tmpdir, "runtime.db")
        store = T.TaskStore.open(db)
        try:
            task = store.create_task(
                T.TaskKind.INVESTIGATION,
                self._objective(),
                {"snapshot_id": self.snapshot.snapshot_id, "source_version_ids": []},
            )
            envelope = C.envelope_for_task(task, execution_id="exec-1")
            self.assertEqual(
                envelope.objective["analysis_directives"], self.DIRETIVAS
            )
            # Schema FECHADO do §10.4.4 continua intacto: as diretivas viajam
            # DENTRO de `objective`, nunca como campo novo de topo.
            self.assertEqual(
                envelope.to_dict()["objective"]["analysis_directives"], self.DIRETIVAS
            )
        finally:
            store.close()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
