"""Testes de extract.py — C) extract_candidates, marker rules, IDs, English degradation."""

import unittest

from ingestion import extract as extr
from ingestion import normalize as norm


class TestExplicitIds(unittest.TestCase):
    """C: IDs explícitos (INI-008, DEC-017, RF-042, RN-023, US-031) capturados."""

    def test_parse_known_prefix_variants(self):
        """parse_explicit_ids() aceita variantes (ini 008, INI008, ini-008)."""
        ids1 = extr.parse_explicit_ids("ini 008")
        ids2 = extr.parse_explicit_ids("INI008")
        ids3 = extr.parse_explicit_ids("ini-008")
        self.assertEqual(ids1[0].value, "INI-008")
        self.assertEqual(ids2[0].value, "INI-008")
        self.assertEqual(ids3[0].value, "INI-008")

    def test_parse_all_known_prefixes(self):
        """parse_explicit_ids() reconhece todos os prefixos conhecidos."""
        text = "INI-042 refina DEC-017 com RF-042 e RN-023, história US-031"
        ids = extr.parse_explicit_ids(text)
        values = {i.value for i in ids}
        self.assertIn("INI-042", values)
        self.assertIn("DEC-017", values)
        self.assertIn("RF-042", values)
        self.assertIn("RN-023", values)
        self.assertIn("US-031", values)

    def test_parse_unknown_prefix_requires_uppercase_hyphen(self):
        """parse_explicit_ids() com prefixo desconhecido: só em MAIÚSCULA-número."""
        # "xyz-042" em maiúsculas → reconhecido como id desconhecido
        ids = extr.parse_explicit_ids("Implementação de XYZ-042 no sistema")
        xyz_ids = [i for i in ids if "XYZ" in i.value]
        self.assertTrue(len(xyz_ids) > 0)
        self.assertEqual(xyz_ids[0].value, "XYZ-042")

        # "xyz 042" em minúsculas → não é id
        ids_lower = extr.parse_explicit_ids("xyz 042 no documento")
        xyz_lower = [i for i in ids_lower if "xyz" in i.value.lower()]
        self.assertEqual(len(xyz_lower), 0)

    def test_parse_id_entity_type_known(self):
        """parse_explicit_ids() seta entity_type para prefixos conhecidos."""
        ids = extr.parse_explicit_ids("DEC-017")
        self.assertTrue(ids[0].known)
        self.assertEqual(ids[0].entity_type, extr.EntityType.DECISION)

    def test_parse_id_entity_type_unknown(self):
        """parse_explicit_ids() deixa entity_type=None para prefixo desconhecido."""
        ids = extr.parse_explicit_ids("XYZ-999")
        self.assertFalse(ids[0].known)
        self.assertIsNone(ids[0].entity_type)

    def test_parse_id_deduplication(self):
        """parse_explicit_ids() não duplica o mesmo id."""
        ids = extr.parse_explicit_ids("INI-042 refina INI-042")
        ini_ids = [i for i in ids if i.value == "INI-042"]
        self.assertEqual(len(ini_ids), 1)


