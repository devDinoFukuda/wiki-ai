from __future__ import annotations

import os
import shutil
import tempfile
import unittest

from codescan.evidence import build_evidence_pack, verify_markdown
from codescan.surface import scan, to_dict

GREEN = "\U0001F7E2"
YELLOW = "\U0001F7E1"
RED = "\U0001F534"


def _write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


class EvidencePackTest(unittest.TestCase):
    def setUp(self):
        self.repo = tempfile.mkdtemp(prefix="codescan_repo_")
        _write(
            os.path.join(self.repo, "src", "payments.py"),
            "\n".join(
                [
                    "MAX_RETRIES = 5",
                    "",
                    "def retry_payment(payment_id):",
                    "    if payment_id is None:",
                    "        return False",
                    "    return True",
                    "",
                ]
            ),
        )
        _write(
            os.path.join(self.repo, "src", "quotes", "Quote.java"),
            "\n".join(
                [
                    "class Quote {}",
                    "",
                ]
            ),
        )
        _write(
            os.path.join(self.repo, "src", "orders.py"),
            "\n".join(
                [
                    "def create_order(order_id):",
                    "    return order_id",
                    "",
                ]
            ),
        )

    def tearDown(self):
        shutil.rmtree(self.repo, ignore_errors=True)

    def test_build_evidence_pack_by_topic(self):
        surface = to_dict(scan(self.repo, module_min_files=1))
        pack = build_evidence_pack(
            self.repo,
            surface,
            "payment retry",
            top=5,
            context=1,
            max_lines=8,
        )
        self.assertEqual(pack["schema"], "codescan.evidence.v1")
        self.assertEqual(pack["topic"], "payment retry")
        files = {item["file"] for item in pack["items"]}
        self.assertIn("src/payments.py", files)
        excerpts = "\n".join(item["excerpt"] for item in pack["items"])
        self.assertIn("retry_payment", excerpts)


