"""Perfil de análise por sistema: regressão de default + efeito exato de cada alavanca.

Cada teste aqui prova UMA das duas metades do contrato:

1. **Regressão**: com `profile=None` / `DEFAULT_PROFILE` / parâmetro ausente, o
   resultado é idêntico ao histórico — comparado por JSON completo (ids,
   `snapshot_id`, prioridades, contrato, matriz), não por amostragem.
2. **Efeito**: cada override muda exatamente o que promete e nada mais, e toda
   violação (chave desconhecida, exclusão sem motivo, faixa, brecha da matriz,
   `.wiki-ai.json` inválido) é REJEITADA com erro tipado.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

from analysis import inventory as inv_mod
from analysis import snapshot as snap_mod
from analysis.capabilities import (
    CapabilityCandidate,
    CapabilityMap,
    EntryRef,
    EvidenceRef,
    ExternalDependency,
    GroupingBasis,
    OrphanSymbol,
    ProfileError as CapProfileError,
    discover,
)
from analysis.extractors.base import Entrypoint, Reference, Symbol
from analysis.extractors.registry import (
    EXTRACTOR_EXTENSIONS_ENV,
    ExtractorExtensionError,
    ExtractionResult,
    default_registry,
    load_extractor_extensions,
)
from analysis.investigation import (
    CONTRACT_FIELDS,
    FAILURE_FAMILIES,
    ContractFieldStatus,
    FailureEdgeMatrix,
    InvestigationObjective,
    MatrixIntegrityError,
    MatrixJustificationRequired,
    MatrixState,
    assert_plan_accounted,
    objectives_from_dict,
    objectives_to_dict,
    plan,
    plan_accounting,
)
from analysis.profile import (
    DEFAULT_PROFILE,
    PROFILE_KEYS,
    REPO_CONFIG_FILENAME,
    REPO_LAYER_KEYS,
    AnalysisProfile,
    ProfileError,
    is_repo_layer,
    load_repo_config,
    matches_any,
    resolve_profile,
)


# --------------------------------------------------------------------------
# Fixtures determinísticas (sem I/O onde possível)
# --------------------------------------------------------------------------


def _extraction():
    symbols = [
        Symbol(
            qualname="module_a.handler_1", name="handler_1", kind="function",
            path="src/a.py", line_start=10, line_end=20,
            visibility="public", resolution="syntactic",
        ),
        Symbol(
            qualname="module_a.helper", name="helper", kind="function",
            path="src/a.py", line_start=22, line_end=30,
            visibility="private", resolution="syntactic",
        ),
        Symbol(
            qualname="module_b.handler_2", name="handler_2", kind="function",
            path="src/b.py", line_start=5, line_end=15,
            visibility="public", resolution="syntactic",
        ),
    ]
    entrypoints = [
        Entrypoint(kind="http", name="post_endpoint", path="src/a.py", line=10, framework="flask"),
        Entrypoint(kind="public_api", name="handler_2", path="src/b.py", line=5),
    ]
    references = [
        Reference(
            from_symbol="module_a.handler_1", to_name="module_a.helper", kind="call",
            path="src/a.py", line=15, resolved=True, target="module_a.helper",
            resolution="syntactic",
        ),
        Reference(
            from_symbol="module_a.handler_1", to_name="undefined_func", kind="call",
            path="src/a.py", line=18, resolved=False, reason="dynamic receiver",
        ),
    ]
    return ExtractionResult(
        symbols=symbols, entrypoints=entrypoints, references=references,
        configuration=[], data_entities=[],
    )


def _capability_map():
    """Mapa com 2 capacidades e 1 órfão — permite testar filtro e contagem."""
    ev = EvidenceRef(path="src/a.py", line_start=10, line_end=20, role="entrypoint")
    cap_a = CapabilityCandidate(
        capability_id="cap_pagamentos",
        name="pagamentos",
        entrypoints=(
            EntryRef(kind="http", name="cobrar", path="src/a.py", line=10, line_end=20,
                     anchor="module_a.handler_1", anchor_basis="symbol", evidence=ev),
        ),
        reachable_symbols=("module_a.handler_1", "module_a.helper"),
        paths=("src/a.py",),
        modules=("module_a",),
        gaps=(),
        external_dependencies=(
            ExternalDependency(module="requests", imported_by="module_a", path="src/a.py",
                               line=1, occurrences=1, evidence=ev),
        ),
        evidence_refs=(ev,),
        grouping_basis=GroupingBasis.BEHAVIOR_CLOSURE,
    )
    cap_b = CapabilityCandidate(
        capability_id="cap_relatorios",
        name="relatorios",
        entrypoints=(
            EntryRef(kind="cli", name="exportar", path="src/b.py", line=5, line_end=15,
                     anchor="module_b.handler_2", anchor_basis="symbol", evidence=ev),
        ),
        reachable_symbols=("module_b.handler_2",),
        paths=("src/b.py",),
        modules=("module_b",),
        gaps=(),
        external_dependencies=(),
        evidence_refs=(ev,),
        grouping_basis=GroupingBasis.BEHAVIOR_CLOSURE,
    )
    orphan = OrphanSymbol(
        qualname="module_c.dead", kind="function", path="src/c.py",
        line_start=1, line_end=3, module="module_c", evidence=ev,
    )
    return CapabilityMap(
        namespace="local/analysis",
        snapshot_id="snap_teste",
        capabilities=(cap_a, cap_b),
        orphans=(orphan,),
        shared_symbols=(),
        totals={"entrypoints_total": 2, "entrypoints_grouped": 2},
        notes=(),
    )


def _strip_accents(text):
    import unicodedata

    return "".join(
        c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn"
    )


def _code_evidence():
    return EvidenceRef(path="src/a.py", line_start=1, line_end=2, role="justificativa")


# --------------------------------------------------------------------------
# 0) Contrato de interface do próprio AnalysisProfile
# --------------------------------------------------------------------------


class TestProfileContract(unittest.TestCase):
    def test_default_is_default_and_empty_dict(self):
        self.assertTrue(DEFAULT_PROFILE.is_default())
        self.assertEqual(DEFAULT_PROFILE.to_dict(), {})

    def test_from_mapping_rejects_unknown_key(self):
        with self.assertRaises(ProfileError) as ctx:
            AnalysisProfile.from_mapping({"inclue": ["src"]}, source="cli")
        self.assertIn("inclue", str(ctx.exception))
        self.assertIn("cli", str(ctx.exception))

    def test_from_mapping_rejects_wrong_type(self):
        with self.assertRaises(ProfileError):
            AnalysisProfile.from_mapping({"include": "src"}, source="cli")
        with self.assertRaises(ProfileError):
            AnalysisProfile.from_mapping({"max_rounds": "3"}, source="cli")
        with self.assertRaises(ProfileError):
            AnalysisProfile.from_mapping({"max_rounds": True}, source="cli")

    def test_from_mapping_rejects_negative_int(self):
        with self.assertRaises(ProfileError) as ctx:
            AnalysisProfile.from_mapping({"max_reading_needs": -1}, source="cli")
        self.assertIn(">= 0", str(ctx.exception))

    def test_from_mapping_rejects_exclusion_without_reason(self):
        for payload in (
            {"contract_exclusions": {"dados": ""}},
            {"failure_families_excluded": {"mensageria": "   "}},
        ):
            with self.assertRaises(ProfileError) as ctx:
                AnalysisProfile.from_mapping(payload, source="cli")
            self.assertIn("motivo", str(ctx.exception))

    def test_from_mapping_rejects_field_outside_contract(self):
        with self.assertRaises(ProfileError) as ctx:
            AnalysisProfile.from_mapping(
                {"contract_exclusions": {"inventado": "porque sim"}}, source="cli"
            )
        self.assertIn("§6.3", str(ctx.exception))

    def test_from_mapping_rejects_excluding_every_contract_field(self):
        with self.assertRaises(ProfileError):
            AnalysisProfile.from_mapping(
                {"contract_exclusions": {f: "motivo" for f in CONTRACT_FIELDS}}, source="cli"
            )

    def test_from_mapping_rejects_unknown_family(self):
        with self.assertRaises(ProfileError) as ctx:
            AnalysisProfile.from_mapping(
                {"failure_families_excluded": {"telepatia": "não existe"}}, source="cli"
            )
        self.assertIn("telepatia", str(ctx.exception))

    def test_from_mapping_rejects_extra_family_colliding_with_default(self):
        with self.assertRaises(ProfileError):
            AnalysisProfile.from_mapping(
                {"failure_families_extra": {"entrada": ["x"]}}, source="cli"
            )

    def test_from_mapping_rejects_extra_family_without_items(self):
        with self.assertRaises(ProfileError):
            AnalysisProfile.from_mapping(
                {"failure_families_extra": {"idempotencia_fiscal": []}}, source="cli"
            )

    def test_hub_fraction_range(self):
        for bad in (0, 0.0, 1.5, -0.2):
            with self.assertRaises(ProfileError):
                AnalysisProfile.from_mapping(
                    {"capabilities": {"hub_fraction": bad}}, source="cli"
                )
        ok = AnalysisProfile.from_mapping({"capabilities": {"hub_fraction": 1.0}}, source="cli")
        self.assertEqual(ok.capabilities["hub_fraction"], 1.0)

    def test_budget_policy_keys_are_closed(self):
        with self.assertRaises(ProfileError):
            AnalysisProfile.from_mapping({"budget": {"max_concurrency": 2}}, source="cli")
        with self.assertRaises(ProfileError):
            AnalysisProfile.from_mapping({"policy": {"overhead_bytes": 2}}, source="cli")
        p = AnalysisProfile.from_mapping(
            {"budget": {"max_bytes": 10, "output_reserve_tokens": 5},
             "policy": {"max_concurrency": 2, "timeout_s": 30}},
            source="cli",
        )
        self.assertEqual(dict(p.budget), {"max_bytes": 10, "output_reserve_tokens": 5})
        self.assertEqual(dict(p.policy), {"max_concurrency": 2, "timeout_s": 30})

    def test_trigger_priority_rejects_unknown_trigger(self):
        with self.assertRaises(ProfileError):
            AnalysisProfile.from_mapping({"trigger_priority": {"telepatia": 1}}, source="cli")

    def test_extractors_spec_format(self):
        with self.assertRaises(ProfileError):
            AnalysisProfile.from_mapping({"extractors": ["pacote.modulo"]}, source="cli")
        p = AnalysisProfile.from_mapping({"extractors": ["pkg.mod:make"]}, source="cli")
        self.assertEqual(p.extractors, ("pkg.mod:make",))

    def test_to_dict_only_non_default_and_deterministic(self):
        p = AnalysisProfile.from_mapping(
            {"include": ["src"], "max_rounds": 2, "result_extra_keys": ["extra"]}, source="cli"
        )
        d = p.to_dict()
        self.assertEqual(sorted(d), ["include", "max_rounds", "result_extra_keys"])
        self.assertEqual(json.dumps(d, sort_keys=True), json.dumps(p.to_dict(), sort_keys=True))
        self.assertNotIn("budget", d)

    def test_to_dict_never_emits_source(self):
        """`source` é proveniência de carga, não configuração: nunca sai em `to_dict`."""
        p = AnalysisProfile.from_mapping({"max_reading_needs": 1}, source="repo:.wiki-ai.json")
        merged = p.merge(AnalysisProfile.from_mapping({}, source="cli"))
        self.assertNotIn("source", p.to_dict())
        self.assertNotIn("source", merged.to_dict())
        self.assertEqual(merged.source, ("repo:.wiki-ai.json", "cli"))
        # só `source` diferente do default continua sendo "perfil default"
        self.assertEqual(AnalysisProfile.from_mapping({}, source="cli").to_dict(), {})

    def _profile_with_every_key(self, source):
        return AnalysisProfile.from_mapping(
            {
                "include": ["src", "lib/**"],
                "exclude": ["src/gen/**"],
                "objectives": ["pagamentos"],
                "max_reading_needs": 12,
                "max_rounds": 3,
                "budget": {"max_bytes": 1000, "max_tokens": 200,
                           "overhead_bytes": 10, "output_reserve_tokens": 5},
                "policy": {"max_bytes": 900, "max_tokens": 180,
                           "max_concurrency": 4, "timeout_s": 60},
                "capabilities": {"hub_fraction": 0.4, "hub_min_entries": 3,
                                 "max_evidence_per_capability": 16, "max_sites_per_gap": 5},
                "contract_exclusions": {"persistencia": "sistema sem banco"},
                "failure_families_excluded": {"mensageria": "sistema sem fila"},
                "failure_families_extra": {"fiscal": ["nota_cancelada", "aliquota_retroativa"]},
                "trigger_priority": {"external_integration": 1, "predicate": 99},
                "extractors": ["pkg.mod:make"],
                "result_extra_keys": ["diagnostico_extra"],
            },
            source=source,
        )

    def test_to_dict_roundtrips_through_from_mapping(self):
        """DEFEITO CORRIGIDO: `from_mapping(p.to_dict(), source=...)` levantava
        `ProfileError: chave(s) desconhecida(s): source`."""
        p = self._profile_with_every_key("store")
        again = AnalysisProfile.from_mapping(p.to_dict(), source="store")
        self.assertEqual(again.to_dict(), p.to_dict())
        self.assertEqual(again.source, ("store",))  # proveniência é do parâmetro

    def test_roundtrip_with_three_layer_provenance(self):
        """Perfil de 3 camadas: o payload reidrata idêntico e a proveniência é
        redeclarada por quem carrega, nunca herdada do dado."""
        repo = self._profile_with_every_key("store")
        store = AnalysisProfile.from_mapping(
            {"max_rounds": 5, "budget": {"max_tokens": 999}}, source="store"
        )
        cli = AnalysisProfile.from_mapping({"objectives": ["relatorios"]}, source="cli")
        merged = repo.merge(store).merge(cli)
        self.assertEqual(merged.source, ("store", "store", "cli"))

        payload = merged.to_dict()
        self.assertNotIn("source", payload)
        json.dumps(payload)  # JSON-serializável
        rehydrated = AnalysisProfile.from_mapping(payload, source="store")
        self.assertEqual(rehydrated.to_dict(), payload)
        self.assertEqual(rehydrated.source, ("store",))
        # e o caminho real do CLI: store guardado -> resolve_profile
        resolved = resolve_profile(repo_config=None, store_profile=payload, cli_overrides=None)
        self.assertEqual(resolved.to_dict(), payload)
        self.assertEqual(resolved.source, ("store",))

    def test_explicit_source_key_in_payload_is_ignored_not_rejected(self):
        p = AnalysisProfile.from_mapping(
            {"max_rounds": 2, "source": ["mentira", "forjada"]}, source="cli"
        )
        self.assertEqual(p.source, ("cli",))
        self.assertEqual(p.max_rounds, 2)

    def test_unknown_key_still_rejected_after_source_tolerance(self):
        with self.assertRaises(ProfileError) as ctx:
            AnalysisProfile.from_mapping({"source": ["x"], "sourc": ["y"]}, source="cli")
        self.assertIn("sourc", str(ctx.exception))

    def test_summary_has_counts_reasons_and_overrides(self):
        p = AnalysisProfile.from_mapping(
            {
                "include": ["src", "lib"],
                "objectives": ["pagamentos"],
                "contract_exclusions": {"persistencia": "sem banco"},
                "failure_families_excluded": {"mensageria": "sistema sem fila"},
                "max_reading_needs": 5,
            },
            source="repo:.wiki-ai.json",
        )
        s = p.summary()
        self.assertEqual(s["source"], ["repo:.wiki-ai.json"])
        self.assertEqual((s["include"], s["exclude"], s["objectives"]), (2, 0, 1))
        self.assertEqual(s["contract_exclusions"], {"persistencia": "sem banco"})
        self.assertEqual(s["failure_families_excluded"], {"mensageria": "sistema sem fila"})
        self.assertEqual(s["overrides"]["max_reading_needs"], 5)
        self.assertFalse(s["is_default"])

    def test_merge_order_repo_store_cli(self):
        repo = AnalysisProfile.from_mapping(
            {"include": ["src"], "max_reading_needs": 1},
            source="repo:.wiki-ai.json",
        )
        store = AnalysisProfile.from_mapping(
            {"include": ["lib"], "max_reading_needs": 2, "budget": {"max_bytes": 10}},
            source="store",
        )
        cli = AnalysisProfile.from_mapping(
            {"max_reading_needs": 3, "budget": {"max_tokens": 20}}, source="cli"
        )
        merged = repo.merge(store).merge(cli)
        self.assertEqual(merged.include, ("src", "lib"))          # união ordenada
        self.assertEqual(merged.max_reading_needs, 3)              # cli prevalece
        self.assertEqual(dict(merged.budget), {"max_bytes": 10, "max_tokens": 20})
        self.assertEqual(merged.source, ("repo:.wiki-ai.json", "store", "cli"))

    def test_merge_does_not_mutate_operands(self):
        a = AnalysisProfile.from_mapping({"include": ["src"]}, source="repo")
        b = AnalysisProfile.from_mapping({"include": ["lib"]}, source="cli")
        a.merge(b)
        self.assertEqual(a.include, ("src",))
        self.assertEqual(b.include, ("lib",))

    def test_merge_keeps_scalar_when_other_is_none(self):
        a = AnalysisProfile.from_mapping({"max_reading_needs": 7}, source="repo")
        b = AnalysisProfile.from_mapping({}, source="cli")
        self.assertEqual(a.merge(b).max_reading_needs, 7)

    def test_resolve_profile_order_and_validation(self):
        repo = AnalysisProfile.from_mapping(
            {"max_reading_needs": 1}, source="repo:.wiki-ai.json"
        )
        resolved = resolve_profile(
            repo_config=repo,
            store_profile={"max_reading_needs": 2},
            cli_overrides={"max_reading_needs": 3},
        )
        self.assertEqual(resolved.max_reading_needs, 3)
        self.assertEqual(resolved.source, ("repo:.wiki-ai.json", "store", "cli"))
        with self.assertRaises(ProfileError):
            resolve_profile(repo_config=None, store_profile={"chave_invalida": 1},
                            cli_overrides=None)

    def test_resolve_profile_all_none_is_default(self):
        self.assertTrue(resolve_profile().is_default())

    def test_matches_any_prefix_and_glob(self):
        self.assertTrue(matches_any("src/a.py", ["src"]))
        self.assertTrue(matches_any("src", ["src"]))
        self.assertFalse(matches_any("srcx/a.py", ["src"]))
        self.assertTrue(matches_any("src/a/b.py", ["src/**"]))
        self.assertTrue(matches_any("src", ["src/**"]))
        # `fnmatch` não trata `/` como fronteira: `*` atravessa diretório.
        self.assertTrue(matches_any("src/a.py", ["*.py"]))
        self.assertTrue(matches_any("src/a.py", ["src/*.py"]))
        self.assertFalse(matches_any("src/a.txt", ["src/*.py"]))
        self.assertFalse(matches_any("src/a.py", []))


# --------------------------------------------------------------------------
# 0b) ELEVAÇÃO DE PRIVILÉGIO PELA CAMADA REPO (achado A)
# --------------------------------------------------------------------------


#: Chaves que só quem OPERA a análise pode declarar. `extractors` é a pior:
#: `importlib.import_module` + chamada de fábrica = execução de código arbitrário
#: só por o repositório analisado conter um `.wiki-ai.json`.
_OPERATOR_ONLY = {
    "extractors": ["os:system"],
    "budget": {"max_bytes": 10 ** 9},
    "policy": {"max_concurrency": 64},
    "capabilities": {"hub_fraction": 1.0},
    "max_rounds": 99,
    "result_extra_keys": ["qualquer_coisa"],
}


class TestRepoLayerPrivilege(unittest.TestCase):
    def test_allowlist_partitions_every_key(self):
        """Toda chave do perfil está de um lado ou do outro — sem terceira via."""
        self.assertTrue(REPO_LAYER_KEYS.issubset(set(PROFILE_KEYS)))
        self.assertEqual(
            set(PROFILE_KEYS) - REPO_LAYER_KEYS, set(_OPERATOR_ONLY),
            "toda chave fora da allowlist do repo precisa estar coberta por teste",
        )

    def test_is_repo_layer_reads_the_parameter_only(self):
        self.assertTrue(is_repo_layer("repo"))
        self.assertTrue(is_repo_layer("repo:.wiki-ai.json"))
        self.assertFalse(is_repo_layer("store"))
        self.assertFalse(is_repo_layer("cli"))

    def test_each_operator_only_key_is_rejected_in_repo_layer(self):
        for key, value in _OPERATOR_ONLY.items():
            with self.subTest(key=key):
                with self.assertRaises(ProfileError) as ctx:
                    AnalysisProfile.from_mapping({key: value}, source="repo:.wiki-ai.json")
                msg = str(ctx.exception)
                self.assertIn(key, msg)
                self.assertIn("apenas em store/cli", msg)

    def test_each_operator_only_key_is_accepted_in_store_and_cli(self):
        for key, value in _OPERATOR_ONLY.items():
            for layer in ("store", "cli"):
                with self.subTest(key=key, layer=layer):
                    prof = AnalysisProfile.from_mapping({key: value}, source=layer)
                    self.assertIn(key, prof.to_dict())

    def test_every_allowlisted_key_still_works_in_repo_layer(self):
        prof = AnalysisProfile.from_mapping(
            {
                "include": ["src"],
                "exclude": ["src/gen/**"],
                "objectives": ["pagamentos"],
                "max_reading_needs": 5,
                "contract_exclusions": {"persistencia": "sem banco"},
                "failure_families_excluded": {"mensageria": "sem fila"},
                "failure_families_extra": {"fiscal": ["nota_cancelada"]},
                "trigger_priority": {"predicate": 1},
                "discovery_max_files_per_objective": 4,
            },
            source="repo:.wiki-ai.json",
        )
        self.assertEqual(sorted(prof.to_dict()), sorted(REPO_LAYER_KEYS))

    def test_payload_cannot_declare_its_own_layer(self):
        """A camada vem do PARÂMETRO: escrever `source: ["store"]` não eleva nada."""
        with self.assertRaises(ProfileError):
            AnalysisProfile.from_mapping(
                {"source": ["store"], "extractors": ["os:system"]},
                source="repo:.wiki-ai.json",
            )

    def test_load_repo_config_rejects_extractors(self):
        repo = tempfile.mkdtemp(prefix="wikiai_rce_")
        self.addCleanup(shutil.rmtree, repo, True)
        path = os.path.join(repo, REPO_CONFIG_FILENAME)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"extractors": ["analysis.tests.test_profile:make_fake_extractor"]}, fh)
        with self.assertRaises(ProfileError) as ctx:
            load_repo_config(repo)
        self.assertIn("extractors", str(ctx.exception))
        self.assertIn("apenas em store/cli", str(ctx.exception))

    def test_forged_repo_config_object_cannot_elevate(self):
        """`AnalysisProfile(...)` direto burla `from_mapping`; `resolve_profile` barra."""
        for key, value in (
            ("extractors", ("os:system",)),
            ("budget", {"max_bytes": 10 ** 9}),
            ("policy", {"max_concurrency": 64}),
            ("capabilities", {"hub_fraction": 1.0}),
            ("max_rounds", 99),
            ("result_extra_keys", ("x",)),
        ):
            with self.subTest(key=key):
                forged = AnalysisProfile(**{key: value}, source=("repo:.wiki-ai.json",))
                self.assertEqual(forged.repo_layer_violations(), (key,))
                with self.assertRaises(ProfileError) as ctx:
                    resolve_profile(repo_config=forged)
                self.assertIn(key, str(ctx.exception))

    def test_resolve_profile_repo_cannot_elevate_via_merge(self):
        """Nem por dentro do merge: o repo entra barrado ANTES de compor."""
        forged = AnalysisProfile(extractors=("os:system",), source=("repo:.wiki-ai.json",))
        with self.assertRaises(ProfileError):
            resolve_profile(repo_config=forged, cli_overrides={"max_rounds": 1})

    def test_store_and_cli_keep_operator_privileges_through_resolve(self):
        resolved = resolve_profile(
            repo_config=AnalysisProfile.from_mapping(
                {"include": ["src"]}, source="repo:.wiki-ai.json"
            ),
            store_profile={"budget": {"max_bytes": 100}},
            cli_overrides={"extractors": ["pkg.mod:make"], "max_rounds": 2},
        )
        self.assertEqual(resolved.include, ("src",))
        self.assertEqual(dict(resolved.budget), {"max_bytes": 100})
        self.assertEqual(resolved.extractors, ("pkg.mod:make",))
        self.assertEqual(resolved.max_rounds, 2)

    def test_resolve_profile_rejects_non_profile_repo_config(self):
        with self.assertRaises(ProfileError):
            resolve_profile(repo_config={"include": ["src"]})


# --------------------------------------------------------------------------
# 1) .wiki-ai.json
# --------------------------------------------------------------------------


class TestRepoConfig(unittest.TestCase):
    def setUp(self):
        self.repo = tempfile.mkdtemp(prefix="wikiai_profile_")
        self.addCleanup(shutil.rmtree, self.repo, True)

    def _write(self, text):
        path = os.path.join(self.repo, REPO_CONFIG_FILENAME)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def test_absent_returns_none(self):
        self.assertIsNone(load_repo_config(self.repo))

    def test_valid_config_carries_source(self):
        self._write(json.dumps({"include": ["src"], "max_reading_needs": 3}))
        prof = load_repo_config(self.repo)
        self.assertEqual(prof.include, ("src",))
        self.assertEqual(prof.max_reading_needs, 3)
        self.assertEqual(prof.source, ("repo:.wiki-ai.json",))

    def test_invalid_json_reports_path(self):
        path = self._write("{ isto não é json")
        with self.assertRaises(ProfileError) as ctx:
            load_repo_config(self.repo)
        self.assertIn(path, str(ctx.exception))
        self.assertIn("JSON inválido", str(ctx.exception))

    def test_non_object_root_reports_path(self):
        path = self._write("[1, 2, 3]")
        with self.assertRaises(ProfileError) as ctx:
            load_repo_config(self.repo)
        self.assertIn(path, str(ctx.exception))

    def test_violation_inside_config_is_rejected(self):
        self._write(json.dumps({"failure_families_excluded": {"entrada": ""}}))
        with self.assertRaises(ProfileError) as ctx:
            load_repo_config(self.repo)
        self.assertIn("motivo", str(ctx.exception))


# --------------------------------------------------------------------------
# 2) snapshot.capture: include (prefixo/glob) + exclude + contagem
# --------------------------------------------------------------------------


class TestSnapshotProfile(unittest.TestCase):
    def setUp(self):
        self.repo = tempfile.mkdtemp(prefix="wikiai_snap_")
        self.addCleanup(shutil.rmtree, self.repo, True)
        for rel, body in (
            ("src/a.py", "print('a')\n"),
            ("src/b.py", "print('b')\n"),
            ("src/gen/c_pb2.py", "print('gen')\n"),
            ("docs/readme.md", "# doc\n"),
        ):
            full = os.path.join(self.repo, *rel.split("/"))
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w", encoding="utf-8") as fh:
                fh.write(body)

    def test_default_call_unchanged(self):
        """Regressão: sem `exclude`, `snapshot_id` e arquivos são os de antes."""
        a = snap_mod.capture(self.repo)
        b = snap_mod.capture(self.repo, exclude=None)
        self.assertEqual(a.snapshot_id, b.snapshot_id)
        self.assertEqual([f.path for f in a.files], [f.path for f in b.files])
        self.assertEqual(a.excluded_by_profile, 0)
        self.assertEqual(a.exclude_patterns, ())

    def test_prefix_scope_still_prefix(self):
        snap = snap_mod.capture(self.repo, scope=["src"])
        paths = [f.path for f in snap.files]
        self.assertTrue(all(p.startswith("src/") for p in paths))
        self.assertNotIn("docs/readme.md", paths)

    def test_glob_scope(self):
        snap = snap_mod.capture(self.repo, scope=["src/*.py"])
        self.assertEqual(
            [f.path for f in snap.files],
            ["src/a.py", "src/b.py", "src/gen/c_pb2.py"],
        )
        self.assertEqual([f.path for f in snap_mod.capture(self.repo, scope=["*.md"]).files],
                         ["docs/readme.md"])

    def test_exclude_beats_include_and_is_counted(self):
        snap = snap_mod.capture(self.repo, scope=["src"], exclude=["src/gen/**"])
        paths = [f.path for f in snap.files]
        self.assertEqual(paths, ["src/a.py", "src/b.py"])
        self.assertEqual(snap.excluded_by_profile, 1)
        self.assertEqual(snap.exclude_patterns, ("src/gen/**",))
        self.assertTrue(
            any("excluded_by_profile" in w for w in snap.warnings),
            "exclusão pelo perfil precisa aparecer em warnings — nunca silenciosa",
        )

    def test_exclude_changes_snapshot_id(self):
        full = snap_mod.capture(self.repo, scope=["src"])
        cut = snap_mod.capture(self.repo, scope=["src"], exclude=["src/gen/**"])
        self.assertNotEqual(full.snapshot_id, cut.snapshot_id)

    def test_excluded_by_profile_not_in_identity(self):
        """A contagem é metadado: dois snapshots do MESMO conjunto batem."""
        a = snap_mod.capture(self.repo, scope=["src/a.py", "src/b.py"])
        b = snap_mod.capture(self.repo, scope=["src"], exclude=["src/gen/**"])
        self.assertEqual(a.snapshot_id, b.snapshot_id)
        self.assertNotEqual(a.excluded_by_profile, b.excluded_by_profile)


# --------------------------------------------------------------------------
# 3) inventory.build: exclusões extras do perfil, registradas
# --------------------------------------------------------------------------


class TestInventoryProfile(unittest.TestCase):
    def setUp(self):
        self.repo = tempfile.mkdtemp(prefix="wikiai_inv_")
        self.addCleanup(shutil.rmtree, self.repo, True)
        for rel in ("src/a.py", "src/b.py", "vendor/lib.py", "src/gen/c_pb2.py"):
            full = os.path.join(self.repo, *rel.split("/"))
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w", encoding="utf-8") as fh:
                fh.write("x = 1\n")
        self.snapshot = snap_mod.capture(self.repo)

    def test_default_build_unchanged(self):
        a = inv_mod.build(self.snapshot)
        b = inv_mod.build(self.snapshot, extra_excluded_dirs=(), exclude_paths=())
        self.assertEqual([f.path for f in a.files], [f.path for f in b.files])
        self.assertEqual(a.summary["exclusions"], b.summary["exclusions"])
        self.assertNotIn("vendor", str(a.summary["exclusions"]))

    def test_extra_excluded_dirs_does_not_mutate_default(self):
        before = dict(inv_mod._EXCLUDED_DIR_NAMES)
        inv = inv_mod.build(self.snapshot, extra_excluded_dirs=["vendor"])
        paths = [f.path for f in inv.files]
        self.assertNotIn("vendor/lib.py", paths)
        self.assertEqual(dict(inv_mod._EXCLUDED_DIR_NAMES), before)

    def test_extra_excluded_dirs_recorded_with_reason(self):
        inv = inv_mod.build(self.snapshot, extra_excluded_dirs=["vendor"])
        hits = [e for e in inv.exclusions if e.path == "vendor"]
        self.assertEqual(len(hits), 1)
        self.assertIn("perfil de análise", hits[0].motivo)
        self.assertIn("1 arquivo(s)", hits[0].impacto)
        self.assertEqual(inv.summary["exclusions_count"], len(inv.exclusions))

    def test_exclude_paths_glob_recorded(self):
        inv = inv_mod.build(self.snapshot, exclude_paths=["src/gen/**"])
        paths = [f.path for f in inv.files]
        self.assertNotIn("src/gen/c_pb2.py", paths)
        hits = [e for e in inv.exclusions if e.path == "src/gen/**"]
        self.assertEqual(len(hits), 1)
        self.assertIn("perfil de análise", hits[0].motivo)


# --------------------------------------------------------------------------
# 4) capabilities.discover: faixa validada no ponto de uso
# --------------------------------------------------------------------------


class TestCapabilitiesTuning(unittest.TestCase):
    def test_default_discover_unchanged(self):
        ext = _extraction()
        a = discover(ext)
        b = discover(ext, **dict(AnalysisProfile().capabilities))
        self.assertEqual(
            [c.capability_id for c in a.capabilities],
            [c.capability_id for c in b.capabilities],
        )

    def test_profile_capabilities_are_splattable(self):
        prof = AnalysisProfile.from_mapping(
            {"capabilities": {"hub_fraction": 0.9, "hub_min_entries": 2,
                              "max_evidence_per_capability": 3, "max_sites_per_gap": 1}},
            source="cli",
        )
        result = discover(_extraction(), **prof.capabilities)
        for cap in result.capabilities:
            self.assertLessEqual(len(cap.evidence_refs), 3)

    def test_out_of_range_rejected_at_use_site(self):
        with self.assertRaises(CapProfileError):
            discover(_extraction(), hub_fraction=0.0)
        with self.assertRaises(CapProfileError):
            discover(_extraction(), hub_fraction=1.4)
        with self.assertRaises(CapProfileError):
            discover(_extraction(), max_sites_per_gap=-1)

    def test_profile_error_is_the_same_class(self):
        self.assertIs(CapProfileError, ProfileError)


# --------------------------------------------------------------------------
# 5) investigation.plan: cada alavanca
# --------------------------------------------------------------------------


class TestPlanProfile(unittest.TestCase):
    def setUp(self):
        self.cmap = _capability_map()
        self.ext = _extraction()

    def _plan(self, profile=None, **kw):
        return plan(self.cmap, self.ext, profile=profile, **kw)

    # -- regressão ---------------------------------------------------------
    def test_default_profile_is_byte_identical(self):
        """`None`, ausência e `DEFAULT_PROFILE` produzem o MESMO payload."""
        base = json.dumps(objectives_to_dict(plan(self.cmap, self.ext)), sort_keys=True)
        none_ = json.dumps(objectives_to_dict(self._plan(None)), sort_keys=True)
        default = json.dumps(objectives_to_dict(self._plan(DEFAULT_PROFILE)), sort_keys=True)
        self.assertEqual(base, none_)
        self.assertEqual(base, default)

    def test_default_matrix_still_40_cells(self):
        obj = self._plan()[0]
        self.assertEqual(obj.matrix.summary()["total"], sum(len(v) for v in FAILURE_FAMILIES.values()))
        self.assertEqual(obj.matrix.summary()["implicit_pending"], obj.matrix.summary()["total"])

    def test_default_contract_still_13_fields(self):
        obj = self._plan()[0]
        self.assertEqual(sorted(obj.contract), sorted(CONTRACT_FIELDS))

    def test_plan_rejects_non_profile(self):
        with self.assertRaises(ProfileError):
            self._plan({"include": ["src"]})

    # -- (a) filtro por objetivo ------------------------------------------
    def test_objectives_filter_by_substring(self):
        prof = AnalysisProfile.from_mapping({"objectives": ["PAGAMENT"]}, source="cli")
        objs = self._plan(prof)
        self.assertEqual([o.capability_id for o in objs], ["cap_pagamentos"])

    def test_objectives_filter_by_exact_objective_id(self):
        full = self._plan()
        target = full[0]
        prof = AnalysisProfile.from_mapping({"objectives": [target.objective_id]}, source="cli")
        objs = self._plan(prof)
        self.assertEqual([o.objective_id for o in objs], [target.objective_id])

    def test_objectives_filter_keeps_ids_stable(self):
        """Filtrar NÃO muda o id do que sobrou — o mesmo objetivo, menos vizinhos."""
        full = {o.capability_id: o.objective_id for o in self._plan()}
        prof = AnalysisProfile.from_mapping({"objectives": ["relatorios"]}, source="cli")
        cut = self._plan(prof)
        self.assertEqual(cut[0].objective_id, full["cap_relatorios"])

    def test_suppressed_objectives_are_counted_not_hidden(self):
        prof = AnalysisProfile.from_mapping({"objectives": ["pagamentos"]}, source="cli")
        objs = self._plan(prof)
        acc = plan_accounting(objs, self.cmap, profile=prof)
        self.assertEqual(acc["capabilities"], 2)
        self.assertEqual(acc["capabilities_selected"], 1)
        self.assertEqual(acc["capabilities_suppressed_by_profile"], 1)
        self.assertEqual(acc["orphan_symbols_suppressed_by_profile"], 1)
        assert_plan_accounted(objs, self.cmap, profile=prof)

    def test_assert_plan_accounted_without_profile_still_strict(self):
        prof = AnalysisProfile.from_mapping({"objectives": ["pagamentos"]}, source="cli")
        objs = self._plan(prof)
        from analysis.investigation import PlanAccountingError

        with self.assertRaises(PlanAccountingError):
            assert_plan_accounted(objs, self.cmap)

    # -- (b) exclusão de campo do contrato --------------------------------
    def test_contract_exclusion_marks_field_and_frees_complete(self):
        prof = AnalysisProfile.from_mapping(
            {"contract_exclusions": {"persistencia": "sistema sem banco de dados"}},
            source="repo:.wiki-ai.json",
        )
        obj = self._plan(prof)[0]
        field = obj.field("persistencia")
        self.assertIs(field.status, ContractFieldStatus.EXCLUDED)
        self.assertIn("sistema sem banco de dados", field.motivo)
        self.assertIn("profile:repo:.wiki-ai.json", field.motivo)
        self.assertNotIn("persistencia", obj.pending_fields())
        # o motivo viaja no payload (auditável)
        payload = obj.to_dict()
        self.assertIn("sistema sem banco de dados", payload["contract"]["persistencia"]["motivo"])

    def test_contract_exclusion_removes_field_from_unmet_obligations(self):
        prof = AnalysisProfile.from_mapping(
            {"contract_exclusions": {"persistencia": "sem banco"}}, source="cli"
        )
        with_prof = self._plan(prof)[0]
        without = self._plan()[0]
        self.assertIn("persistencia", " ".join(without.unmet_obligations()))
        self.assertNotIn("persistencia", " ".join(with_prof.unmet_obligations()))

    def test_contract_exclusion_uses_the_same_representation_as_manual_exclude(self):
        """Reutiliza `ContractField.exclude` — nada de campo paralelo."""
        prof = AnalysisProfile.from_mapping(
            {"contract_exclusions": {"dados": "capacidade sem transformação de dados"}},
            source="cli",
        )
        obj = self._plan(prof)[0]
        manual = self._plan()[0]
        manual.field("dados").exclude("capacidade sem transformação de dados [profile:cli]")
        self.assertEqual(obj.field("dados").to_dict(), manual.field("dados").to_dict())

    def test_contract_exclusion_is_noted(self):
        prof = AnalysisProfile.from_mapping(
            {"contract_exclusions": {"dados": "n/a"}}, source="cli"
        )
        obj = self._plan(prof)[0]
        self.assertTrue(any("excluído(s) pelo perfil" in n for n in obj.notes))

    def test_contract_exclusion_applies_to_orphan_objectives_too(self):
        prof = AnalysisProfile.from_mapping(
            {"contract_exclusions": {"integracoes": "grupo de órfãos não integra nada"}},
            source="cli",
        )
        orphans = [o for o in self._plan(prof) if o.kind.value == "orphan_group"]
        self.assertEqual(len(orphans), 1)
        self.assertIs(orphans[0].field("integracoes").status, ContractFieldStatus.EXCLUDED)

    # -- (c) matriz de falhas ---------------------------------------------
    def test_failure_family_exclusion_marks_cells_with_reason(self):
        prof = AnalysisProfile.from_mapping(
            {"failure_families_excluded": {"mensageria": "sistema não usa fila nem broker"}},
            source="repo:.wiki-ai.json",
        )
        obj = self._plan(prof)[0]
        for item in FAILURE_FAMILIES["mensageria"]:
            cell = obj.matrix.cell("mensageria", item)
            self.assertIs(cell.state, MatrixState.NOT_APPLICABLE)
            self.assertTrue(cell.marked)
            self.assertEqual(cell.note, "sistema não usa fila nem broker")
            self.assertEqual(cell.justification_source, "profile:repo:.wiki-ai.json")
        # nenhuma outra família foi tocada
        self.assertIs(obj.matrix.cell("entrada", "ausente").state, MatrixState.UNRESOLVED)

    def test_failure_family_exclusion_shrinks_implicit_pending(self):
        prof = AnalysisProfile.from_mapping(
            {"failure_families_excluded": {"mensageria": "sem fila"}}, source="cli"
        )
        before = self._plan()[0].matrix.summary()["implicit_pending"]
        after = self._plan(prof)[0].matrix.summary()["implicit_pending"]
        self.assertEqual(before - after, len(FAILURE_FAMILIES["mensageria"]))

    def test_failure_family_extra_adds_unresolved_cells(self):
        prof = AnalysisProfile.from_mapping(
            {"failure_families_extra": {"fiscal": ["nota_cancelada", "aliquota_retroativa"]}},
            source="cli",
        )
        obj = self._plan(prof)[0]
        self.assertEqual(obj.matrix.families["fiscal"], ("nota_cancelada", "aliquota_retroativa"))
        for item in ("nota_cancelada", "aliquota_retroativa"):
            cell = obj.matrix.cell("fiscal", item)
            self.assertIs(cell.state, MatrixState.UNRESOLVED)
            self.assertFalse(cell.marked)
            self.assertEqual(cell.justification_source, "profile:cli")
        self.assertEqual(
            obj.matrix.summary()["total"],
            sum(len(v) for v in FAILURE_FAMILIES.values()) + 2,
        )

    def test_matrix_is_per_objective_not_shared(self):
        prof = AnalysisProfile.from_mapping(
            {"failure_families_extra": {"fiscal": ["x"]}}, source="cli"
        )
        objs = self._plan(prof)
        objs[0].matrix.mark("fiscal", "x", MatrixState.COVERED, _code_evidence())
        self.assertIs(objs[1].matrix.cell("fiscal", "x").state, MatrixState.UNRESOLVED)

    def test_global_failure_families_not_mutated(self):
        before = {k: tuple(v) for k, v in FAILURE_FAMILIES.items()}
        prof = AnalysisProfile.from_mapping(
            {"failure_families_extra": {"fiscal": ["x"]},
             "failure_families_excluded": {"mensageria": "sem fila"}},
            source="cli",
        )
        self._plan(prof)
        self.assertEqual({k: tuple(v) for k, v in FAILURE_FAMILIES.items()}, before)

    # -- (d) prioridade de gatilho ----------------------------------------
    def test_trigger_priority_override_reorders_without_mutating_global(self):
        from analysis.investigation import _TRIGGER_PRIORITY, ReadingTrigger

        before = dict(_TRIGGER_PRIORITY)
        prof = AnalysisProfile.from_mapping(
            {"trigger_priority": {"external_integration": 1}}, source="cli"
        )
        obj = self._plan(prof)[0]
        ext_needs = [n for n in obj.reading_needs
                     if n.trigger is ReadingTrigger.EXTERNAL_INTEGRATION]
        self.assertTrue(ext_needs)
        self.assertEqual(ext_needs[0].priority, 1)
        self.assertEqual(obj.reading_needs[0].trigger, ReadingTrigger.EXTERNAL_INTEGRATION)
        self.assertEqual(dict(_TRIGGER_PRIORITY), before)

    def test_trigger_priority_untouched_triggers_keep_historic_value(self):
        from analysis.investigation import ReadingTrigger

        prof = AnalysisProfile.from_mapping(
            {"trigger_priority": {"external_integration": 1}}, source="cli"
        )
        obj = self._plan(prof)[0]
        for need in obj.reading_needs:
            if need.trigger is ReadingTrigger.PREDICATE:
                self.assertEqual(need.priority, 40)

    def test_need_ids_do_not_depend_on_priority(self):
        """Regressão de identidade: reordenar não renomeia obrigação."""
        base = {n.need_id for n in self._plan()[0].reading_needs}
        prof = AnalysisProfile.from_mapping(
            {"trigger_priority": {"external_integration": 1}}, source="cli"
        )
        moved = {n.need_id for n in self._plan(prof)[0].reading_needs}
        self.assertEqual(base, moved)

    # -- (e) max_reading_needs --------------------------------------------
    def test_max_reading_needs_from_profile(self):
        prof = AnalysisProfile.from_mapping({"max_reading_needs": 1}, source="cli")
        obj = self._plan(prof)[0]
        self.assertEqual(len(obj.reading_needs), 1)
        self.assertGreater(obj.accounting["reading_needs_dropped"], 0)
        self.assertTrue(any("orçamento" in n for n in obj.notes))

    def test_explicit_argument_wins_over_profile(self):
        prof = AnalysisProfile.from_mapping({"max_reading_needs": 1}, source="cli")
        obj = self._plan(prof, max_reading_needs=2)[0]
        self.assertEqual(len(obj.reading_needs), 2)

    def test_dropped_needs_still_block_complete(self):
        prof = AnalysisProfile.from_mapping({"max_reading_needs": 1}, source="cli")
        obj = self._plan(prof)[0]
        self.assertTrue(
            any("descartada(s) por orçamento" in u for u in obj.unmet_obligations())
        )


# --------------------------------------------------------------------------
# 6) FECHAMENTO DA BRECHA: FailureEdgeMatrix.from_dict
# --------------------------------------------------------------------------


class TestMatrixFromDictHole(unittest.TestCase):
    def test_roundtrip_of_default_matrix(self):
        m = FailureEdgeMatrix()
        again = FailureEdgeMatrix.from_dict(m.to_dict())
        self.assertEqual(again.to_dict(), m.to_dict())

    def test_roundtrip_of_marked_matrix(self):
        m = FailureEdgeMatrix()
        m.mark("entrada", "ausente", MatrixState.COVERED, _code_evidence())
        m.mark("entrada", "nula", MatrixState.UNRESOLVED, note="impacto declarado")
        again = FailureEdgeMatrix.from_dict(m.to_dict())
        self.assertEqual(again.to_dict(), m.to_dict())

    def test_not_applicable_without_justification_is_rejected(self):
        """A brecha: `mark()` exigia evidência, `from_dict()` não exigia nada."""
        payload = {
            "families": {k: list(v) for k, v in FAILURE_FAMILIES.items()},
            "cells": [{"family": "entrada", "item": "ausente",
                       "state": "not_applicable", "marked": True}],
        }
        with self.assertRaises(MatrixJustificationRequired):
            FailureEdgeMatrix.from_dict(payload)

    def test_covered_without_justification_is_rejected(self):
        payload = {"cells": [{"family": "entrada", "item": "nula",
                              "state": "covered", "marked": True}]}
        with self.assertRaises(MatrixJustificationRequired):
            FailureEdgeMatrix.from_dict(payload)

    def test_unknown_family_without_provenance_is_rejected(self):
        payload = {
            "families": {"telepatia": ["adivinhar"]},
            "cells": [{"family": "telepatia", "item": "adivinhar", "state": "unresolved"}],
        }
        with self.assertRaises(MatrixIntegrityError) as ctx:
            FailureEdgeMatrix.from_dict(payload)
        self.assertIn("telepatia", str(ctx.exception))

    def test_injected_cell_outside_families_is_rejected(self):
        """Antes, uma célula fora de `families` entrava direto em `cells`."""
        payload = {
            "families": {k: list(v) for k, v in FAILURE_FAMILIES.items()},
            "cells": [{"family": "entrada", "item": "item_inventado",
                       "state": "not_applicable", "marked": True,
                       "justification_source": "", "note": ""}],
        }
        with self.assertRaises((MatrixIntegrityError, MatrixJustificationRequired)):
            FailureEdgeMatrix.from_dict(payload)

    def test_profile_sourced_exclusion_roundtrips_only_when_trusted(self):
        m = FailureEdgeMatrix()
        m.exclude_family("mensageria", "sistema sem fila", source="profile:cli")
        again = FailureEdgeMatrix.from_dict(m.to_dict(), trust_profile_cells=True)
        self.assertEqual(again.to_dict(), m.to_dict())
        with self.assertRaises(MatrixIntegrityError):
            FailureEdgeMatrix.from_dict(m.to_dict())  # default: origem não confiável

    def test_profile_sourced_exclusion_without_note_is_rejected(self):
        payload = {
            "cells": [{"family": "entrada", "item": "ausente", "state": "not_applicable",
                       "marked": True, "justification_source": "profile:cli", "note": ""}],
        }
        with self.assertRaises(MatrixJustificationRequired):
            FailureEdgeMatrix.from_dict(payload, trust_profile_cells=True)

    def test_marked_unresolved_without_note_is_rejected(self):
        payload = {"cells": [{"family": "entrada", "item": "ausente",
                              "state": "unresolved", "marked": True, "note": ""}]}
        with self.assertRaises(MatrixIntegrityError):
            FailureEdgeMatrix.from_dict(payload)

    def test_allowed_families_parameter_widens_the_set(self):
        payload = {
            "families": {"fiscal": ["nota_cancelada"]},
            "cells": [{"family": "fiscal", "item": "nota_cancelada", "state": "unresolved"}],
        }
        m = FailureEdgeMatrix.from_dict(payload, allowed_families={"fiscal": ("nota_cancelada",)})
        self.assertIs(m.cell("fiscal", "nota_cancelada").state, MatrixState.UNRESOLVED)

    def test_exclude_family_requires_reason(self):
        m = FailureEdgeMatrix()
        with self.assertRaises(MatrixJustificationRequired):
            m.exclude_family("entrada", "   ", source="profile:cli")

    def test_exclude_unknown_family_rejected(self):
        m = FailureEdgeMatrix()
        with self.assertRaises(MatrixIntegrityError):
            m.exclude_family("telepatia", "motivo", source="profile:cli")

    def test_add_family_rejects_redefinition_and_empty(self):
        m = FailureEdgeMatrix()
        with self.assertRaises(MatrixIntegrityError):
            m.add_family("entrada", ["x"], source="profile:cli")
        with self.assertRaises(MatrixIntegrityError):
            m.add_family("fiscal", [], source="profile:cli")

    def test_mark_clears_profile_provenance(self):
        """Marcar com evidência de código substitui a proveniência do perfil."""
        m = FailureEdgeMatrix()
        m.exclude_family("mensageria", "sem fila", source="profile:cli")
        m.mark("mensageria", "duplicidade", MatrixState.COVERED, _code_evidence())
        self.assertEqual(m.cell("mensageria", "duplicidade").justification_source, "")

    def test_objective_roundtrip_with_profile_matrix(self):
        """`InvestigationObjective.from_dict` sobrevive ao perfil aplicado."""
        prof = AnalysisProfile.from_mapping(
            {"failure_families_excluded": {"mensageria": "sem fila"},
             "failure_families_extra": {"fiscal": ["nota_cancelada"]},
             "contract_exclusions": {"persistencia": "sem banco"}},
            source="cli",
        )
        obj = plan(_capability_map(), _extraction(), profile=prof)[0]
        again = InvestigationObjective.from_dict(obj.to_dict(), trust_profile_cells=True)
        self.assertEqual(again.to_dict(), obj.to_dict())

    def test_integrate_style_payload_with_valid_families_still_works(self):
        """`knowledge/integrate.py` injeta `data["matrix"]` vindo do agente:
        com famílias válidas e justificativa, continua reconstruindo."""
        obj = plan(_capability_map(), _extraction())[0]
        data = obj.to_dict()
        matrix = FailureEdgeMatrix()
        matrix.mark("entrada", "ausente", MatrixState.COVERED, _code_evidence())
        data["matrix"] = matrix.to_dict()
        rebuilt = InvestigationObjective.from_dict(data)
        self.assertIs(rebuilt.matrix.cell("entrada", "ausente").state, MatrixState.COVERED)


# --------------------------------------------------------------------------
# 6b) PROVENIENCIA DE PERFIL FORJADA PELO AGENTE (achado B)
# --------------------------------------------------------------------------


def _agent_payload_forging_profile_cell():
    """O que um worker hostil manda em `output["matrix"]`: `not_applicable`
    sem evidencia nenhuma, blindado por uma string `justification_source`."""
    return {
        "families": {k: list(v) for k, v in FAILURE_FAMILIES.items()},
        "cells": [
            {"family": "entrada", "item": "ausente", "state": "not_applicable",
             "marked": True, "justification_source": "profile:cli",
             "note": "o agente decidiu que nao se aplica"}
        ],
    }


class TestForgedProfileProvenance(unittest.TestCase):
    def test_agent_payload_with_forged_provenance_is_rejected_by_default(self):
        with self.assertRaises(MatrixIntegrityError) as ctx:
            FailureEdgeMatrix.from_dict(_agent_payload_forging_profile_cell())
        self.assertIn("nao aceita desta origem", _strip_accents(str(ctx.exception)))

    def test_same_payload_accepted_when_origin_is_trusted(self):
        m = FailureEdgeMatrix.from_dict(
            _agent_payload_forging_profile_cell(), trust_profile_cells=True
        )
        self.assertIs(m.cell("entrada", "ausente").state, MatrixState.NOT_APPLICABLE)

    def test_objective_from_dict_defaults_to_untrusted(self):
        prof = AnalysisProfile.from_mapping(
            {"failure_families_excluded": {"mensageria": "sem fila"}}, source="cli"
        )
        obj = plan(_capability_map(), _extraction(), profile=prof)[0]
        data = obj.to_dict()
        with self.assertRaises(MatrixIntegrityError):
            InvestigationObjective.from_dict(data)
        self.assertEqual(
            InvestigationObjective.from_dict(data, trust_profile_cells=True).to_dict(), data
        )

    def test_objectives_from_dict_roundtrip_trusted(self):
        """(4) `plan(profile=)` -> `objectives_to_dict` -> `objectives_from_dict` identico."""
        prof = AnalysisProfile.from_mapping(
            {"failure_families_excluded": {"mensageria": "sem fila"},
             "failure_families_extra": {"fiscal": ["nota_cancelada"]},
             "contract_exclusions": {"persistencia": "sem banco"},
             "trigger_priority": {"predicate": 5}},
            source="repo:.wiki-ai.json",
        )
        objs = plan(_capability_map(), _extraction(), profile=prof)
        payload = objectives_to_dict(objs)
        back = objectives_from_dict(payload, trust_profile_cells=True)
        self.assertEqual(
            json.dumps(objectives_to_dict(back), sort_keys=True),
            json.dumps(payload, sort_keys=True),
        )
        with self.assertRaises(MatrixIntegrityError):
            objectives_from_dict(payload)

    def test_default_profile_payload_needs_no_trust(self):
        """Regressao: sem perfil nao ha celula de proveniencia, logo nada muda."""
        payload = objectives_to_dict(plan(_capability_map(), _extraction()))
        back = objectives_from_dict(payload)  # default False, sem erro
        self.assertEqual(
            json.dumps(objectives_to_dict(back), sort_keys=True),
            json.dumps(payload, sort_keys=True),
        )

    # -- profile_cells / reapply_profile_cells -----------------------------
    def _prior(self):
        m = FailureEdgeMatrix()
        m.add_family("fiscal", ["nota_cancelada"], source="profile:cli")
        m.exclude_family("mensageria", "sistema sem fila", source="profile:cli")
        return m

    def test_profile_cells_lists_exactly_the_profile_provenance(self):
        prior = self._prior()
        expected = {("fiscal", "nota_cancelada")} | {
            ("mensageria", i) for i in FAILURE_FAMILIES["mensageria"]
        }
        self.assertEqual(prior.profile_cells(), frozenset(expected))
        self.assertEqual(FailureEdgeMatrix().profile_cells(), frozenset())

    def test_reapply_restores_cell_the_agent_removed(self):
        prior = self._prior()
        agent = FailureEdgeMatrix()  # agente devolveu a matriz sem a familia extra
        agent.mark("entrada", "ausente", MatrixState.COVERED, _code_evidence())
        report = agent.reapply_profile_cells(prior)

        self.assertIn("fiscal/nota_cancelada", report["restored"])
        self.assertEqual(
            agent.cell("fiscal", "nota_cancelada").justification_source, "profile:cli"
        )
        self.assertEqual(agent.families["fiscal"], ("nota_cancelada",))
        for item in FAILURE_FAMILIES["mensageria"]:
            cell = agent.cell("mensageria", item)
            self.assertIs(cell.state, MatrixState.NOT_APPLICABLE)
            self.assertEqual(cell.note, "sistema sem fila")

    def test_reapply_restores_cell_the_agent_altered(self):
        prior = self._prior()
        agent = FailureEdgeMatrix.from_dict(prior.to_dict(), trust_profile_cells=True)
        agent.mark("mensageria", "duplicidade", MatrixState.COVERED, _code_evidence())
        report = agent.reapply_profile_cells(prior)
        self.assertIn("mensageria/duplicidade", report["restored"])
        self.assertEqual(
            agent.cell("mensageria", "duplicidade").to_dict(),
            prior.cell("mensageria", "duplicidade").to_dict(),
        )

    def test_reapply_does_not_touch_other_cells(self):
        prior = self._prior()
        agent = FailureEdgeMatrix.from_dict(prior.to_dict(), trust_profile_cells=True)
        agent.mark("entrada", "ausente", MatrixState.COVERED, _code_evidence())
        agent.mark("negocio", "regra_conflitante", MatrixState.UNRESOLVED, note="impacto X")
        before_entrada = agent.cell("entrada", "ausente").to_dict()
        before_negocio = agent.cell("negocio", "regra_conflitante").to_dict()

        report = agent.reapply_profile_cells(prior)

        self.assertEqual(report["restored"], [])
        self.assertEqual(report["revoked_forged_provenance"], [])
        self.assertEqual(agent.cell("entrada", "ausente").to_dict(), before_entrada)
        self.assertEqual(agent.cell("negocio", "regra_conflitante").to_dict(), before_negocio)

    def test_reapply_revokes_provenance_the_agent_invented(self):
        """O agente nao pode CRIAR celula de perfil - nem com trust=True a jusante."""
        prior = FailureEdgeMatrix()  # perfil default: nenhuma celula de perfil
        agent = FailureEdgeMatrix.from_dict(
            _agent_payload_forging_profile_cell(), trust_profile_cells=True
        )
        report = agent.reapply_profile_cells(prior)
        self.assertEqual(report["revoked_forged_provenance"], ["entrada/ausente"])
        cell = agent.cell("entrada", "ausente")
        self.assertIs(cell.state, MatrixState.UNRESOLVED)
        self.assertFalse(cell.marked)
        self.assertEqual(cell.justification_source, "")

    def test_reapply_output_survives_untrusted_reconstruction(self):
        """Depois de reimpor, a matriz volta a ser payload legitimo do pipeline."""
        prior = self._prior()
        agent = FailureEdgeMatrix()
        agent.reapply_profile_cells(prior)
        again = FailureEdgeMatrix.from_dict(agent.to_dict(), trust_profile_cells=True)
        self.assertEqual(again.profile_cells(), prior.profile_cells())
        with self.assertRaises(MatrixIntegrityError):
            FailureEdgeMatrix.from_dict(agent.to_dict())

    def test_integrator_sequence_end_to_end(self):
        """A sequencia exata que `knowledge/integrate.py` precisa usar."""
        prof = AnalysisProfile.from_mapping(
            {"failure_families_excluded": {"mensageria": "sem fila"}}, source="cli"
        )
        objective = plan(_capability_map(), _extraction(), profile=prof)[0]
        prior = objective.matrix

        # payload hostil nao passa da porta
        with self.assertRaises(MatrixIntegrityError):
            FailureEdgeMatrix.from_dict(_agent_payload_forging_profile_cell())

        # agente honesto: marca com evidencia de codigo, sem proveniencia forjada
        honest = FailureEdgeMatrix()
        honest.mark("entrada", "ausente", MatrixState.COVERED, _code_evidence())
        agent_matrix = FailureEdgeMatrix.from_dict(honest.to_dict())
        agent_matrix.reapply_profile_cells(prior)

        data = objective.to_dict()
        data["matrix"] = agent_matrix.to_dict()
        rebuilt = InvestigationObjective.from_dict(data, trust_profile_cells=True)
        self.assertIs(rebuilt.matrix.cell("entrada", "ausente").state, MatrixState.COVERED)
        self.assertIs(
            rebuilt.matrix.cell("mensageria", "duplicidade").state, MatrixState.NOT_APPLICABLE
        )
        self.assertEqual(rebuilt.matrix.profile_cells(), prior.profile_cells())


# --------------------------------------------------------------------------
# 7) extractors: extensões por parâmetro e por env
# --------------------------------------------------------------------------


from analysis.extractors.base import CodeExtractor as _CodeExtractor


class _FakeExtractor(_CodeExtractor):
    """Extrator de terceiro: mínimo viável para provar o hook de extensão."""

    language = "cobol"
    extensions = (".cbl",)

    def detect(self, path, content=""):
        return path.lower().endswith(".cbl")

    def symbols(self, path, content):
        return []

    def references(self, path, content):
        return []

    def entrypoints(self, path, content):
        return []

    def configuration(self, path, content):
        return []


def make_fake_extractor():
    return _FakeExtractor()


def make_two_fake_extractors():
    return [_FakeExtractor(), _FakeExtractor()]


def broken_factory():
    raise RuntimeError("fábrica quebrada de propósito")


class TestExtractorExtensions(unittest.TestCase):
    def setUp(self):
        self.assertIn(__name__, sys.modules)  # spec resolvível por importlib
        self._spec = f"{__name__}:make_fake_extractor"
        self._old_env = os.environ.get(EXTRACTOR_EXTENSIONS_ENV)
        os.environ.pop(EXTRACTOR_EXTENSIONS_ENV, None)

    def tearDown(self):
        if self._old_env is None:
            os.environ.pop(EXTRACTOR_EXTENSIONS_ENV, None)
        else:
            os.environ[EXTRACTOR_EXTENSIONS_ENV] = self._old_env

    def test_default_registry_unchanged(self):
        reg = default_registry()
        self.assertEqual(len(reg.extractors), 9)
        self.assertEqual(default_registry(extensions=()).languages(), reg.languages())

    def test_extension_by_parameter(self):
        reg = default_registry(extensions=[self._spec])
        self.assertIn("cobol", reg.languages())
        self.assertEqual(len(reg.extractors), 10)

    def test_extension_by_env(self):
        os.environ[EXTRACTOR_EXTENSIONS_ENV] = self._spec
        reg = default_registry()
        self.assertIn("cobol", reg.languages())

    def test_parameter_wins_over_env(self):
        os.environ[EXTRACTOR_EXTENSIONS_ENV] = f"{__name__}:broken_factory"
        reg = default_registry(extensions=[self._spec])  # env não é lido
        self.assertIn("cobol", reg.languages())

    def test_factory_returning_sequence(self):
        reg = default_registry(extensions=[f"{__name__}:make_two_fake_extractors"])
        self.assertEqual(len(reg.extractors), 11)

    def test_builtin_precedence_preserved(self):
        reg = default_registry(extensions=[self._spec])
        self.assertEqual(reg.for_path("x.py").language, "python")
        self.assertEqual(reg.for_path("x.cbl").language, "cobol")

    def test_malformed_spec_names_the_extension(self):
        with self.assertRaises(ExtractorExtensionError) as ctx:
            default_registry(extensions=["sem_dois_pontos"])
        self.assertIn("sem_dois_pontos", str(ctx.exception))

    def test_import_failure_names_the_extension(self):
        with self.assertRaises(ExtractorExtensionError) as ctx:
            default_registry(extensions=["pacote.que.nao.existe:fabrica"])
        self.assertIn("pacote.que.nao.existe:fabrica", str(ctx.exception))

    def test_broken_factory_names_the_extension(self):
        spec = f"{__name__}:broken_factory"
        with self.assertRaises(ExtractorExtensionError) as ctx:
            default_registry(extensions=[spec])
        self.assertIn(spec, str(ctx.exception))

    def test_non_extractor_return_is_rejected(self):
        with self.assertRaises(ExtractorExtensionError):
            load_extractor_extensions(default_registry(), [f"{__name__}:_not_an_extractor"])

    def test_no_adapter_diagnostic_still_visible(self):
        """Extensão que não cobre a linguagem NÃO apaga a lacuna."""
        from analysis.extractors.base import SourceFile

        reg = default_registry(extensions=[self._spec])
        result = reg.extract_all([SourceFile(path="a.rs", content="fn main() {}", language="rust")])
        codes = {d.code for d in result.diagnostics}
        self.assertIn("no_adapter", codes)
        result.assert_accounted()


def _not_an_extractor():
    return object()


if __name__ == "__main__":
    unittest.main()