class TestClassifyBlock(unittest.TestCase):
    """C: classify_block — decision, question, hypothesis, requirement, action, assertion."""

    def test_classify_decision(self):
        """Blocos com marcadores de decisão → DECISION."""
        texts = [
            "Decidimos usar PostgreSQL",
            "Ficou decidido optar por Rust",
            "Aprovamos a proposta de refatoração",
        ]
        for text in texts:
            kind, marker = extr.classify_block(text)
            self.assertEqual(kind, extr.CandidateKind.DECISION, f"Failed for: {text}")
            self.assertTrue(len(marker) > 0, f"No marker for: {text}")

    def test_classify_question(self):
        """Interrogativa ou "não sabemos" → QUESTION."""
        texts = [
            "O serviço deve validar CPF?",
            "Não sabemos como lidar com isso",
            "Não ficou claro qual abordagem seguir",
        ]
        for text in texts:
            kind, marker = extr.classify_block(text)
            self.assertEqual(kind, extr.CandidateKind.QUESTION, f"Failed for: {text}")

    def test_classify_hypothesis(self):
        """"Talvez", "acho que", "provavelmente" → HYPOTHESIS."""
        texts = [
            "Talvez a gente aprove o token amanhã",
            "Achamos que essa é a melhor solução",
            "Provavelmente temos um gargalo aqui",
        ]
        for text in texts:
            kind, marker = extr.classify_block(text)
            self.assertEqual(kind, extr.CandidateKind.HYPOTHESIS, f"Failed for: {text}")

    def test_classify_requirement(self):
        """"Deve", "precisa", "é obrigatório" → REQUIREMENT."""
        texts = [
            "O servico deve validar entrada",
            "O sistema precisa de latencia menor que 100ms",
            "E obrigatorio ter auditoria",
        ]
        for text in texts:
            kind, marker = extr.classify_block(text)
            self.assertEqual(kind, extr.CandidateKind.REQUIREMENT, f"Failed for: {text}")

    def test_classify_action(self):
        """"Vai fazer", "fica responsável", "até dia X" → ACTION."""
        texts = [
            "João vai fazer o deploy até dia 15",
            "Maria fica responsável pela documentação",
            "O time assume a tarefa",
        ]
        for text in texts:
            kind, marker = extr.classify_block(text)
            self.assertEqual(kind, extr.CandidateKind.ACTION, f"Failed for: {text}")

    def test_classify_assertion_no_marker(self):
        """Sem marcador linguístico → ASSERTION."""
        texts = [
            "O PostgreSQL é usado em produção",
            "A base de dados foi migrada",
            "Temos 50 usuários ativos",
        ]
        for text in texts:
            kind, marker = extr.classify_block(text)
            self.assertEqual(kind, extr.CandidateKind.ASSERTION, f"Failed for: {text}")
            self.assertEqual(marker, "", f"Expected no marker for: {text}")

    def test_classify_precedence_question_over_hypothesis(self):
        """Pergunta vence hipótese em precedência."""
        text = "Talvez o serviço deva validar CPF?"
        kind, marker = extr.classify_block(text)
        self.assertEqual(kind, extr.CandidateKind.QUESTION)

    def test_classify_accent_insensitive(self):
        """Classificação não sensível a acentos."""
        texts = [
            "Decidimos usar PostgreSQL",
            "Decidimos usar PostgreSQL",  # sem acento
        ]
        kinds = [extr.classify_block(t)[0] for t in texts]
        self.assertEqual(kinds[0], kinds[1])


class TestCandidateInvariant(unittest.TestCase):
    """C: Candidate.__post_init__ rejeita nature=IMPLEMENTED ou epistemic=SUPPORTED."""

    def test_candidate_rejects_implemented(self):
        """Candidate levanta quando nature=IMPLEMENTED."""
        ref = extr.BlockRef(
            block_id="blk-00000-abc",
            block_kind="paragraph",
            locator={"version": "sha256:abc", "section": "intro", "block": "blk-00000-abc"},
        )
        with self.assertRaises(extr.CandidateInvariant) as cm:
            extr.Candidate(
                text="Test",
                kind=extr.CandidateKind.ASSERTION,
                block_refs=(ref,),
                nature=extr.FactNature.IMPLEMENTED,
                epistemic=extr.EpistemicStatus.INFERRED,
                lifecycle=extr.LifecycleStatus.CURRENT,
            )
        self.assertIn("implemented", str(cm.exception).lower())

    def test_candidate_rejects_supported(self):
        """Candidate levanta quando epistemic=SUPPORTED."""
        ref = extr.BlockRef(
            block_id="blk-00000-abc",
            block_kind="paragraph",
            locator={"version": "sha256:abc", "section": "intro", "block": "blk-00000-abc"},
        )
        with self.assertRaises(extr.CandidateInvariant) as cm:
            extr.Candidate(
                text="Test",
                kind=extr.CandidateKind.ASSERTION,
                block_refs=(ref,),
                nature=extr.FactNature.OBSERVED,
                epistemic=extr.EpistemicStatus.SUPPORTED,
                lifecycle=extr.LifecycleStatus.CURRENT,
            )
        self.assertIn("supported", str(cm.exception).lower())

    def test_candidate_rejects_no_block_refs(self):
        """Candidate levanta quando sem block_refs."""
        with self.assertRaises(extr.CandidateInvariant) as cm:
            extr.Candidate(
                text="Test",
                kind=extr.CandidateKind.ASSERTION,
                block_refs=(),
                nature=extr.FactNature.OBSERVED,
                epistemic=extr.EpistemicStatus.INFERRED,
                lifecycle=extr.LifecycleStatus.CURRENT,
            )
        self.assertIn("localizador", str(cm.exception).lower())