class VerifyMarkdownTest(unittest.TestCase):
    def setUp(self):
        self.repo = tempfile.mkdtemp(prefix="codescan_verify_")
        _write(
            os.path.join(self.repo, "src", "payments.py"),
            "\n".join(
                [
                    "MAX_RETRIES = 5",
                    "def retry_payment(payment_id):",
                    "    return True",
                    "",
                ]
            ),
        )
        _write(
            os.path.join(self.repo, "src", "quotes", "Quote.java"),
            "\n".join(
                [
                    "class Quote {}",
                    "",
                ]
            ),
        )

    def tearDown(self):
        shutil.rmtree(self.repo, ignore_errors=True)

    def test_verify_accepts_claim_with_existing_citation(self):
        md = (
            f"- A responsabilidade confirmada do fluxo de pagamento é aplicar limite de retry "
            f"antes de retornar sucesso operacional. {GREEN} Evidência: `src/payments.py:1-2`"
        )
        report = verify_markdown(self.repo, md)
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["claims"], 1)
        self.assertEqual(report["citations"], 1)

    def test_verify_rejects_claim_without_citation(self):
        report = verify_markdown(self.repo, f"- Retry usa MAX_RETRIES. {GREEN}")
        self.assertFalse(report["ok"])
        self.assertEqual(report["errors"][0]["rule"], "claim_sem_evidencia")

    def test_verify_rejects_invalid_line(self):
        report = verify_markdown(
            self.repo,
            "- Retry usa MAX_RETRIES. Evidência: `src/payments.py:99`",
        )
        self.assertFalse(report["ok"])
        self.assertTrue(any(e["rule"] == "linha_invalida" for e in report["errors"]))

    def test_verify_rejects_file_outside_repo(self):
        report = verify_markdown(
            self.repo,
            "- Retry usa MAX_RETRIES. Evidência: `../other.py:1`",
        )
        self.assertFalse(report["ok"])
        self.assertTrue(any(e["rule"] in ("fora_do_repo", "sem_citacoes") for e in report["errors"]))

    def test_verify_accepts_green_claim_with_existing_citation(self):
        report = verify_markdown(
            self.repo,
            f"- A responsabilidade do fluxo de pagamento é usar MAX_RETRIES como política "
            f"explícita de tentativa antes de retornar resultado. {GREEN} Evidência: `src/payments.py:1-2`",
        )
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["green_claims"], 1)
        self.assertEqual(report["valid_citations"], 1)

    def test_verify_rejects_claims_without_valid_citations(self):
        # F-03: documento com ≥1 claim-block e ZERO citações válidas → ok=False, rule="sem_evidencia"
        report = verify_markdown(
            self.repo,
            f"- Retry parece usar política padrão. {YELLOW}\n- Falta confirmar SLA. {RED}",
        )
        self.assertFalse(report["ok"])
        self.assertEqual(report["claims"], 2)
        self.assertEqual(report["green_claims"], 0)
        self.assertTrue(any(e["rule"] == "sem_evidencia" for e in report["errors"]))

    def test_verify_allows_yellow_and_red_without_individual_citation(self):
        # F-03: claims 🟡/🔴 não exigem citação individual, mas documento com claims
        # precisa de ao menos uma citação válida para passar.
        report = verify_markdown(
            self.repo,
            f"- Retry parece usar política padrão. {YELLOW}\n"
            f"- Falta confirmar o limite de SLA. {RED}\n"
            f"- A responsabilidade confirmada do fluxo de pagamento é aplicar limite de retry "
            f"antes de retornar sucesso operacional. {GREEN} Evidência: `src/payments.py:1-2`",
        )
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["claims"], 3)
        self.assertEqual(report["green_claims"], 1)
        self.assertEqual(report["valid_citations"], 1)

    def test_verify_rejects_basename_citation_when_full_relative_path_is_required(self):
        report = verify_markdown(
            self.repo,
            f"- A classe de cotação existe no pacote de domínio de quotes e deve ser rastreada "
            f"pelo caminho completo. {GREEN} Evidência: `Quote.java:1`",
        )
        self.assertFalse(report["ok"])
        # basename único agora vira `caminho_parcial` com sugestão do caminho completo
        self.assertTrue(any(e["rule"] == "caminho_parcial" for e in report["errors"]))
        # Validar que o detail contém o caminho sugerido
        error = next((e for e in report["errors"] if e["rule"] == "caminho_parcial"), None)
        self.assertIsNotNone(error)
        self.assertIn("src/quotes/Quote.java", error.get("detail", ""))

    def test_verify_rejects_invalid_partial_relative_path(self):
        report = verify_markdown(
            self.repo,
            f"- A classe de cotação existe no pacote de domínio de quotes e deve ser rastreada "
            f"pelo caminho completo. {GREEN} Evidência: `quotes/Quote.java:1`",
        )
        self.assertFalse(report["ok"])
        self.assertTrue(any(e["rule"] == "arquivo_inexistente" for e in report["errors"]))

    def test_verify_accepts_full_relative_path_for_nested_file(self):
        report = verify_markdown(
            self.repo,
            f"- A classe de cotação existe no pacote de domínio de quotes e deve ser rastreada "
            f"pelo caminho relativo completo. {GREEN} Evidência: `src/quotes/Quote.java:1`",
        )
        self.assertTrue(report["ok"], report)

    def test_verify_rejects_confirmed_artifact_in_english(self):
        report = verify_markdown(
            self.repo,
            "\n".join(
                [
                    "## Overview",
                    f"- Responsibility: this module handles payment retry with explicit policy. {GREEN} `src/payments.py:1`",
                    f"- Requirements: the implementation returns operational status after processing. {GREEN} `src/payments.py:2`",
                ]
            ),
        )
        self.assertFalse(report["ok"])
        self.assertTrue(any(e["rule"] == "provavel_ingles" for e in report["errors"]))

    def test_verify_rejects_boilerplate_and_generic_green_claim(self):
        report = verify_markdown(
            self.repo,
            f"- TODO preencher. Serviço existe. {GREEN} `src/payments.py:1`",
        )
        self.assertFalse(report["ok"])
        rules = {e["rule"] for e in report["errors"]}
        self.assertIn("boilerplate", rules)
        self.assertIn("claim_verde_generica", rules)

    def test_verify_rejects_isolated_technical_todo(self):
        report = verify_markdown(
            self.repo,
            f"- TODO revisar a política de retry porque o recorte confirma limite e retorno operacional para pagamento. {GREEN} Evidência: `src/payments.py:1-2`",
        )

        self.assertFalse(report["ok"])
        self.assertTrue(any(e["rule"] == "boilerplate" for e in report["errors"]))

    def test_verify_accepts_ptbr_todo_words(self):
        report = verify_markdown(
            self.repo,
            f"- todo pagamento válido percorre todos os passos de tentativa antes de retornar resultado operacional confirmado. {GREEN} Evidência: `src/payments.py:1-2`",
        )

        self.assertTrue(report["ok"], report)


if __name__ == "__main__":
    unittest.main(verbosity=2)