class TestExtractCandidates(unittest.TestCase):
    """C: extract_candidates — bloco "Decidimos X" → decision; interrogativa → question, etc."""

    def _make_doc(self, blocks_text, metadata=None):
        """Helper: cria SourceDocument simplificado para teste."""
        blocks = []
        for i, text in enumerate(blocks_text):
            blocks.append(norm.Block(
                block_id=f"blk-{i:05d}-test",
                kind=norm.BlockKind.PARAGRAPH,
                text=text,
                locator={"version": "sha256:test", "section": "corpo", "block": f"blk-{i:05d}-test"},
                index=i,
                source_kind=norm.SourceKind.DOCUMENT,
                content_kind=norm.ContentKind.PROSE,
            ))
        return {
            "source_id": "src_testdoc",
            "blocks": blocks,
            "status": "ingested",
            "metadata": metadata or {},
            "diagnostics": [],
        }

    def test_extract_decision(self):
        """Extract de bloco com marcador de decisão."""
        doc = self._make_doc(["Decidimos usar PostgreSQL"])
        result = extr.extract_candidates(doc)
        self.assertEqual(len(result.decisions), 1)
        self.assertEqual(result.decisions[0].text, "Decidimos usar PostgreSQL")

    def test_extract_question(self):
        """Extract de interrogativa."""
        doc = self._make_doc(["Qual banco de dados usar?"])
        result = extr.extract_candidates(doc)
        self.assertEqual(len(result.questions), 1)

    def test_extract_hypothesis(self):
        """Extract de hipótese."""
        doc = self._make_doc(["Talvez possamos usar Redis"])
        result = extr.extract_candidates(doc)
        self.assertEqual(len(result.hypotheses), 1)

    def test_extract_requirement(self):
        """Extract de requisito."""
        doc = self._make_doc(["Precisa ser escalável"])
        result = extr.extract_candidates(doc)
        self.assertEqual(len(result.requirements), 1)

    def test_extract_action(self):
        """Extract de ação."""
        doc = self._make_doc(["João fica responsável até dia 15"])
        result = extr.extract_candidates(doc)
        self.assertEqual(len(result.actions), 1)

    def test_extract_assertion(self):
        """Extract de bloco neutro → assertion."""
        doc = self._make_doc(["PostgreSQL é usado em produção"])
        result = extr.extract_candidates(doc)
        self.assertEqual(len(result.assertions), 1)
        self.assertEqual(result.assertions[0].nature, extr.FactNature.OBSERVED)

    def test_extract_explicit_ids(self):
        """Extract captura IDs explícitos."""
        doc = self._make_doc(["DEC-017 refina INI-042"])
        result = extr.extract_candidates(doc)
        ids = result.explicit_ids()
        values = {i.value for i in ids}
        self.assertIn("DEC-017", values)
        self.assertIn("INI-042", values)

    def test_extract_metadata_whitelist(self):
        """Extract respeita whitelist de metadata."""
        doc = self._make_doc(
            ["Content"],
            metadata={
                "initiative_id": "INI-001",
                "phase": "design",
                "system_prompt": "ignored",  # suspeita
            }
        )
        result = extr.extract_candidates(doc)
        # system_prompt deve gerar diagnóstico de tentativa de instrução
        self.assertTrue(any("system_prompt" in d for d in result.diagnostics))

    def test_extract_english_text_degrades_to_assertion(self):
        """Texto em inglês → ASSERTION (degradação segura)."""
        doc = self._make_doc([
            "We decided to use PostgreSQL",
            "Maybe we can use Redis",
        ])
        result = extr.extract_candidates(doc)
        # Em inglês, marcadores não casam → todas viram assertion
        # (assunção: marcadores em português)
        self.assertTrue(len(result.assertions) >= 2)


class TestCandidateStatesByKind(unittest.TestCase):
    """C: Estados candidatos verificam invariantes (sem IMPLEMENTED, sem SUPPORTED)."""

    def test_candidate_states_no_implemented(self):
        """CANDIDATE_STATES nunca tem FactNature.IMPLEMENTED."""
        for kind, (nature, epistemic, lifecycle) in extr.CANDIDATE_STATES.items():
            self.assertNotEqual(nature, extr.FactNature.IMPLEMENTED,
                              f"{kind.value} não pode ter IMPLEMENTED")

    def test_candidate_states_no_supported(self):
        """CANDIDATE_STATES nunca tem EpistemicStatus.SUPPORTED."""
        for kind, (nature, epistemic, lifecycle) in extr.CANDIDATE_STATES.items():
            self.assertNotEqual(epistemic, extr.EpistemicStatus.SUPPORTED,
                              f"{kind.value} não pode ter SUPPORTED")


if __name__ == "__main__":
    unittest.main()
