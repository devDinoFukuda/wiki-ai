"""Resultado aceito de tarefa -> fato verificado em `knowledge.db` (achado nº1).

O buraco que este módulo fecha
------------------------------
`runtime.coordinator.accept_result` valida o envelope e deixa a saída do worker
em `tasks.result_json`. E parava ali: nada lia aquele JSON, nada o confrontava
com o código, nada o gravava. Uma investigação podia descobrir "total > 1000
devolve 409", a tarefa terminava `done` — e a regra não existia em
`knowledge.db`, logo não existia na consulta nem na publicação.

O caminho agora é executável e tem três portas, nesta ordem:

| Passo | Função | O que garante |
|---|---|---|
| ler | `collect_results` | só tarefa `done` de investigação/verificação, com resultado; recusada nunca entra |
| confrontar | `results_to_claims` + `analysis.verification.verify_claim` | a afirmação é confrontada com o TRECHO real do snapshot |
| gravar | `integrate` | uma revisão atômica, com o `epistemic` do VEREDITO — nunca o que o worker disse de si |

Invariantes em código, não em prosa
-----------------------------------
1. **O worker nunca se aprova.** `asserted_by` é o worker (`llm:...`);
   `support_recorded_by` só é preenchido a partir de `Verdict.support_recorded_by`
   (`pipeline:verification`). `knowledge.repository._check_support` rejeitaria os
   dois iguais, e `verify_claim` já rebaixa antes disso.
2. **`implemented` exige veredito `supported` sobre evidência executável.**
   `_nature_for` só devolve `FactNature.IMPLEMENTED` quando o veredito é
   `SUPPORTED` e a natureza do próprio veredito é `IMPLEMENTED` — que
   `verify_claim` só emite com `LocationResult.executable`. Afirmação não
   sustentada vira `declared_requirement`/`test_expectation`, que
   `knowledge.query._bucket_of` mantém FORA de `implemented_current`.
3. **Citação que contradiz vira `disputed`, não silêncio.** Vem inteiro de
   `check_support`/`check_consistency`; aqui só se preserva o veredito.
4. **Afirmação sem evidência nunca é `supported`.** Sem `evidence_refs` o
   veredito é `unresolved`, o fato é gravado assim mesmo e a lacuna entra no
   relatório: desconhecido explícito, não omissão (§6.6).
5. **Reintegrar não duplica.** `claim_id` é conteúdo (`objetivo:campo:hash do
   enunciado`), `fact_id` deriva de `(namespace, subject, predicate, scope)` e
   `evidence_id` do localizador. Mesmo resultado ⇒ mesmo `content_hash` ⇒
   `WriteResult.changed=False`.
6. **`partial` nunca vira `complete`.** O estado é RECALCULADO por
   `InvestigationObjective.evaluate()` a partir do contrato preenchido pelo
   resultado; o `state` que o worker declarou só é honrado quando ele PIORA o
   estado (`blocked`).
7. **A frase inteira vale pelo componente MAIS FRACO.** Uma frase condicional
   tem condição *e* consequência; aprovar a condição e gravar a frase toda como
   sustentada é o achado nº1 da 2ª auditoria ("total > 1000 é aprovado com HTTP
   200" virava `supported` porque `total > 1000` existe no código que devolve
   409). `_consequence_of` extrai a consequência, `_consequence_guard` exige que
   ela seja confirmada MECANICAMENTE no trecho, e rebaixa (`inferred`) ou
   contradiz (`disputed`) a frase inteira quando não é.
7b. **Condição e efeito precisam estar no MESMO RAMO.** Confirmar a consequência
   no trecho não bastava: em `if total > 1000: return 409` seguido de
   `return 200`, "quando total > 1000 retorna 200" citando a função inteira
   achava o `200` — no ramo errado — e saía `supported` (achado nº1 da 3ª
   auditoria). `_branch_association` localiza no `ast` o `if` cujo teste é a
   condição afirmada (inversão de operandos conta) e só confirma o efeito dentro
   daquele ramo; efeito no caminho oposto não confirma, ramo com efeito
   incompatível vira `disputed`, e condição não localizável ou trecho não-Python
   têm teto `inferred`.
8. **Integração é ESCOPADA.** `integrate` exige `objectives`/`objective_ids` e
   descarta (em `IntegrationReport.descartados`) resultado de objetivo fora do
   escopo ou cujas entradas não pertencem ao snapshot corrente. Dois repositórios
   no mesmo store deixaram de se contaminar: sem escopo, `collect_results`
   devolvia toda tarefa `done` do runtime.db, inclusive as do outro repositório.
9. **Fato nunca aponta entidade de outro namespace.** `_capability_subject` e
   `_write_claim` conferem o namespace da entidade ANTES de gravar; citação cujo
   caminho não existe no snapshot corrente rejeita o claim com motivo.
10. **Evidência é CÓDIGO.** `_gate_rejection` recusa, com código estável em
   `rejeitados[*]["tipo"]`, citação para documento ou para faixa que só tem
   comentário/docstring (`evidencia_nao_codigo`), citação cuja faixa não
   resolve (`evidencia_nao_resolvida`) e citação de fora do snapshot
   (`evidencia_fora_do_snapshot`). `is_comment_only` cobre família por família
   (C-like, Python, `#`, SQL/Sybase, COBOL coluna 7 e `*>`, JCL `//*`, XML) e
   cai em `generic` no desconhecido — a análise precisa valer para mainframe,
   Java 7-25, Go, C++ e Rust, e o comentário de cada uma tem forma diferente.
11. **Descoberta é afirmação do agente, nunca verificação.** Em objetivo
   `ObjectiveKind.DISCOVERY` (módulo sem extrator) não há gramática extraída
   contra a qual conferir: `_cap_discovery_verdict` rebaixa qualquer
   `supported` para `inferred`, a entidade nasce `LifecycleStatus.PROPOSED`
   com `asserted_by` do agente nos atributos, e só nasce se houver citação de
   código RESOLVIDA (`_support_evidence_ids`). Claim de descoberta sem
   nenhuma citação é recusado — ali, afirmação sem path/linhas é opinião.

O que este módulo NÃO faz
-------------------------
Não invoca LLM, não escreve em `runtime.db` (só lê), não publica e não fia
nenhum comando: `integrate` devolve `IntegrationReport` serializável e quem
chama (o CLI) decide o que fazer com ele. Também não inventa entidade de
capacidade quando ela já existe — só cria a que falta, para que reintegração
sobre base já povoada por `wk analyze` não gere revisão de título.

Imports: stdlib + `knowledge` + `analysis` (verification/snapshot/investigation)
+ `runtime.tasks` (leitura). Sem `wk`, `codescan`, `publishing` ou `sbindex`.
"""

from __future__ import annotations

import ast
import hashlib
import os
import re
import unicodedata
from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Mapping, Sequence

from analysis.investigation import (
    CONTRACT_FIELDS,
    CONTRACT_LABELS,
    ContractField,
    ContractFieldStatus,
    InvestigationError,
    InvestigationObjective,
    ObjectiveState,
)
from analysis.snapshot import Snapshot
from analysis.verification import (
    SUPPORT_RECORDER,
    Claim,
    ClaimEvidence,
    Inconsistency,
    LocationResult,
    Verdict,
    VerificationError,
    apply_inconsistencies,
    check_consistency,
    check_location,
    extract_effects,
    reread_obligations,
    verification_summary,
    verify_claim,
)
# Reuso deliberado das PRIMITIVAS de `verification` (nunca reimplementadas aqui):
# `_parse_python` é o mesmo parser defensivo (dedent + wrap em função/classe) que
# `extract_comparisons`/`extract_effects` usam, e `_operand_equal` é a MESMA
# igualdade de operandos que `check_support` aplica (`self.x` ≡ `x`, `0` ≡ `0.0`).
# Duplicar qualquer uma faria a associação condição→ramo divergir da checagem que
# aprovou a condição — que é justamente o buraco do achado nº1.
from analysis.verification import _operand_equal as _operand_equal
from analysis.verification import _parse_python as _parse_python_fragment
from runtime import tasks as rt_tasks

from . import evidence as ev_mod
from . import identity as id_mod
from .models import (
    ApprovalState,
    ContentKind,
    EntityDraft,
    EntityType,
    EpistemicStatus,
    Evidence,
    FactDraft,
    FactNature,
    KnowledgeError,
    LifecycleStatus,
    RelationDraft,
    RelationType,
    SourceKind,
)

__all__ = [
    "DISCOVERY_KIND",
    "DOC_EXTENSIONS",
    "FIELD_PREDICATE_KIND",
    "INTEGRATOR",
    "KNOWN_OUTPUT_FIELDS",
    "REJECT_EVIDENCE_NOT_CODE",
    "REJECT_EVIDENCE_UNRESOLVED",
    "REJECT_EVIDENCE_OUT_OF_SNAPSHOT",
    "UNFORESEEN_PREDICATE_KIND",
    "RULE_ENTITY_TYPE",
    "AcceptedResult",
    "FactWrite",
    "IntegrationReport",
    "ObjectiveOutcome",
    "collect_results",
    "integrate",
    "is_comment_only",
    "results_to_claims",
    "strip_comments",
    "unforeseen_contract_fields",
]


#: `ObjectiveKind.DISCOVERY.value` (`analysis.investigation`). Comparado como
#: STRING de propósito: este módulo já reconstrói o objetivo com
#: `InvestigationObjective.from_dict`, e ler `.value` mantém a comparação
#: estável mesmo quando o objetivo chega como dicionário cru (payload de
#: resultado, fixture de teste) e não como instância da enum.
DISCOVERY_KIND = "discovery"

#: Códigos ESTÁVEIS de rejeição de claim por evidência. Ficam em
#: `ObjectiveOutcome.rejeitados[*]["tipo"]` para que o CLI possa contar e
#: agrupar sem reconhecer texto em português.
REJECT_EVIDENCE_UNRESOLVED = "evidencia_nao_resolvida"
REJECT_EVIDENCE_NOT_CODE = "evidencia_nao_codigo"
REJECT_EVIDENCE_OUT_OF_SNAPSHOT = "evidencia_fora_do_snapshot"

#: Extensões de DOCUMENTO. Citação para um destes arquivos nunca sustenta
#: afirmação sobre o sistema (§5.4, F10): documento é intenção declarada, não
#: comportamento implementado. Espelha `analysis.inventory._DOC_EXTENSIONS`
#: (que classifica `FileClass.DOC`) e a acrescenta — a duplicação é deliberada
#: para não importar `inventory` aqui; a divergência é detectável por teste.
DOC_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".md", ".markdown", ".mdx", ".rst", ".adoc", ".asciidoc", ".txt", ".text",
        ".rtf", ".docx", ".doc", ".pdf", ".org", ".wiki", ".textile", ".tex",
    }
)


#: Autoria da REVISÃO e de qualquer mudança de natureza. Origem `pipeline`
#: (exigida por `repository._check_nature_transition`) e distinta de
#: `SUPPORT_RECORDER`: quem integra não é quem registra sustentação.
INTEGRATOR = "pipeline:knowledge-integrate"

#: `asserted_by` de fallback quando nem a tarefa nem o chamador dizem qual
#: worker afirmou. Prefixo `llm:` deliberado: na dúvida, a afirmação é tratada
#: como saída de modelo — o caminho mais restritivo (§5.3).
DEFAULT_ASSERTED_BY = "llm:worker"

#: Campos de TOPO do resultado do worker que o runtime já declara em
#: `coordinator.DEFAULT_SCHEMA` (required + optional). Serve para uma coisa só:
#: separar o vocabulário FIXO das chaves EXTRAS que o operador autorizou por
#: perfil (`ResultSchema.with_extra`). Tudo que chega no topo do resultado e
#: não está aqui é extensão de perfil e vai INTEIRO para
#: `ObjectiveOutcome.extra` — descartar seria a mesma perda silenciosa que
#: `campos_nao_previstos` corrige dentro de `contract`.
#:
#: A cópia (em vez de importar `runtime.coordinator`) mantém a fronteira de
#: imports deste módulo — stdlib + knowledge + analysis + `runtime.tasks`. A
#: divergência entre as duas listas é DETECTÁVEL: há teste que compara este
#: conjunto com `coordinator.DEFAULT_SCHEMA.declared`.
KNOWN_OUTPUT_FIELDS: frozenset[str] = frozenset(
    {
        "objective_id",
        "capability_id",
        "contract",
        "evidence",
        "facts",
        "gaps",
        "matrix",
        "notes",
        "reading_needs",
        "reading_satisfied",
        "relations",
        "state",
        "unresolved",
    }
)


#: §6.3 -> `analysis.verification.PREDICATE_KINDS`. `lacunas` não aparece: um
#: ponto não resolvido é lacuna declarada, nunca afirmação a verificar.
FIELD_PREDICATE_KIND: Mapping[str, str] = {
    "identidade": "contract",
    "gatilho": "contract",
    "precondicoes": "behavior",
    "dados": "data",
    "decisoes": "behavior",
    "persistencia": "behavior",
    "integracoes": "contract",
    "sucesso": "behavior",
    "falhas": "behavior",
    "edge_cases": "behavior",
    "dependencias": "contract",
    "verificacao": "test",
}

#: Campo do contrato -> classe de entidade quando a afirmação traz assunto
#: NOMEADO. Regra de negócio e fluxo são as duas classes do §5.1 que uma
#: investigação de comportamento pode legitimamente criar; o resto continua
#: preso à Capability.
RULE_ENTITY_TYPE: Mapping[str, EntityType] = {
    "decisoes": EntityType.BUSINESS_RULE,
    "falhas": EntityType.BUSINESS_RULE,
    "precondicoes": EntityType.BUSINESS_RULE,
    "edge_cases": EntityType.BUSINESS_RULE,
    "gatilho": EntityType.FLOW,
    "sucesso": EntityType.FLOW,
}

#: Prefixo do `predicate` gravado. Fixo para que a consulta padrão possa
#: separar o que veio de investigação do que veio da extração estrutural.
PREDICATE_PREFIX = "investigacao"

_TASK_KINDS = (rt_tasks.TaskKind.INVESTIGATION, rt_tasks.TaskKind.VERIFICATION)

#: Chaves aceitas como lista de afirmações dentro de um campo do contrato.
_ASSERTION_KEYS = ("afirmacoes", "afirmações", "assertions", "statements", "itens", "items")

#: Chaves aceitas como nome do assunto/regra dentro de uma afirmação.
_NAME_KEYS = ("rule", "regra", "nome", "name", "assunto", "subject", "titulo", "título")

#: Chaves aceitas como enunciado dentro de uma afirmação.
_STATEMENT_KEYS = ("statement", "afirmacao", "afirmação", "texto", "text", "content", "descricao", "descrição")

#: Chaves aceitas como lista de evidências dentro de uma afirmação/campo.
_EVIDENCE_KEYS = ("evidence_refs", "evidencias", "evidências", "evidence", "refs")

_STATEMENT_FIELD_ALIASES: Mapping[str, str] = {
    "condition": "condition",
    "condicao": "condition",
    "condição": "condition",
    "comparator": "comparator",
    "comparador": "comparator",
    "operator": "comparator",
    "operador": "comparator",
    "value": "value",
    "valor": "value",
    "effect": "effect",
    "efeito": "effect",
    "exception": "exception",
    "excecao": "exception",
    "exceção": "exception",
    "erro": "exception",
    "negated": "negated",
    "negado": "negated",
    "literals": "literals",
    "literais": "literals",
    "after": "after",
    "apos": "after",
    "após": "after",
    "exception_to": "exception_to",
    "excecao_de": "exception_to",
}

_CMP_SYMBOL = r"(>=|<=|===|!==|==|!=|<>|=|>|<)"
_TEXT_CMP_SYMBOLIC = re.compile(
    r"([A-Za-z_][A-Za-z_0-9]*(?:\.[A-Za-z_][A-Za-z_0-9]*)*)\s*" + _CMP_SYMBOL + r"\s*"
    r"([-+]?(?:\d[\d_]*(?:\.\d+)?|'[^']*'|\"[^\"]*\"|[A-Za-z_][A-Za-z_0-9]*"
    r"(?:\.[A-Za-z_][A-Za-z_0-9]*)*))"
)
_TEXT_CMP_WORDS = re.compile(
    r"([A-Za-z_][A-Za-z_0-9]*(?:\.[A-Za-z_][A-Za-z_0-9]*)*)\s+"
    r"(maior ou igual a|menor ou igual a|maior que|menor que|igual a|diferente de|acima de|abaixo de)\s+"
    r"([-+]?(?:\d[\d_]*(?:\.\d+)?|'[^']*'|\"[^\"]*\"|[A-Za-z_][A-Za-z_0-9]*))",
    re.IGNORECASE,
)
_WORD_COMPARATOR: Mapping[str, str] = {
    "maior ou igual a": ">=",
    "menor ou igual a": "<=",
    "maior que": ">",
    "acima de": ">",
    "menor que": "<",
    "abaixo de": "<",
    "igual a": "==",
    "diferente de": "!=",
}
_TEXT_RAISE = re.compile(
    r"(?:raise[sd]?|throw[sn]?|levanta|lan[cç]a)\s+([^\W\d]\w*(?:\.[^\W\d]\w*)*)",
    re.IGNORECASE,
)
_EXC_NAME = re.compile(r"\b([A-Z]\w*(?:Error|Exception|Fault))\b")

#: Verbos que abrem a CONSEQUÊNCIA de uma frase (o "então" do "se ... então").
#: Chaves em minúsculas SEM acento (o texto é dobrado por `_accent_fold` antes
#: da busca, porque o worker escreve tanto "é aprovado" quanto "e aprovado").
#: Valor: `(efeito observável que confirmaria a consequência, polaridade)`.
#: A polaridade só serve para DESCREVER divergência: consequência positiva
#: ("aprovado") contra trecho que levanta exceção nomeada é contradição.
_CONSEQUENCE_VERBS: Mapping[str, tuple[str, str]] = {
    "devolve": ("return", "neutral"), "devolvem": ("return", "neutral"),
    "devolver": ("return", "neutral"), "devolvido": ("return", "neutral"),
    "retorna": ("return", "neutral"), "retornam": ("return", "neutral"),
    "retornar": ("return", "neutral"), "retornado": ("return", "neutral"),
    "responde": ("return", "neutral"), "responde com": ("return", "neutral"),
    "return": ("return", "neutral"), "returns": ("return", "neutral"),
    "aprova": ("return", "positive"), "aprovado": ("return", "positive"),
    "aprovada": ("return", "positive"), "aceito": ("return", "positive"),
    "aceita": ("return", "positive"), "autoriza": ("return", "positive"),
    "autorizado": ("return", "positive"), "permitido": ("return", "positive"),
    "rejeita": ("raise", "negative"), "rejeitado": ("raise", "negative"),
    "rejeitada": ("raise", "negative"), "recusa": ("raise", "negative"),
    "recusado": ("raise", "negative"), "nega": ("raise", "negative"),
    "negado": ("raise", "negative"), "bloqueia": ("raise", "negative"),
    "bloqueado": ("raise", "negative"), "impede": ("raise", "negative"),
    "falha": ("raise", "negative"), "cancelado": ("state", "negative"),
    "lanca": ("raise", "negative"), "lança": ("raise", "negative"),
    "levanta": ("raise", "negative"), "dispara": ("raise", "negative"),
    "raise": ("raise", "negative"), "raises": ("raise", "negative"),
    "throw": ("raise", "negative"), "throws": ("raise", "negative"),
    "notifica": ("publish", "neutral"), "notificar": ("publish", "neutral"),
    "avisa": ("publish", "neutral"), "envia": ("publish", "neutral"),
    "publica": ("publish", "neutral"), "emite": ("publish", "neutral"),
    "grava": ("write", "neutral"), "salva": ("write", "neutral"),
    "persiste": ("write", "neutral"), "registra": ("write", "neutral"),
    "atualiza": ("write", "neutral"),
    "vira": ("state", "neutral"), "muda para": ("state", "neutral"),
    "passa a": ("state", "neutral"), "torna-se": ("state", "neutral"),
    "fica": ("state", "neutral"), "marcado como": ("state", "neutral"),
}
_CONSEQUENCE_VERB_RE = re.compile(
    r"(?<![A-Za-z0-9_])("
    + "|".join(re.escape(v) for v in sorted(_CONSEQUENCE_VERBS, key=len, reverse=True))
    + r")(?![A-Za-z0-9_])",
    re.IGNORECASE,
)
#: Código de status (HTTP e afins) citado na consequência. Literal mecânico por
#: excelência: ou o número está no trecho, ou não está.
_STATUS_CODE = re.compile(r"(?<![\w.])([1-5]\d{2})(?![\w.])")
_STATUS_WORD = re.compile(r"\b(?:HTTP|status|c[oó]digo|code)\b\s*[:=]?\s*(\d{3})\b", re.IGNORECASE)
#: Nome de estado citado (`APROVADO`, `PENDING_REVIEW`) ou literal entre aspas.
_STATE_TOKEN = re.compile(r"(?<![\w])([A-Z][A-Z0-9]{2,}(?:_[A-Z0-9]+)*)(?![\w])")
_QUOTED = re.compile(r"'([^']{1,60})'|\"([^\"]{1,60})\"")
#: Fim da cláusula de consequência: outra oração começa.
_CLAUSE_END = re.compile(r"\s+(?:exceto|salvo|a menos que|caso contrario|porem|mas)\b|[;.]")
_NAMED_PREFIX = re.compile(r"^\s*([^:\n]{3,80}?)\s*:\s*(\S.*)$", re.DOTALL)
_BULLET = re.compile(r"^\s*(?:[-*•—]|\d+[.)])\s*")
_ASSERT_LINE = re.compile(r"\b(assert|expect|should|it\(|test)", re.IGNORECASE)


# --------------------------------------------------------------------------
# Evidencia e CODIGO — nunca documento, comentario ou docstring (§5.4, F10)
# --------------------------------------------------------------------------
#
# A regra nao e estilistica: um comentario afirma o que alguem QUIS que o
# codigo fizesse; so o codigo executavel afirma o que ele FAZ. Aceitar
# comentario como sustentacao transformaria a intencao do autor em fato
# verificado — que e exatamente o erro que o §5.4 nomeia.
#
# `analysis.verification.classify_content_kind` ja detecta parte disso
# (extensao de documento, linhas todas iniciadas por `#`/`//`, docstring
# Python, bloco iniciado por `/*`). O que falta, e mora aqui, e a cobertura
# LANGUAGE-AGNOSTIC por familia de sintaxe — necessaria porque a analise
# precisa valer para mainframe (COBOL, JCL), Sybase/T-SQL, Java 7 a 25, Go,
# C++, Rust e o que vier: o comentario de cada uma tem forma diferente, e uma
# citacao inteiramente comentada nao pode passar so porque a linguagem nao
# estava numa lista de quatro nomes.
#
# O criterio e MECANICO: remove-se comentario e literal de documentacao; se o
# que sobra e vazio, a citacao nao e codigo.

#: Familia de sintaxe -> como o comentario se escreve nela.
#: `line`: prefixos de comentario ate o fim da linha.
#: `block`: pares (abre, fecha).
#: `strings`: delimitadores de literal — existem para que `"http://x"` e
#: `'--'` NAO sejam lidos como inicio de comentario (o erro classico deste
#: tipo de varredura).
#: `triple`: literal triplo do Python, que tambem serve de docstring.
_COMMENT_FAMILIES: Mapping[str, Mapping[str, Any]] = {
    # C, C++, C#, Java, Go, Rust, Kotlin, Scala, Swift, JS/TS, PHP, Dart...
    "c_like": {"line": ("//",), "block": (("/*", "*/"),), "strings": ('"', "'", "`"), "triple": False},
    "python": {"line": ("#",), "block": (), "strings": ('"', "'"), "triple": True},
    # Shell, Ruby, Perl, YAML, TOML, R, Makefile, Terraform, PowerShell...
    "hash": {"line": ("#",), "block": (("<#", "#>"),), "strings": ('"', "'"), "triple": False},
    # SQL ANSI, T-SQL/Sybase, PL/SQL, MySQL.
    "sql": {"line": ("--", "#"), "block": (("/*", "*/"),), "strings": ("'", '"'), "triple": False},
    # COBOL: `*` (ou `/`) na COLUNA 7 no formato fixo; `*>` no formato livre.
    "cobol": {"line": ("*>",), "block": (), "strings": ("'", '"'), "triple": False, "column7": True},
    # JCL: `//*` e comentario; `/*` e fim de fluxo de dados (tambem nao e regra).
    "jcl": {"line": ("//*", "/*"), "block": (), "strings": ("'",), "triple": False},
    "xml": {"line": (), "block": (("<!--", "-->"),), "strings": ('"', "'"), "triple": False},
    "lisp": {"line": (";",), "block": (("#|", "|#"),), "strings": ('"',), "triple": False},
    # Desconhecida: UNIAO das formas. E o lado seguro — reconhecer comentario a
    # mais so REJEITA evidencia (lacuna declarada); reconhecer de menos
    # aceitaria comentario como sustentacao, que e o dano irreversivel.
    "generic": {
        "line": ("//", "#", "--", ";", "*>", "%", "!"),
        "block": (("/*", "*/"), ("<!--", "-->")),
        "strings": ('"', "'", "`"),
        "triple": True,
    },
}

#: Extensao -> familia de comentario.
_EXT_COMMENT_FAMILY: Mapping[str, str] = {
    **{e: "c_like" for e in (
        ".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hh", ".hxx", ".cs", ".java",
        ".go", ".rs", ".kt", ".kts", ".scala", ".swift", ".js", ".jsx", ".mjs",
        ".cjs", ".ts", ".tsx", ".php", ".m", ".mm", ".groovy", ".dart", ".proto",
        ".gradle", ".json5", ".sol", ".vala", ".pas", ".d",
    )},
    **{e: "python" for e in (".py", ".pyw", ".pyi")},
    **{e: "hash" for e in (
        ".sh", ".bash", ".zsh", ".ksh", ".rb", ".rake", ".pl", ".pm", ".yaml",
        ".yml", ".toml", ".r", ".tf", ".tfvars", ".ps1", ".psm1", ".cmake",
        ".mk", ".dockerfile", ".ini", ".cfg", ".conf", ".properties", ".nim",
    )},
    **{e: "sql" for e in (".sql", ".pls", ".plsql", ".tsql", ".ddl", ".dml", ".pkb", ".pks")},
    **{e: "cobol" for e in (".cob", ".cbl", ".cpy", ".cobol", ".ccp", ".cblle")},
    **{e: "jcl" for e in (".jcl", ".prc", ".proc")},
    **{e: "xml" for e in (".xml", ".xsd", ".xsl", ".xslt", ".html", ".htm", ".svg", ".wsdl")},
    **{e: "lisp" for e in (".lisp", ".el", ".clj", ".cljs", ".scm", ".rkt")},
}

#: Nome de linguagem -> familia (o `language` que `LocationResult` carrega, e o
#: que `analysis.inventory.Language` usa).
_NAME_COMMENT_FAMILY: Mapping[str, str] = {
    "python": "python", "java": "c_like", "javascript": "c_like",
    "typescript": "c_like", "go": "c_like", "golang": "c_like", "c": "c_like",
    "cpp": "c_like", "c++": "c_like", "csharp": "c_like", "c#": "c_like",
    "rust": "c_like", "kotlin": "c_like", "scala": "c_like", "swift": "c_like",
    "php": "c_like", "dart": "c_like", "groovy": "c_like",
    "shell": "hash", "bash": "hash", "ruby": "hash", "perl": "hash",
    "yaml": "hash", "toml": "hash", "r": "hash", "terraform": "hash",
    "powershell": "hash", "dockerfile": "hash", "makefile": "hash",
    "sql": "sql", "tsql": "sql", "plsql": "sql", "sybase": "sql",
    "cobol": "cobol", "jcl": "jcl",
    "xml": "xml", "html": "xml",
    "lisp": "lisp", "clojure": "lisp", "elisp": "lisp",
}

#: Abertura de literal triplo do Python, com prefixo (`r`, `b`, `f`, `rb`...).
_TRIPLE_OPEN = re.compile(r'[rRbBuUfF]{0,2}("""|\'\'\')')


def _comment_family(language: str) -> str:
    """Familia de sintaxe de comentario de uma linguagem, extensao ou caminho.

    Aceita as tres formas porque quem chama tem uma delas em maos:
    `LocationResult.language` (nome), o caminho citado (extensao) ou a propria
    familia. Desconhecido cai em `generic` — a uniao das formas, que erra para
    o lado de REJEITAR evidencia em vez de aceitar comentario como codigo.
    """
    raw = str(language or "").strip().lower()
    if raw in _COMMENT_FAMILIES:
        return raw
    if raw in _NAME_COMMENT_FAMILY:
        return _NAME_COMMENT_FAMILY[raw]
    ext = os.path.splitext(raw)[1] if ("." in raw) else ""
    if not ext and raw:
        ext = "." + raw
    return _EXT_COMMENT_FAMILY.get(ext, "generic")


def _strip_cobol_indicator(text: str) -> str:
    """Remove a linha cujo INDICADOR (coluna 7) e `*` ou `/` — comentario COBOL.

    Formato fixo: colunas 1-6 sao numeracao de sequencia e a 7 e o indicador.
    Indice 6 (0-based) e, portanto, a coluna 7.
    """
    out: list[str] = []
    for line in text.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        terminator = line[len(body):]
        if len(body) > 6 and body[6] in "*/":
            out.append(terminator)
            continue
        out.append(line)
    return "".join(out)


def strip_comments(text: str, language: str = "") -> str:
    """Texto SEM comentario nem literal de documentacao, preservando o resto.

    Varredura com estado (codigo / literal / comentario de linha / comentario
    de bloco): e o estado de literal que impede `"// nao e comentario"` e
    `'--'` de serem lidos como comentario — sem ele, uma linha de codigo com
    URL viraria "comentario" e a evidencia seria rejeitada por engano.
    """
    src = text or ""
    if not src:
        return ""
    family = _comment_family(language)
    rules = _COMMENT_FAMILIES[family]
    if rules.get("column7"):
        src = _strip_cobol_indicator(src)

    line_tokens: tuple = tuple(sorted(rules["line"], key=len, reverse=True))
    blocks: tuple = tuple(rules["block"])
    quotes: tuple = tuple(rules["strings"])
    triple = bool(rules["triple"])

    out: list[str] = []
    i, n = 0, len(src)
    while i < n:
        token = next((t for t in line_tokens if src.startswith(t, i)), None)
        if token is not None:
            end = src.find("\n", i)
            i = n if end < 0 else end  # o `\n` fica: linhas nao se fundem
            continue
        block = next((b for b in blocks if src.startswith(b[0], i)), None)
        if block is not None:
            end = src.find(block[1], i + len(block[0]))
            i = n if end < 0 else end + len(block[1])
            continue
        if triple:
            match = _TRIPLE_OPEN.match(src, i)
            if match is not None:
                fence = match.group(1)
                end = src.find(fence, match.end())
                i = n if end < 0 else end + len(fence)
                continue
        if src[i] in quotes:
            quote = src[i]
            j = i + 1
            while j < n:
                if src[j] == "\\":
                    j += 2
                    continue
                if src[j] == quote:
                    j += 1
                    break
                if src[j] == "\n":  # literal nao fechado: nao engolir o resto
                    break
                j += 1
            out.append(src[i:j])
            i = j
            continue
        out.append(src[i])
        i += 1
    return "".join(out)


def is_comment_only(text: str, language: str = "") -> bool:
    """O trecho e SO comentario/documentacao/vazio? Entao nao sustenta nada.

    `language` aceita nome de linguagem, extensao ou caminho (ver
    `_comment_family`). Trecho vazio conta como comentario-only: nao ha codigo
    citado, e "citou o nada" nunca pode valer como sustentacao.
    """
    if not str(text or "").strip():
        return True
    return not strip_comments(text, language).strip()


def is_doc_path(path: str) -> bool:
    """O caminho citado e DOCUMENTO (§5.4, F10)?

    Documento nunca sustenta afirmacao sobre o sistema: ele registra intencao.
    A checagem e por extensao porque o localizador e o que existe aqui — a
    classificacao por conteudo de `analysis.inventory` roda antes, na analise,
    e nao e reimplementada.
    """
    return os.path.splitext(str(path or "").strip().lower())[1] in DOC_EXTENSIONS


# --------------------------------------------------------------------------
# Leitura de `runtime.db`
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class AcceptedResult:
    """Um resultado que o coordenador ACEITOU, pronto para virar claims.

    `input_versions_hash` viaja junto porque é a identidade da entrada que o
    resultado descreve: reintegrar o MESMO resultado sobre o MESMO snapshot é
    a operação que precisa ser idempotente, e é este par que a define.
    """

    task_id: str
    kind: str
    objective_id: str
    capability_id: str
    execution_id: str | None
    input_versions: Mapping[str, Any]
    input_versions_hash: str
    output: Mapping[str, Any]
    objective_payload: Mapping[str, Any]
    asserted_by: str
    created_at: str = ""
    updated_at: str = ""

    @property
    def integration_key(self) -> str:
        """Identidade do PAR (resultado, entradas). Estável entre execuções."""
        return _hash(
            rt_tasks.canonical_json(
                {
                    "objective_id": self.objective_id,
                    "input_versions_hash": self.input_versions_hash,
                    "output": dict(self.output),
                }
            )
        )


def _expected_hash_for(
    expected: str | Mapping[str, str] | None, objective_id: str
) -> str:
    """Hash de entradas esperado para ESTE objetivo (str única ou mapa)."""
    if expected is None:
        return ""
    if isinstance(expected, Mapping):
        return str(expected.get(objective_id) or "")
    return str(expected)


def _worker_of(task: rt_tasks.Task) -> str:
    """Quem afirmou, a partir da configuração da tarefa.

    Nunca vem do `output`: `coordinator.ResultSchema` é fechado e `asserted_by`
    não está declarado — um worker não escolhe em nome de quem fala.
    """
    config = task.config or {}
    for key in ("asserted_by", "worker", "agent", "agent_slot", "engine"):
        raw = str(config.get(key) or "").strip()
        if not raw:
            continue
        return raw if ":" in raw else f"llm:{raw}"
    return DEFAULT_ASSERTED_BY


def collect_results(
    task_store: rt_tasks.TaskStore,
    objective_ids: Sequence[str] | None = None,
    *,
    latest_only: bool = True,
    expected_input_versions_hash: str | Mapping[str, str] | None = None,
) -> list[AcceptedResult]:
    """Resultados aceitos em `runtime.db`, prontos para integração.

    Filtra por construção o que NÃO pode virar conhecimento:

    - estado diferente de `done` (recusa e falha nunca chegam aqui: o
      coordenador as move para `failed`/`blocked`);
    - `termination_reason` de recusa (`rejected:*`, `submit:*`), mesmo que a
      linha tenha resultado antigo de outra tentativa;
    - tarefa sem `result_json` (A01 já impede `done` sem resultado);
    - tarefa cuja última tentativa terminou `rejected`;
    - `objective_id` fora de `objective_ids`, quando dado;
    - `input_versions_hash` diferente do esperado, quando dado.

    `objective_ids` é opcional AQUI (a função também serve para inspeção), mas
    `integrate` o exige: um `runtime.db` guarda as tarefas de TODOS os
    repositórios já analisados no mesmo store, e integrar sem escopo foi o que
    fez a integração do repositório B processar o objetivo do repositório A
    (achado nº5).

    `expected_input_versions_hash` aceita um hash único ou um mapa
    `objective_id -> hash`: resultado que descreve OUTRA versão das entradas
    descreve outro código, e reintegrá-lo grava conhecimento vencido.

    `latest_only` mantém apenas a tarefa mais recente por `objective_id`.
    ATENÇÃO (§7.1): isso NÃO é mais a fonte de verdade da integração. Manter só
    o mais recente descartava a rodada A quando a rodada B chegava — o contrato
    apurado por A desaparecia se B não o repetisse, que é exatamente o
    "reconstruir progresso a partir da última tarefa" que o §7.1 proíbe.
    `integrate()` passou a coletar com `latest_only=False` e a DOBRAR a cadeia
    de resultados por objetivo (`results_by_objective`), aplicando cada um como
    delta sobre o estado acumulado. O parâmetro continua existindo, com o mesmo
    default, para quem usa esta função para INSPEÇÃO ("qual foi o último
    resultado deste objetivo?"), que é um uso legítimo e diferente.

    A preocupação original de `latest_only` (código mudado gera tarefa NOVA, e
    integrar as duas gravaria a versão velha por cima) continua tratada, e por
    um filtro mais preciso: `expected_input_versions_hash` e a conferência de
    `source_version_ids` contra o snapshot descartam o que descreve OUTRA
    revisão de entrada. Resultados da MESMA revisão são rodadas da mesma
    cadeia — e devem somar, não competir.
    """
    wanted = {str(o) for o in objective_ids} if objective_ids is not None else None
    accepted: list[AcceptedResult] = []

    for task in task_store.tasks_in_state(rt_tasks.TaskState.DONE):
        if task.kind not in _TASK_KINDS:
            continue
        if not isinstance(task.result, Mapping) or not task.result:
            continue
        reason = (task.termination_reason or "").strip().lower()
        if reason and reason != "completed":
            continue
        attempts = task_store.attempts(task.task_id)
        if attempts and attempts[-1].outcome is rt_tasks.AttemptOutcome.REJECTED:
            continue
        execution_id = next(
            (
                a.execution_id
                for a in reversed(attempts)
                if a.outcome is rt_tasks.AttemptOutcome.SUCCEEDED
            ),
            None,
        )
        objective_payload = task.objective or {}
        objective_id = str(
            task.result.get("objective_id") or objective_payload.get("objective_id") or ""
        )
        if not objective_id:
            continue
        if wanted is not None and objective_id not in wanted:
            continue
        expected = _expected_hash_for(expected_input_versions_hash, objective_id)
        if expected and expected != task.input_versions_hash:
            continue
        accepted.append(
            AcceptedResult(
                task_id=task.task_id,
                kind=task.kind.value,
                objective_id=objective_id,
                capability_id=str(
                    task.result.get("capability_id")
                    or objective_payload.get("capability_id")
                    or ""
                ),
                execution_id=execution_id,
                input_versions=dict(task.input_versions or {}),
                input_versions_hash=task.input_versions_hash,
                output=dict(task.result),
                objective_payload=dict(objective_payload),
                asserted_by=_worker_of(task),
                created_at=task.created_at,
                updated_at=task.updated_at,
            )
        )

    if not latest_only:
        return accepted

    latest: dict[str, AcceptedResult] = {}
    for item in accepted:
        current = latest.get(item.objective_id)
        if current is None or (item.created_at, item.task_id) > (
            current.created_at,
            current.task_id,
        ):
            latest[item.objective_id] = item
    return [latest[k] for k in sorted(latest)]


# --------------------------------------------------------------------------
# Resultado -> claims
# --------------------------------------------------------------------------


def results_by_objective(
    results: Sequence[AcceptedResult],
) -> dict[str, list[AcceptedResult]]:
    """Resultados agrupados por objetivo, em ordem CRONOLÓGICA da cadeia.

    A ordem é `(created_at, task_id)`: é ela que define o que é "A depois B".
    O delta seguinte sempre se aplica sobre o anterior, e não o contrário —
    sem ordem estável, o contrato consolidado dependeria da ordem de varredura
    do SQLite.
    """
    grouped: dict[str, list[AcceptedResult]] = {}
    for item in results:
        grouped.setdefault(item.objective_id, []).append(item)
    for chain in grouped.values():
        chain.sort(key=lambda r: (r.created_at, r.task_id))
    return grouped


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _norm_ws(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text if text is not None else "")).strip()


def _slug(text: str, limit: int = 60) -> str:
    """Chave estável e legível para `stable_key` de entidade."""
    folded = unicodedata.normalize("NFKD", text or "")
    ascii_only = "".join(c for c in folded if not unicodedata.combining(c))
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_only.lower()).strip("-")
    return (slug[:limit].rstrip("-")) or "sem-assunto"


def _as_mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _evidence_dicts(container: Any) -> list[dict[str, Any]]:
    if not isinstance(container, Mapping):
        return []
    for key in _EVIDENCE_KEYS:
        raw = container.get(key)
        if isinstance(raw, Mapping):
            return [dict(raw)]
        if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
            return [dict(e) for e in raw if isinstance(e, Mapping)]
    return []


def _as_evidence_ref_dict(raw: Mapping[str, Any]) -> dict[str, Any] | None:
    """Normaliza para o formato de `analysis.capabilities.EvidenceRef.to_dict`.

    `ContractField.from_dict` reconstrói `EvidenceRef`, que exige
    `path`/`line_start`/`line_end`: um worker que mandar `start_line` (o nome
    do LOCALIZADOR, não o do ref) não pode perder a citação por causa disso.
    """
    citation = _claim_evidence(raw)
    if citation is None:
        return None
    locator = _as_mapping(raw.get("locator"))
    return {
        "path": citation.path,
        "line_start": citation.start_line,
        "line_end": citation.end_line,
        "role": str(raw.get("role") or ""),
        "symbol": raw.get("symbol") or locator.get("symbol"),
        "locator": locator or None,
        "note": str(raw.get("note") or ""),
    }


def _claim_evidence(raw: Mapping[str, Any]) -> ClaimEvidence | None:
    """`EvidenceRef`/localizador do worker -> `ClaimEvidence` verificável.

    Aceita as três formas que aparecem no caminho real: o `to_dict()` de
    `analysis.capabilities.EvidenceRef`, o localizador de código de
    `knowledge.evidence` e o par `path` + `start_line`/`end_line`. Localizador
    sem faixa não vira citação — devolve `None` em vez de faixa inventada.
    """
    locator = _as_mapping(raw.get("locator"))
    source = locator or raw
    path = str(source.get("path") or raw.get("path") or "").strip()
    start = source.get("start_line", source.get("line_start", raw.get("line_start")))
    end = source.get("end_line", source.get("line_end", raw.get("line_end")))
    if not path:
        return None
    try:
        start_line = int(start)
        end_line = int(end if end is not None else start)
    except (TypeError, ValueError):
        return None
    if start_line < 1 or end_line < start_line:
        return None
    declared = raw.get("content_kind") or locator.get("content_kind")
    try:
        content_kind = ContentKind(declared) if declared else ContentKind.EXECUTABLE
    except ValueError:
        content_kind = ContentKind.EXECUTABLE
    snippet_hash = locator.get("snippet_hash") or raw.get("snippet_hash")
    try:
        return ClaimEvidence(
            path=path,
            start_line=start_line,
            end_line=end_line,
            content_kind=content_kind,
            source_kind=SourceKind.CODE,
            snippet_hash=str(snippet_hash) if snippet_hash else None,
        )
    except VerificationError:
        return None


def _split_statements(text: str) -> list[str]:
    """Conteúdo livre de um campo -> uma afirmação por linha/bullet.

    Uma linha é a menor unidade que um worker escreve como afirmação isolada.
    Fundir o campo inteiro num claim só produziria um enunciado que nenhuma
    checagem mecânica alcança.
    """
    out: list[str] = []
    for raw_line in str(text or "").splitlines():
        line = _BULLET.sub("", raw_line).strip()
        if len(line) >= 3:
            out.append(line)
    if not out:
        collapsed = _norm_ws(text)
        if len(collapsed) >= 3:
            out.append(collapsed)
    return out


def _accent_fold(text: str) -> str:
    """Remove diacríticos PRESERVANDO o comprimento em caracteres.

    NFKD decompõe `é` em `e` + combining; descartar só os combining devolve uma
    string com o mesmo número de caracteres para o português corrente, então os
    offsets do casamento no texto dobrado indexam o texto ORIGINAL. É o que
    permite detectar o verbo em "é aprovado" e recortar a cláusula no original.
    """
    folded = "".join(
        c for c in unicodedata.normalize("NFKD", text or "") if not unicodedata.combining(c)
    )
    return folded if len(folded) == len(text or "") else (text or "")


@dataclass(frozen=True)
class _Consequence:
    """A parte da frase que diz O QUE ACONTECE — o "então" do "se ... então".

    `literals` são os valores mecanicamente confrontáveis com o trecho (código
    de status, nome de estado, literal citado). `effect_kind` é o efeito que
    confirmaria a consequência quando ela não traz literal nenhum. `verifiable`
    é falso quando a consequência existe mas não produziu NADA mecânico — o
    caso em que o claim inteiro não pode sair `supported`.
    """

    text: str
    verb: str
    effect_kind: str
    polarity: str
    literals: tuple[str, ...] = ()
    exception: str | None = None

    @property
    def verifiable(self) -> bool:
        return bool(self.literals or self.exception or self.effect_kind in _MECHANICAL_EFFECTS)


#: Efeitos que `analysis.verification.extract_effects` sabe observar no trecho.
#: `state` fica de fora: "vira aprovado" sem literal não tem forma mecânica.
_MECHANICAL_EFFECTS: frozenset[str] = frozenset({"return", "raise", "publish", "write"})


def _consequence_of(statement: str, start: int = 0) -> _Consequence | None:
    """Consequência declarada no enunciado, a partir de `start` (§achado nº1).

    `start` é o fim da CONDIÇÃO já extraída: procurar o verbo depois dela evita
    confundir o operando da comparação com o valor da consequência.
    """
    text = statement or ""
    folded = _accent_fold(text)
    match = _CONSEQUENCE_VERB_RE.search(folded, start)
    if match is None and start <= 0:
        # Sem verbo de consequência E sem condição antes: a frase inteira é uma
        # afirmação só. Colher "literais" dela seria inventar consequência onde
        # o enunciado não declara nenhuma — e inventar checagem que PASSA é pior
        # que não checar (promoveria claim hoje `unresolved` a `supported`).
        return None
    clause_start = match.start() if match else start
    end_match = _CLAUSE_END.search(folded, clause_start + (len(match.group(1)) if match else 0))
    clause = text[clause_start : end_match.start() if end_match else len(text)]
    if not clause.strip():
        return None

    literals: list[str] = []
    for m in _STATUS_WORD.finditer(clause):
        literals.append(m.group(1))
    for m in _STATUS_CODE.finditer(clause):
        if m.group(1) not in literals:
            literals.append(m.group(1))
    for m in _QUOTED.finditer(clause):
        value = m.group(1) or m.group(2) or ""
        if value and value not in literals:
            literals.append(value)

    exception = None
    raised = _TEXT_RAISE.search(clause)
    if raised:
        exception = raised.group(1)
    else:
        named = _EXC_NAME.search(clause)
        if named:
            exception = named.group(1)

    if match is None:
        # Sem verbo: só é consequência se houver valor mecânico solto depois da
        # condição ("Se total > 1000, HTTP 409").
        if not literals and exception is None:
            return None
        return _Consequence(
            text=_norm_ws(clause), verb="", effect_kind="", polarity="neutral",
            literals=tuple(literals), exception=exception,
        )

    verb = _accent_fold(match.group(1)).lower()
    effect_kind, polarity = _CONSEQUENCE_VERBS.get(verb, ("", "neutral"))
    if effect_kind == "state" and not literals:
        for m in _STATE_TOKEN.finditer(clause):
            if m.group(1) not in literals:
                literals.append(m.group(1))
    return _Consequence(
        text=_norm_ws(clause),
        verb=verb,
        effect_kind=effect_kind,
        polarity=polarity,
        literals=tuple(literals),
        exception=exception,
    )


def _statement_parse(text: str, campo: str = "") -> tuple[dict[str, Any], _Consequence | None]:
    """Enunciado -> (campos mecânicos, consequência). Nunca para na 1ª extração.

    A versão anterior RETORNAVA na comparação e a consequência ficava fora do
    claim: `check_support` aprovava `total > 1000`, e `_write_claim` gravava a
    FRASE INTEIRA ("... é aprovado com HTTP 200") como sustentada, contra um
    trecho que devolve 409. Aqui condição, consequência e exceção são derivadas
    juntas — e a consequência volta ao chamador para que a regra de sustentação
    (`_consequence_guard`) possa julgar a frase pelo componente mais fraco.
    """
    statement = _norm_ws(text)
    if not statement:
        return {}, None

    fields: dict[str, Any] = {}
    end = 0
    match = _TEXT_CMP_SYMBOLIC.search(statement)
    if match:
        comparator = match.group(2)
        fields["condition"] = match.group(1)
        fields["comparator"] = "==" if comparator == "=" else comparator
        fields["value"] = match.group(3)
        end = match.end()
    else:
        worded = _TEXT_CMP_WORDS.search(statement)
        if worded:
            fields["condition"] = worded.group(1)
            fields["comparator"] = _WORD_COMPARATOR[worded.group(2).lower()]
            fields["value"] = worded.group(3)
            end = worded.end()

    consequence = _consequence_of(statement, end)
    if consequence is not None:
        # `literals` é a forma mecânica da consequência que NÃO presume a
        # gramática do efeito: "devolve 409" contra `raise ConflictError(409)`
        # continua sustentado (o valor está lá), enquanto "aprovado com HTTP
        # 200" contra o mesmo trecho falha — que é exatamente o achado nº1.
        # `effect` NÃO é derivado por isto: `_check_effect` exigiria um `return`
        # no trecho e rebaixaria o primeiro caso, verdadeiro. O efeito é
        # confrontado em `_consequence_confirmed`, com `extract_effects`.
        if consequence.literals:
            fields["literals"] = list(consequence.literals)
        if consequence.exception:
            fields["exception"] = consequence.exception

    if "exception" not in fields:
        raised = _TEXT_RAISE.search(statement)
        if raised:
            fields["exception"] = raised.group(1)
        elif campo == "falhas":
            named = _EXC_NAME.search(statement)
            if named:
                fields["exception"] = named.group(1)
    return fields, consequence


def _derive_statement_fields(text: str, campo: str) -> dict[str, Any]:
    """Campos mecânicos EXTRAÍDOS do enunciado (condição + consequência)."""
    return _statement_parse(text, campo)[0]


# --------------------------------------------------------------------------
# Regra de sustentação da FRASE INTEIRA (achado nº1 da 2ª auditoria)
# --------------------------------------------------------------------------

def _literal_in(text: str, snippet: str) -> bool:
    """Mesma semântica de `verification._check_literals`: substring normalizada."""
    if not text:
        return False
    if text in (snippet or ""):
        return True
    if _norm_ws(text) and _norm_ws(text) in _norm_ws(snippet):
        return True
    collapsed = re.sub(r"\s+", "", snippet or "")
    compact = re.sub(r"\s+", "", text)
    return bool(compact) and compact in collapsed


def _consequence_in(cons: _Consequence, code: str, path: str) -> bool:
    """A consequência aparece MECANICAMENTE neste PEDAÇO de código?

    `code` é um trecho citado inteiro OU o corpo de um ramo (a associação
    condição→ramo usa a MESMA regra de confirmação, para que "está no trecho" e
    "está no ramo" não divirjam). Literal presente basta e é deliberado:
    "devolve 409" contra `raise ConflictError(409)` continua sustentado — o valor
    afirmado ESTÁ ali, e exigir a forma `return` reprovaria uma afirmação
    verdadeira. O que não passa é o valor AUSENTE ("HTTP 200" contra o mesmo
    trecho).
    """
    if not cons.verifiable:
        return False
    if cons.literals and not all(_literal_in(lit, code) for lit in cons.literals):
        return False
    if cons.exception:
        effects = extract_effects(code, path)
        leaf = cons.exception.rsplit(".", 1)[-1]
        if not any(r.rsplit(".", 1)[-1] == leaf for r in effects.raises if r):
            return False
    if not cons.literals and not cons.exception:
        effects = extract_effects(code, path)
        observed = {
            "return": effects.returns,
            "raise": effects.raises,
            "publish": tuple(effects.publishes),
            "write": tuple(effects.writes),
        }.get(cons.effect_kind, ())
        if not observed:
            return False
    return True


def _consequence_confirmed(cons: _Consequence, verdict: Verdict) -> bool:
    """A consequência aparece em alguma citação que resolveu?

    Regra de PRESENÇA, sem vínculo de controle: só é usada quando o enunciado
    NÃO traz condição mecânica (frase não condicional). Frase condicional passa
    por `_branch_association` — presença no trecho não prova que o efeito
    pertence ao ramo da condição (achado nº1 da 3ª auditoria).
    """
    if not cons.verifiable:
        return False
    return any(
        loc.ok and _consequence_in(cons, loc.snippet, loc.path) for loc in verdict.locations
    )


# --------------------------------------------------------------------------
# Associação condição -> ramo -> efeito (achado nº1 da 3ª auditoria)
# --------------------------------------------------------------------------
#
# O buraco: `_consequence_confirmed` respondia "o literal afirmado está no
# trecho?". Para
#
#     def approve(total):
#         if total > 1000:
#             return {"status": 409}
#         return {"status": 200}
#
# e o claim "quando total > 1000, retorna HTTP 200" citando a função INTEIRA, a
# resposta era SIM — o `200` está no trecho, no ramo ERRADO. Condição e
# consequência em ramos diferentes viravam `supported`. Aqui a pergunta passa a
# ser "o efeito está DENTRO do ramo governado pela condição?", respondida pela
# gramática (`ast`), nunca por proximidade textual.

#: Comparador espelhado quando os operandos trocam de lado (`a > b` ≡ `b < a`).
_MIRROR_CMP: Mapping[str, str] = {
    ">": "<", "<": ">", ">=": "<=", "<=": ">=", "==": "==", "!=": "!=",
}
#: Comparador negado — o que vale no caminho COMPLEMENTAR do mesmo `if`.
_NEGATE_CMP: Mapping[str, str] = {
    ">": "<=", "<": ">=", ">=": "<", "<=": ">", "==": "!=", "!=": "==",
}
#: Comparadores do enunciado que não vêm normalizados de `_statement_parse`.
_CMP_CANON: Mapping[str, str] = {"===": "==", "!==": "!=", "<>": "!=", "=": "=="}
_AST_CMP_SYM: Mapping[type, str] = {
    ast.Gt: ">", ast.GtE: ">=", ast.Lt: "<", ast.LtE: "<=",
    ast.Eq: "==", ast.NotEq: "!=", ast.Is: "==", ast.IsNot: "!=",
}

#: Vereditos da associação. Só os dois primeiros deixam a frase `supported`.
_ASSOC_INSIDE = "inside"            # efeito DENTRO do ramo da condição
_ASSOC_COMPLEMENT = "complement"    # condição complementar + efeito no else/fallthrough do MESMO if
_ASSOC_CONTRARY = "contrary"        # o ramo faz outra coisa, incompatível
_ASSOC_OUTSIDE = "outside"          # efeito existe, mas fora do ramo
_ASSOC_UNDETERMINED = "undetermined"  # condição não localizada como ramo no trecho
_ASSOC_HEURISTIC = "heuristic"      # trecho não parseável como Python
_ASSOC_NONE = ""                    # enunciado sem condição mecânica: regra antiga


@dataclass(frozen=True)
class _Condition:
    """A condição do enunciado em forma comparável com um `if` do código."""

    left: str
    op: str
    right: str

    def render(self) -> str:
        return f"{self.left} {self.op} {self.right}"


def _condition_of(fields: Mapping[str, Any]) -> _Condition | None:
    """Condição mecânica do enunciado, ou `None` quando a frase não traz uma."""
    left = _norm_ws(fields.get("condition"))
    right = _norm_ws(fields.get("value"))
    op = _norm_ws(fields.get("comparator"))
    op = _CMP_CANON.get(op, op)
    if not left or not right or op not in _NEGATE_CMP:
        return None
    return _Condition(left, op, right)


@dataclass(frozen=True)
class _Branch:
    """Um ramo do código: o que roda QUANDO a condição do claim vale.

    `taken` é o caminho governado pela condição afirmada; `other` é o caminho
    oposto — usado só para DIZER que a consequência apareceu do lado errado.
    """

    taken: tuple[Any, ...]
    other: tuple[Any, ...]
    test_src: str
    via: str
    complementary: bool = False


def _src(node: Any) -> str:
    try:
        return ast.unparse(node)
    except (AttributeError, ValueError, TypeError, RecursionError):
        return ""


def _block_src(stmts: Sequence[Any]) -> str:
    return "\n".join(s for s in (_src(node) for node in stmts) if s)


def _stmt_lists(tree: ast.AST) -> Iterable[list[Any]]:
    """Todo bloco de statements do trecho (corpo, else, finally) — inclusive
    aninhados, porque o efeito pode estar num `if` interno do mesmo ramo."""
    for node in ast.walk(tree):
        for name in ("body", "orelse", "finalbody"):
            seq = getattr(node, name, None)
            if isinstance(seq, list) and all(isinstance(s, ast.stmt) for s in seq):
                yield seq


def _terminates(stmts: Sequence[Any]) -> bool:
    """O bloco SEMPRE sai (return/raise)?

    É a condição mecânica para o `fallthrough` — o código DEPOIS do `if`, sem
    `else` — ser o caminho complementar: se o corpo sempre retorna/levanta, o que
    vem depois só executa quando a condição é FALSA. Sem isso, "depois do if" não
    prova nada e a associação fica indeterminada.
    """
    if not stmts:
        return False
    last = stmts[-1]
    if isinstance(last, (ast.Return, ast.Raise)):
        return True
    if isinstance(last, ast.If):
        return _terminates(last.body) and _terminates(last.orelse)
    return False


def _and_parts(node: Any, negated: bool) -> Iterable[tuple[Any, bool]]:
    """Condições que precisam TODAS valer para entrar no corpo.

    `and` distribui (entrar no corpo implica cada conjunto verdadeiro); `or` NÃO
    (entrar não implica o ramo específico), então só é aberto sob negação, onde
    `not (a or b)` ≡ `not a and not b`.
    """
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        yield from _and_parts(node.operand, not negated)
        return
    if isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.And) and not negated:
            for value in node.values:
                yield from _and_parts(value, False)
            return
        if isinstance(node.op, ast.Or) and negated:
            for value in node.values:
                yield from _and_parts(value, True)
            return
    yield node, negated


def _cmp_same(sym: str, left: str, right: str, cond: _Condition) -> bool:
    """`left sym right` é a MESMA comparação que a condição do enunciado?

    Inversão de operandos conta: `total > 1000` ≡ `1000 < total`.
    """
    if sym == cond.op and _operand_equal(left, cond.left) and _operand_equal(right, cond.right):
        return True
    return (
        sym == _MIRROR_CMP[cond.op]
        and _operand_equal(left, cond.right)
        and _operand_equal(right, cond.left)
    )


def _test_relation(test: Any, cond: _Condition) -> str | None:
    """`direct` (o teste é a condição), `complement` (é a negação dela) ou None."""
    for part, negated in _and_parts(test, False):
        if not isinstance(part, ast.Compare) or len(part.ops) != 1:
            continue
        sym = _AST_CMP_SYM.get(type(part.ops[0]))
        if sym is None:
            continue
        if negated:
            sym = _NEGATE_CMP[sym]
        left, right = _src(part.left), _src(part.comparators[0])
        if _cmp_same(sym, left, right, cond):
            return "direct"
        if _cmp_same(_NEGATE_CMP[sym], left, right, cond):
            return "complement"
    return None


def _branches_for(tree: ast.AST, cond: _Condition) -> list[_Branch]:
    """Ramos do trecho governados pela condição do enunciado."""
    out: list[_Branch] = []
    for body in _stmt_lists(tree):
        for idx, node in enumerate(body):
            if not isinstance(node, ast.If):
                continue
            relation = _test_relation(node.test, cond)
            if relation is None:
                continue
            fall = tuple(body[idx + 1:]) if (not node.orelse and _terminates(node.body)) else ()
            test_src = _src(node.test)
            if relation == "direct":
                out.append(
                    _Branch(tuple(node.body), tuple(node.orelse) + fall, test_src, "corpo do if")
                )
                continue
            complement = tuple(node.orelse) or fall
            if complement:
                out.append(
                    _Branch(
                        complement,
                        tuple(node.body),
                        test_src,
                        "else/fallthrough imediato do mesmo if",
                        complementary=True,
                    )
                )
    for node in ast.walk(tree):
        if not isinstance(node, ast.IfExp):
            continue
        relation = _test_relation(node.test, cond)
        if relation is None:
            continue
        yes, no = (ast.Expr(node.body),), (ast.Expr(node.orelse),)
        direct = relation == "direct"
        out.append(
            _Branch(
                yes if direct else no,
                no if direct else yes,
                _src(node.test),
                "ramo do ternário",
                complementary=not direct,
            )
        )
    return out


def _branch_contradiction(cons: _Consequence, code: str, path: str) -> str:
    """O ramo faz algo INCOMPATÍVEL com a consequência afirmada? O quê."""
    codes = {lit for lit in cons.literals if _STATUS_CODE.fullmatch(lit or "")}
    found = {m.group(1) for m in _STATUS_CODE.finditer(code or "")}
    if codes and found and not (codes & found):
        return f"usa código {', '.join(sorted(found))}, não {', '.join(sorted(codes))}"
    effects = extract_effects(code, path)
    named = sorted({r for r in effects.raises if r})
    if cons.exception:
        # Guard-rail simétrico: exceção nomeada segue a MESMA regra de ramo.
        leaf = cons.exception.rsplit(".", 1)[-1]
        if named and effects.raises_all_named and not any(
            r.rsplit(".", 1)[-1] == leaf for r in named
        ):
            return f"levanta {', '.join(named)}, não {cons.exception}"
    elif cons.polarity == "positive" and named and effects.raises_all_named:
        return f"levanta {', '.join(named)} (consequência afirmada é de aprovação/retorno)"
    return ""


def _branch_association(cons: _Consequence, cond: _Condition, verdict: Verdict) -> tuple[str, str]:
    """(veredito, motivo) do vínculo condição→ramo→efeito nas citações que resolveram.

    Precedência: uma citação que CONFIRMA vence; senão contradição de ramo vence
    "só existe fora do ramo", que vence "condição não localizada". Nenhum
    resultado que não seja `inside`/`complement` pode deixar a frase `supported`.
    """
    best: tuple[str, str] | None = None
    order = {
        _ASSOC_CONTRARY: 0,
        _ASSOC_OUTSIDE: 1,
        _ASSOC_UNDETERMINED: 2,
        _ASSOC_HEURISTIC: 3,
    }
    for loc in verdict.locations:
        if not loc.ok:
            continue
        assoc, why = _branch_association_at(cons, cond, loc.snippet, loc.path, loc.language)
        why = f"{why} (trecho em {loc.span})"
        if assoc in (_ASSOC_INSIDE, _ASSOC_COMPLEMENT):
            return assoc, why
        if best is None or order[assoc] < order[best[0]]:
            best = (assoc, why)
    if best is None:
        return _ASSOC_UNDETERMINED, "nenhuma citação resolvida para associar condição e efeito"
    return best


def _branch_association_at(
    cons: _Consequence, cond: _Condition, snippet: str, path: str, language: str = ""
) -> tuple[str, str]:
    """Associação condição→ramo→efeito DENTRO de um trecho citado."""
    if language and language != "python":
        return (
            _ASSOC_HEURISTIC,
            f"trecho em {language}: associação condição→ramo só é resolvida por gramática Python",
        )
    tree = _parse_python_fragment(snippet or "")
    if tree is None:
        return (
            _ASSOC_HEURISTIC,
            "trecho não parseável como Python: associação condição→ramo seria heurística",
        )
    branches = _branches_for(tree, cond)
    if not branches:
        return (
            _ASSOC_UNDETERMINED,
            f"condição `{cond.render()}` não localizada como ramo (if/ternário) no trecho",
        )
    contrary = ""
    outside = ""
    for branch in branches:
        taken = _block_src(branch.taken)
        if _consequence_in(cons, taken, path):
            assoc = _ASSOC_COMPLEMENT if branch.complementary else _ASSOC_INSIDE
            return assoc, f"efeito afirmado está no {branch.via} de `{branch.test_src}`"
        why = _branch_contradiction(cons, taken, path)
        if why and not contrary:
            contrary = f"o {branch.via} de `{branch.test_src}` {why}"
        if not outside and _consequence_in(cons, _block_src(branch.other), path):
            outside = (
                f"a consequência aparece APENAS FORA do {branch.via} de `{branch.test_src}` "
                "(outro caminho de execução)"
            )
    if contrary:
        return _ASSOC_CONTRARY, contrary
    if outside:
        return _ASSOC_OUTSIDE, outside
    return (
        _ASSOC_OUTSIDE,
        f"efeito afirmado não observado no ramo de `{cond.render()}`",
    )


def _contrary_consequence(cons: _Consequence, verdict: Verdict) -> str:
    """O trecho mostra consequência CONTRÁRIA à afirmada? Devolve a divergência.

    Duas formas mecânicas, ambas do achado nº1: código de status afirmado que
    não é nenhum dos códigos do trecho, e consequência de aprovação/retorno
    contra trecho que só levanta exceção nomeada.
    """
    codes = {lit for lit in cons.literals if _STATUS_CODE.fullmatch(lit or "")}
    for loc in verdict.locations:
        if not loc.ok:
            continue
        if codes:
            found = {m.group(1) for m in _STATUS_CODE.finditer(loc.snippet or "")}
            if found and not (codes & found):
                return (
                    f"afirmado código {', '.join(sorted(codes))}; o trecho em {loc.span} usa "
                    f"{', '.join(sorted(found))}"
                )
        if cons.polarity == "positive":
            effects = extract_effects(loc.snippet, loc.path)
            named = sorted({r for r in effects.raises if r})
            if named and effects.raises_all_named:
                return (
                    f"consequência afirmada é de aprovação/retorno; o trecho em {loc.span} "
                    f"levanta {', '.join(named)}"
                )
    return ""


def _consequence_guard(claim: Claim, verdict: Verdict) -> tuple[Verdict, str]:
    """Veredito da frase INTEIRA + o escopo efetivamente sustentado (achado nº1).

    Reprodução da auditoria: código `if total > 1000: raise Conflict(409)`,
    worker devolve "Quando total > 1000, o pedido é aprovado com HTTP 200".
    `check_support` aprovava a comparação — que existe mesmo — e a frase inteira
    era gravada `supported/implemented`. Aqui a frase só continua sustentada se
    a CONSEQUÊNCIA também for confirmada no trecho; se o trecho mostra a
    consequência contrária, o claim vira `disputed`; se a consequência não é
    mecanicamente verificável, cai para `inferred`. Nunca promove nada.

    3ª auditoria: confirmar a consequência no TRECHO não bastava — em
    `if total > 1000: return 409` / `return 200`, o claim "quando total > 1000
    retorna 200" citando a função inteira achava o `200` no ramo ERRADO e saía
    `supported`. Frase com condição mecânica passa agora por
    `_branch_association`: o efeito tem de estar DENTRO do ramo governado pela
    condição (ou no caminho complementar, quando a condição afirmada é a
    complementar). Fora do ramo, sem ramo localizável, ou trecho não parseável
    como Python: nunca `supported`.
    """
    fields, consequence = _statement_parse(claim.statement)
    if consequence is None:
        return verdict, ""
    # Frase CONDICIONAL passa pela associação condição→ramo→efeito (3ª auditoria):
    # presença do literal no trecho não prova que o efeito pertence ao ramo da
    # condição. Frase sem condição mecânica (ou consequência sem forma mecânica)
    # segue pela regra de presença, que é tudo que há para checar.
    condition = _condition_of(fields) if consequence.verifiable else None
    assoc, assoc_why = (
        _branch_association(consequence, condition, verdict)
        if condition is not None
        else (_ASSOC_NONE, "")
    )
    if assoc in (_ASSOC_INSIDE, _ASSOC_COMPLEMENT):
        return verdict, f"frase completa: condição e consequência no mesmo ramo — {assoc_why}"
    if assoc == _ASSOC_NONE and _consequence_confirmed(consequence, verdict):
        return verdict, "frase completa: condição e consequência confirmadas no trecho"

    contrary = assoc_why if assoc == _ASSOC_CONTRARY else _contrary_consequence(consequence, verdict)
    if contrary:
        reasons = verdict.reasons + (
            f"consequência afirmada ({consequence.text!r}) CONTRADITA pelo trecho: {contrary}",
            "a frase vale pelo componente mais fraco: condição sustentada não sustenta "
            "consequência divergente (achado nº1)",
        )
        return (
            replace(
                verdict,
                epistemic=EpistemicStatus.DISPUTED,
                reasons=reasons,
                support_recorded_by=None,
                external_behavior_supported=False,
                insufficient=False,
            ),
            f"nenhum: o trecho mostra consequência contrária ({contrary})",
        )

    motivo = (
        f"consequência afirmada ({consequence.text!r}) não confirmada mecanicamente no trecho"
        if consequence.verifiable
        else (
            f"consequência afirmada ({consequence.text!r}) não é derivável em campo mecânico "
            "(sem literal, efeito ou exceção a confrontar)"
        )
    )
    if assoc == _ASSOC_OUTSIDE:
        motivo = (
            f"consequência afirmada ({consequence.text!r}) não confirmada NO RAMO da condição "
            f"`{condition.render()}`: {assoc_why}"
        )
    elif assoc == _ASSOC_UNDETERMINED:
        motivo = (
            f"vínculo condição→efeito não estabelecido para ({consequence.text!r}): {assoc_why}"
        )
    elif assoc == _ASSOC_HEURISTIC:
        motivo = (
            f"vínculo condição→efeito não estabelecido para ({consequence.text!r}): {assoc_why} "
            "— associação heurística nunca sustenta a frase inteira"
        )
    escopo = f"apenas a condição — {motivo}"
    if verdict.epistemic is not EpistemicStatus.SUPPORTED:
        return verdict, escopo
    reasons = verdict.reasons + (
        motivo,
        "claim rebaixado a inferred: a condição sozinha não sustenta a frase inteira "
        "(achado nº1)",
    )
    return (
        replace(
            verdict,
            epistemic=EpistemicStatus.INFERRED,
            reasons=reasons,
            nature=None,
            support_recorded_by=None,
            external_behavior_supported=False,
            insufficient=not consequence.verifiable,
        ),
        escopo,
    )


def _structured_fields(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Campos mecânicos DECLARADOS pelo worker, normalizados para o schema
    fechado de `Claim.statement_fields` (campo desconhecido é ignorado aqui e
    seria rejeitado por `Claim.__post_init__`)."""
    out: dict[str, Any] = {}
    for key, value in raw.items():
        target = _STATEMENT_FIELD_ALIASES.get(str(key).strip().lower())
        if target is None or value in (None, "", [], {}):
            continue
        out[target] = value
    return out


def _named_subject(raw: Mapping[str, Any], statement: str) -> str:
    """Assunto nomeado da afirmação — o que vira `BusinessRule`/`Flow`.

    Prefere o campo explícito; só então o prefixo `Nome: enunciado`, e apenas
    quando o prefixo é curto o bastante para ser um nome e não uma frase.
    """
    for key in _NAME_KEYS:
        value = _norm_ws(raw.get(key))
        if value:
            return value
    match = _NAMED_PREFIX.match(statement or "")
    if match:
        candidate = _norm_ws(match.group(1))
        if candidate and len(candidate.split()) <= 8 and not _TEXT_CMP_SYMBOLIC.search(candidate):
            return candidate
    return ""


@dataclass(frozen=True)
class _Assertion:
    """Uma afirmação normalizada, antes de virar `Claim`."""

    campo: str
    statement: str
    subject_name: str
    statement_fields: Mapping[str, Any]
    evidence: tuple[ClaimEvidence, ...]
    symbol: str | None


def _field_payload(output: Mapping[str, Any], name: str) -> Any:
    contract = output.get("contract")
    if isinstance(contract, Mapping):
        return contract.get(name)
    return None


def _merged_field(objective: InvestigationObjective, output: Mapping[str, Any], name: str) -> ContractField:
    """Campo do contrato depois do resultado, como `ContractField` válido."""
    base = objective.contract.get(name)
    data = base.to_dict() if base is not None else {
        "name": name,
        "label": CONTRACT_LABELS.get(name, ""),
        "status": ContractFieldStatus.PENDING.value,
    }
    raw = _field_payload(output, name)
    if isinstance(raw, str):
        raw = {"status": ContractFieldStatus.FILLED.value, "content": raw}
    elif isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        raw = {"status": ContractFieldStatus.FILLED.value, "afirmacoes": list(raw)}
    if isinstance(raw, Mapping):
        for key in ("status", "content", "motivo", "impacto", "label"):
            if key in raw and raw[key] not in (None, ""):
                data[key] = raw[key]
        refs = [d for d in (_as_evidence_ref_dict(r) for r in _evidence_dicts(raw)) if d]
        if refs:
            data["evidence_refs"] = refs
    data["name"] = name
    data.setdefault("label", CONTRACT_LABELS.get(name, ""))
    try:
        return ContractField.from_dict(data)
    except Exception:
        return ContractField(name=name, label=CONTRACT_LABELS.get(name, ""))


def _citations(raws: Sequence[Mapping[str, Any]]) -> tuple[tuple[ClaimEvidence, ...], str | None]:
    """Citações verificáveis + o símbolo declarado na primeira delas."""
    out: list[ClaimEvidence] = []
    symbol: str | None = None
    for raw in raws:
        citation = _claim_evidence(raw)
        if citation is None:
            continue
        out.append(citation)
        if symbol is None:
            candidate = raw.get("symbol") or _as_mapping(raw.get("locator")).get("symbol")
            symbol = _norm_ws(candidate) or None
    return tuple(out), symbol


def _assertions_of(
    campo: str, raw: Any, merged: ContractField
) -> list[_Assertion]:
    """Afirmações de UM campo do contrato, venham como lista ou como texto."""
    field_raw_refs = _evidence_dicts(raw if isinstance(raw, Mapping) else {}) or [
        e.to_dict() for e in merged.evidence_refs
    ]
    field_evidence, field_symbol = _citations(field_raw_refs)

    items: list[Any] = []
    if isinstance(raw, Mapping):
        for key in _ASSERTION_KEYS:
            value = raw.get(key)
            if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
                items = list(value)
                break
    elif isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        items = list(raw)

    if not items:
        content = ""
        if isinstance(raw, Mapping):
            content = str(raw.get("content") or "")
        elif isinstance(raw, str):
            content = raw
        content = content or merged.content
        items = _split_statements(content)

    out: list[_Assertion] = []
    for item in items:
        if isinstance(item, Mapping):
            statement = ""
            for key in _STATEMENT_KEYS:
                statement = _norm_ws(item.get(key))
                if statement:
                    break
            structured = _structured_fields(item)
            if not statement:
                # `value` só serve de enunciado quando não é o operando de uma
                # comparação declarada — senão o mesmo campo seria as duas coisas.
                statement = "" if "comparator" in structured else _norm_ws(item.get("value"))
                if statement:
                    structured.pop("value", None)
            if not statement:
                continue
            own_evidence, own_symbol = _citations(_evidence_dicts(item))
            evidence = own_evidence or field_evidence
            symbol = _norm_ws(item.get("symbol")) or own_symbol or (
                field_symbol if evidence is field_evidence else None
            )
            out.append(
                _Assertion(
                    campo=campo,
                    statement=statement,
                    subject_name=_named_subject(item, statement),
                    statement_fields=structured or _derive_statement_fields(statement, campo),
                    evidence=evidence,
                    symbol=symbol or None,
                )
            )
            continue

        statement = _norm_ws(item)
        if len(statement) < 3:
            continue
        out.append(
            _Assertion(
                campo=campo,
                statement=statement,
                subject_name=_named_subject({}, statement),
                statement_fields=_derive_statement_fields(statement, campo),
                evidence=field_evidence,
                symbol=field_symbol,
            )
        )
    return out


#: `predicate_kind` de afirmação vinda de campo NÃO previsto no §6.3.
#: `behavior` é o mais genérico de `analysis.verification.PREDICATE_KINDS` — o
#: único que não promete estrutura (comparação, dado, contrato, teste) que
#: ninguém declarou para o campo novo.
UNFORESEEN_PREDICATE_KIND = "behavior"


def unforeseen_contract_fields(
    result: AcceptedResult | Mapping[str, Any]
) -> list[str]:
    """Chaves de `contract` fora de `CONTRACT_FIELDS`, em ordem estável.

    `results_to_claims` iterava `CONTRACT_FIELDS` e só isso: um campo novo
    dentro de `contract` — aceito pelo schema do coordenador via
    `ResultSchema.with_extra`/perfil — atravessava a integração inteira sem
    virar claim e sem aparecer em lugar nenhum. Perda silenciosa: o worker
    respondeu, o operador nunca soube. Esta função é o que torna a perda
    VISÍVEL; `results_to_claims` é o que a torna verificável.

    `CONTRACT_FIELDS` NÃO é alterada: campo novo não vira vocabulário do §6.3
    por ter aparecido uma vez num resultado.
    """
    output, _, _ = _result_parts(result)
    contract = output.get("contract")
    if not isinstance(contract, Mapping):
        return []
    conhecidos = set(CONTRACT_FIELDS)
    return sorted({str(k) for k in contract if str(k) not in conhecidos})


def _degraded_assertions(campo: str, raw: Any) -> list[_Assertion]:
    """Afirmações de um campo FORA do §6.3, sem estrutura mecânica.

    Não passa por `ContractField` (que recusa nome fora de `CONTRACT_FIELDS`)
    e não deriva `statement_fields`: a estrutura de um campo que ninguém
    declarou não é derivável, e inventá-la faria `check_support` julgar um
    formato imaginado. Enunciado, citação e símbolo são preservados — é o que
    faz o campo desconhecido virar claim (tipicamente `unresolved`) em vez de
    sumir.
    """
    field_evidence, field_symbol = _citations(
        _evidence_dicts(raw if isinstance(raw, Mapping) else {})
    )
    items: list[Any] = []
    if isinstance(raw, Mapping):
        for key in _ASSERTION_KEYS:
            value = raw.get(key)
            if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
                items = list(value)
                break
    elif isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        items = list(raw)
    if not items:
        content = ""
        if isinstance(raw, Mapping):
            content = str(raw.get("content") or "")
        elif isinstance(raw, str):
            content = raw
        items = _split_statements(content)

    out: list[_Assertion] = []
    for item in items:
        if isinstance(item, Mapping):
            statement = ""
            for key in _STATEMENT_KEYS:
                statement = _norm_ws(item.get(key))
                if statement:
                    break
            if not statement:
                statement = _norm_ws(item.get("value"))
            own_evidence, own_symbol = _citations(_evidence_dicts(item))
            evidence = own_evidence or field_evidence
            symbol = _norm_ws(item.get("symbol")) or own_symbol or field_symbol
            nomeado = item
        else:
            statement = _norm_ws(item)
            evidence = field_evidence
            symbol = field_symbol
            nomeado = {}
        if len(statement) < 3:
            continue
        out.append(
            _Assertion(
                campo=campo,
                statement=statement,
                subject_name=_named_subject(nomeado, statement),
                statement_fields={},
                evidence=evidence,
                symbol=symbol or None,
            )
        )
    return out


def results_to_claims(
    result: AcceptedResult | Mapping[str, Any],
    objective: InvestigationObjective | Mapping[str, Any] | None = None,
) -> list[Claim]:
    """Contrato §6.3 do resultado -> `Claim`s de `analysis.verification`.

    `predicate_kind` sai de `FIELD_PREDICATE_KIND` (o campo diz que TIPO de
    afirmação é aquilo), `asserted_by` é o worker e `claim_id` é conteúdo:
    `objetivo:campo:hash do enunciado`. Isso é o que faz reintegrar o mesmo
    resultado cair no mesmo `fact_id` em vez de gerar um segundo fato.

    Afirmação sem `evidence_refs` VIRA claim mesmo assim — sem checagem
    aplicável, portanto — para que `verify_claim` a marque `unresolved` e a
    lacuna fique registrada. Descartá-la aqui apagaria a pergunta em aberto.
    """
    output, asserted_by, objective_id = _result_parts(result)
    obj = _objective_of(objective, output, objective_id)
    objective_id = objective_id or obj.objective_id

    claims: list[Claim] = []
    seen: set[str] = set()
    for campo in CONTRACT_FIELDS:
        predicate_kind = FIELD_PREDICATE_KIND.get(campo)
        if predicate_kind is None:  # `lacunas` — não é afirmação a verificar
            continue
        merged = _merged_field(obj, output, campo)
        if merged.status in (ContractFieldStatus.EXCLUDED, ContractFieldStatus.UNRESOLVED):
            continue
        raw = _field_payload(output, campo)
        for assertion in _assertions_of(campo, raw, merged):
            claim_id = f"{objective_id}:{campo}:{_hash(assertion.statement)[:12]}"
            if claim_id in seen:
                continue
            seen.add(claim_id)
            try:
                claims.append(
                    Claim(
                        claim_id=claim_id,
                        subject=assertion.symbol or "",
                        predicate_kind=predicate_kind,
                        statement_fields=dict(assertion.statement_fields),
                        evidence_refs=assertion.evidence,
                        asserted_by=asserted_by,
                        statement=assertion.statement,
                        symbol=assertion.symbol,
                        scope=f"{objective_id}:{campo}",
                    )
                )
            except VerificationError:
                # Campo fora do schema fechado do claim: a afirmação vira
                # claim SEM estrutura mecânica em vez de sumir.
                claims.append(
                    Claim(
                        claim_id=claim_id,
                        subject=assertion.symbol or "",
                        predicate_kind=predicate_kind,
                        statement_fields={},
                        evidence_refs=assertion.evidence,
                        asserted_by=asserted_by,
                        statement=assertion.statement,
                        symbol=assertion.symbol,
                        scope=f"{objective_id}:{campo}",
                    )
                )

    # Campos NÃO previstos no §6.3: viram claim degradado (sem
    # `statement_fields`, `predicate_kind` genérico). Ficam sujeitos à mesma
    # verificação que qualquer outro — o que muda é que não se finge conhecer
    # a estrutura deles. `integrate` conta o mesmo conjunto em
    # `IntegrationReport.campos_nao_previstos`.
    for campo in unforeseen_contract_fields(output):
        for assertion in _degraded_assertions(campo, _field_payload(output, campo)):
            claim_id = f"{objective_id}:{campo}:{_hash(assertion.statement)[:12]}"
            if claim_id in seen:
                continue
            seen.add(claim_id)
            claims.append(
                Claim(
                    claim_id=claim_id,
                    subject=assertion.symbol or "",
                    predicate_kind=UNFORESEEN_PREDICATE_KIND,
                    statement_fields={},
                    evidence_refs=assertion.evidence,
                    asserted_by=asserted_by,
                    statement=assertion.statement,
                    symbol=assertion.symbol,
                    scope=f"{objective_id}:{campo}",
                )
            )
    return claims


def _result_parts(
    result: AcceptedResult | Mapping[str, Any]
) -> tuple[dict[str, Any], str, str]:
    if isinstance(result, AcceptedResult):
        return dict(result.output), result.asserted_by, result.objective_id
    data = _as_mapping(result)
    output = _as_mapping(data.get("output")) or data
    asserted_by = str(data.get("asserted_by") or DEFAULT_ASSERTED_BY)
    return output, asserted_by, str(output.get("objective_id") or "")


def _rebuild_objective(
    data: Mapping[str, Any],
    *,
    origem: str,
    trust_profile_cells: bool,
    objective_id: str = "",
    task_id: str = "",
    rejeicoes: list[dict[str, Any]] | None = None,
) -> InvestigationObjective | None:
    """`InvestigationObjective.from_dict` com o erro virando DIAGNÓSTICO TIPADO.

    `FailureEdgeMatrix.from_dict` passou a levantar `MatrixIntegrityError`
    quando o payload do agente traz célula/família sem justificativa (§6.5), e
    `InvestigationObjective.from_dict` a chama. Duas saídas eram inaceitáveis:
    deixar a exceção subir (uma matriz malformada de UM agente derrubaria a
    integração de TODOS os objetivos da revisão) e engolir com `except
    Exception: pass` (o objetivo voltava ao estado anterior e ninguém ficava
    sabendo por quê — a mesma perda silenciosa dos campos não previstos).

    Devolve `None` quando não deu para reconstruir; o chamador FICA COM O
    OBJETIVO ANTERIOR e o motivo entra em `rejeicoes` com `objective_id`,
    `task_id` e o NOME da exceção (`MatrixIntegrityError`,
    `MatrixJustificationRequired`, …), que é o que torna o diagnóstico tipado
    e não um texto solto.

    `trust_profile_cells` é decidido pela PROVENIÊNCIA DA MATRIZ que está em
    `data`, nunca pelo chamador em bloco:

    * `True` para matriz escrita pelo pipeline — o objetivo do plano
      (`escopo`), o `objective_json` da tarefa (`objective_payload`), o estado
      consolidado (`estado_acumulado`) e a matriz ANTERIOR reimposta na
      segunda tentativa. Ali `justification_source` foi posto por
      `investigation._matrix_for(profile)`, e recusá-lo seria rejeitar a
      própria exclusão que o operador configurou.
    * `False` para a matriz que veio no resultado do AGENTE. Sem isso, bastava
      o worker escrever `justification_source: "profile:cli"` + `note` para uma
      célula sair de `unmet_obligations()` — a exclusão de perfil forjada pelo
      próprio investigado.
    """
    try:
        return InvestigationObjective.from_dict(
            data, trust_profile_cells=trust_profile_cells
        )
    except Exception as exc:  # noqa: BLE001 — vira diagnóstico, não crash
        if rejeicoes is not None:
            rejeicoes.append(
                {
                    "objective_id": str(objective_id or data.get("objective_id") or ""),
                    "task_id": str(task_id or ""),
                    "origem": origem,
                    "erro": type(exc).__name__,
                    "contrato": isinstance(exc, InvestigationError),
                    "motivo": str(exc),
                }
            )
        return None


def _objective_of(
    objective: InvestigationObjective | Mapping[str, Any] | None,
    output: Mapping[str, Any],
    objective_id: str,
    *,
    task_id: str = "",
    rejeicoes: list[dict[str, Any]] | None = None,
) -> InvestigationObjective:
    if isinstance(objective, InvestigationObjective):
        return objective
    if isinstance(objective, Mapping) and objective.get("objective_id"):
        # Payload parcial (o `objective_json` da tarefa nem sempre traz o
        # pacote inteiro) cai para o objetivo mínimo em vez de derrubar a
        # integração dos demais objetivos — mas COM motivo registrado.
        reconstruido = _rebuild_objective(
            objective,
            origem="objective_payload",
            trust_profile_cells=True,
            objective_id=objective_id,
            task_id=task_id,
            rejeicoes=rejeicoes,
        )
        if reconstruido is not None:
            return reconstruido
    return InvestigationObjective(
        objective_id=objective_id or str(output.get("objective_id") or "objetivo"),
        kind="capability",
        capability_id=str(output.get("capability_id") or ""),
        name=str(output.get("capability_id") or objective_id or ""),
    )


# --------------------------------------------------------------------------
# Estado do objetivo (achado nº6)
# --------------------------------------------------------------------------


def _reading_satisfied_entries(output: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Campo opcional `reading_satisfied` do resultado, normalizado.

    É DADO, não comando: cada item diz qual obrigação o worker afirma ter
    cumprido e com que evidência. Nada aqui fecha obrigação por si — quem fecha
    é `_satisfy_readings`, e só depois de resolver a evidência no snapshot.
    """
    raw = output.get("reading_satisfied")
    if isinstance(raw, Mapping):
        raw = [raw]
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return []
    out: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, Mapping):
            out.append(dict(item))
        elif isinstance(item, str) and item.strip():
            out.append({"need_id": item.strip()})
    return out


def _satisfy_readings(
    data: dict[str, Any], output: Mapping[str, Any], snapshot: Snapshot | None
) -> list[dict[str, Any]]:
    """Fecha as obrigações de leitura que o resultado EVIDENCIOU (achado nº3).

    Declaração não fecha nada: cada `evidence_refs` vira `ClaimEvidence` e passa
    por `check_location` contra o snapshot — localizador que não resolve (ou
    cujo `snippet_hash` não bate) deixa a obrigação ABERTA, e ela continua em
    `unmet_obligations()`. É a mesma porta que o resto do módulo usa para
    evidência; um worker não fecha obrigação escrevendo que leu.
    """
    entries = _reading_satisfied_entries(output)
    needs = data.get("reading_needs")
    if not entries or not isinstance(needs, list):
        return []

    fechadas: list[dict[str, Any]] = []
    for entry in entries:
        need_id = _norm_ws(entry.get("need_id"))
        target = _norm_ws(entry.get("target"))
        alvo = [
            n
            for n in needs
            if isinstance(n, Mapping)
            and not n.get("satisfied")
            and not (n.get("waived_reason") or "")
            and (
                (need_id and str(n.get("need_id") or "") == need_id)
                or (not need_id and target and _norm_ws(n.get("target")) == target)
            )
        ]
        if not alvo:
            continue
        spans: list[str] = []
        motivos: list[str] = []
        for raw in _evidence_dicts(entry) or ([entry] if entry.get("path") else []):
            citation = _claim_evidence(raw)
            if citation is None:
                motivos.append("evidência sem localizador utilizável (path + faixa de linhas)")
                continue
            if snapshot is None:
                motivos.append("sem snapshot para resolver a evidência")
                continue
            loc = check_location(citation, snapshot)
            if loc.ok:
                spans.append(loc.span)
            else:
                motivos.append(f"{citation.span}: {loc.reason}")
        registro = {
            "need_id": need_id or str(alvo[0].get("need_id") or ""),
            "target": target or _norm_ws(alvo[0].get("target")),
            "satisfeita": bool(spans),
            "evidencia": spans,
            "motivo": "; ".join(motivos[:3]),
        }
        if spans:
            nota = "leitura satisfeita com evidência resolvida no snapshot: " + ", ".join(spans)
            for need in alvo:
                need["satisfied"] = True
                need["satisfied_note"] = nota
        else:
            registro["motivo"] = (
                registro["motivo"]
                or "reading_satisfied sem evidence_refs: declaração não fecha obrigação"
            )
        fechadas.append(registro)
    return fechadas


def _seed_with_accumulated(
    objective: InvestigationObjective,
    accumulated: Mapping[str, Any] | None,
    *,
    rejeicoes: list[dict[str, Any]] | None = None,
) -> InvestigationObjective:
    """Objetivo do índice + contrato JÁ APURADO em rodadas anteriores (§7.1).

    Sem isto, cada integração partia do objetivo PRISTINO do plano: o campo que
    a rodada A preencheu voltava a `pending` na rodada B, e o worker de B
    recebia um contrato vazio — o §7.2 exige o contrário ("enviar o contrato
    acumulado COM conteúdo").

    `accumulated` é o `contract` do `runtime.state.ConsolidatedState` (nome do
    campo -> `{content, evidence_refs, status, impacto}`). Conteúdo vazio nunca
    sobrescreve conteúdo apurado; campo desconhecido do contrato é ignorado
    (este módulo não inventa campo de contrato a partir de estado externo).
    """
    contract = dict((accumulated or {}).get("contract") or accumulated or {})
    if not contract:
        return objective
    data = objective.to_dict()
    campos = dict(data.get("contract") or {})
    mudou = False
    for name in CONTRACT_FIELDS:
        incoming = contract.get(name)
        if not isinstance(incoming, Mapping):
            continue
        conteudo = str(incoming.get("content") or "").strip()
        if not conteudo:
            continue
        atual = dict(campos.get(name) or {"name": name, "label": CONTRACT_LABELS.get(name, "")})
        if str(atual.get("content") or "").strip():
            continue  # o índice já traz conteúdo: não sobrescrever
        atual["name"] = name
        atual.setdefault("label", CONTRACT_LABELS.get(name, ""))
        atual["content"] = conteudo
        atual["status"] = str(incoming.get("status") or ContractFieldStatus.FILLED.value)
        refs = [d for d in (_as_evidence_ref_dict(r) for r in _evidence_dicts(incoming)) if d]
        if refs:
            atual["evidence_refs"] = refs
        campos[name] = atual
        mudou = True
    if not mudou:
        return objective
    data["contract"] = campos
    reconstruido = _rebuild_objective(
        data,
        origem="estado_acumulado",
        trust_profile_cells=True,
        objective_id=objective.objective_id,
        rejeicoes=rejeicoes,
    )
    return objective if reconstruido is None else reconstruido


def _sanitize_agent_matrix(
    raw: Mapping[str, Any], anterior: InvestigationObjective
) -> tuple[dict[str, Any], list[str], list[str]]:
    """Matriz do agente CÉLULA A CÉLULA, antes de `from_dict(trust=False)`.

    `trust_profile_cells=False` recusa a matriz INTEIRA assim que encontra uma
    célula com `justification_source`. Isso é fail-closed correto e caro: um
    resultado que apenas ECOA uma exclusão de perfil já existente — coisa que
    um worker que recebeu a matriz no envelope faz naturalmente — derrubava
    junto as células novas e legítimas da mesma rodada (uma `covered` com
    `EvidenceRef` real). Progresso perdido por causa de uma linha copiada.

    Aqui a proveniência de perfil é resolvida ANTES, por célula:

    * eco IDÊNTICO de célula de perfil anterior — sai do payload (inócuo) e
      volta intacto por `reapply_profile_cells`; nada é registrado, porque
      repetir o que já valia não é tentativa de nada;
    * eco DIVERGENTE (mesmo `justification_source`, outro estado/nota) ou
      `justification_source` em célula que não é de perfil (forja) — a célula
      volta ao que era antes (ou a `unresolved` sem marca, se não existia) e
      entra em `resultados_rejeitados` com `descartado="celula"`;
    * as demais seguem inalteradas para `from_dict`, que continua julgando
      estado e evidência com o mesmo rigor.

    Devolve `(matriz_saneada, revertidas, ecoadas)`.
    """
    perfil = anterior.matrix.profile_cells()
    anteriores = anterior.matrix.cells
    saneadas: list[Any] = []
    revertidas: list[str] = []
    ecoadas: list[str] = []
    for bruta in raw.get("cells", ()) or ():
        if not isinstance(bruta, Mapping):
            # Formato fora do contrato: quem recusa é `from_dict`, não este
            # saneamento — inventar uma célula aqui esconderia o erro real.
            saneadas.append(bruta)
            continue
        familia = str(bruta.get("family", ""))
        item = str(bruta.get("item", ""))
        fonte = str(bruta.get("justification_source") or "").strip()
        if not fonte:
            saneadas.append(dict(bruta))
            continue
        chave = (familia, item)
        rotulo = f"{familia}/{item}"
        anterior_celula = anteriores.get(chave)
        if (
            chave in perfil
            and anterior_celula is not None
            and dict(bruta) == anterior_celula.to_dict()
        ):
            ecoadas.append(rotulo)
            continue
        revertidas.append(rotulo)
        if chave in perfil:
            # `reapply_profile_cells` reimpõe a versão ANTERIOR desta célula.
            continue
        if anterior_celula is not None:
            saneadas.append(anterior_celula.to_dict())
        # Sem célula anterior, a forjada simplesmente NÃO ENTRA. Escrever no
        # lugar dela uma célula `unresolved` sintética seria pior: numa família
        # que o §6.5 não conhece, essa célula é ela própria inválida
        # (`fora do conjunto permitido e sem justificativa`) e derrubaria a
        # matriz inteira — trocando a recusa pontual pelo fallback que esta
        # onda existe para evitar. Omitida, a célula volta a ser o que era:
        # nunca avaliada, e portanto pendente em `unmet_obligations()`.
    matriz = dict(raw)
    matriz["cells"] = saneadas
    return matriz, revertidas, ecoadas


def _objective_after_result(
    objective: InvestigationObjective,
    output: Mapping[str, Any],
    snapshot: Snapshot | None = None,
    accumulated: Mapping[str, Any] | None = None,
    *,
    task_id: str = "",
    rejeicoes: list[dict[str, Any]] | None = None,
) -> tuple[InvestigationObjective, list[dict[str, Any]]]:
    """Objetivo do PLANO + o que o resultado preencheu, sem promoção indevida.

    O `state` declarado pelo worker só entra quando PIORA o estado (`blocked`):
    o estado final vem sempre de `evaluate()`, que exige `unmet_obligations()`
    vazio para `complete`. É por isso que um worker não consegue fechar um
    objetivo declarando-o fechado.

    `reading_satisfied` (achado nº3) é o único caminho pelo qual o resultado
    fecha obrigação de leitura — e só quando a evidência resolve no snapshot.
    Devolve também o registro do que foi (e do que não foi) fechado.
    """
    objective = _seed_with_accumulated(objective, accumulated, rejeicoes=rejeicoes)
    data = objective.to_dict()
    contract = {name: _merged_field(objective, output, name).to_dict() for name in CONTRACT_FIELDS}
    data["contract"] = contract
    leituras = _satisfy_readings(data, output, snapshot)
    matrix = output.get("matrix")
    matriz_anterior = data.get("matrix")
    matriz_do_payload = isinstance(matrix, Mapping) and bool(matrix)
    revertidas: list[str] = []
    ecoadas: list[str] = []
    if matriz_do_payload:
        # Saneamento POR CÉLULA antes do schema: proveniência de perfil é
        # resolvida contra a matriz anterior, para que uma linha copiada não
        # custe as células legítimas da mesma rodada.
        data["matrix"], revertidas, ecoadas = _sanitize_agent_matrix(matrix, objective)
    declared = str(output.get("state") or "").strip().lower()
    if declared == ObjectiveState.BLOCKED.value:
        data["state"] = ObjectiveState.BLOCKED.value
    elif data.get("state") == ObjectiveState.COMPLETE.value:
        # Nunca reafirmar `complete` vindo do payload: recalcula do zero.
        data["state"] = ObjectiveState.PARTIAL.value
    # Aqui mora o resultado do AGENTE: `matrix`, contrato e estado declarados
    # por ele. Recusa de reconstrução (matriz sem justificativa, §6.5) mantém o
    # objetivo NO ESTADO ANTERIOR e devolve o motivo pelo `rejeicoes` — nunca
    # derruba a integração dos outros objetivos da mesma revisão.
    marca = len(rejeicoes) if rejeicoes is not None else 0
    reconstruido = _rebuild_objective(
        data,
        origem="resultado",
        # A matriz em `data` é do AGENTE quando ele mandou uma; senão é a do
        # objetivo anterior, escrita pelo pipeline. A confiança segue a
        # proveniência da matriz, não a do resto do payload.
        trust_profile_cells=not matriz_do_payload,
        objective_id=objective.objective_id,
        task_id=task_id,
        rejeicoes=rejeicoes,
    )
    descartado = "resultado"
    if reconstruido is None and matriz_do_payload:
        # Degradação em vez de descarte total: a matriz do payload é a parte
        # recusada, e jogar fora JUNTO o contrato apurado, as leituras fechadas
        # e o estado declarado nesta rodada seria perder trabalho válido por
        # causa de uma célula. Reconstrói com a matriz ANTERIOR (validada pelo
        # pipeline, por isso `trust_profile_cells=True`); se nem assim der, aí
        # sim o objetivo inteiro fica como estava.
        data["matrix"] = matriz_anterior
        reconstruido = _rebuild_objective(
            data, origem="resultado", trust_profile_cells=True
        )
        if reconstruido is not None:
            descartado = "matrix"
    if reconstruido is None:
        descartado = "resultado"
    if rejeicoes is not None and len(rejeicoes) > marca:
        # `descartado` diz O QUE a recusa custou: só a matriz do payload, ou a
        # rodada inteira. Sem isso o operador não sabe se precisa reenviar o
        # resultado ou só corrigir a matriz.
        rejeicoes[marca]["descartado"] = descartado
    elif reconstruido is not None and matriz_do_payload:
        # As células que o saneamento reverteu são recusa PONTUAL: o resto da
        # matriz do agente foi aceito, então o custo é a célula, não a matriz.
        if revertidas and rejeicoes is not None:
            rejeicoes.append(
                {
                    "objective_id": objective.objective_id,
                    "task_id": task_id,
                    "origem": "resultado",
                    "erro": "MatrixIntegrityError",
                    "contrato": True,
                    "descartado": "celula",
                    "celulas": list(revertidas),
                    "motivo": (
                        "proveniência de perfil não aceita do resultado (só o pipeline "
                        "escreve `justification_source`); célula revertida ao estado "
                        f"anterior: {', '.join(revertidas)}"
                    ),
                }
            )
        # A matriz do agente PASSOU no schema — o que ainda não impede que ela
        # tenha APAGADO uma exclusão de perfil (transformando decisão do
        # operador em obrigação coberta). `reapply_profile_cells` reimpõe as
        # células de perfil da matriz ANTERIOR e despe qualquer proveniência
        # que não seja dela. É a última barreira, depois do saneamento.
        _reimpor_perfil(
            reconstruido,
            objective,
            objective_id=objective.objective_id,
            task_id=task_id,
            rejeicoes=rejeicoes,
            ignorar=ecoadas,
        )
    return (objective if reconstruido is None else reconstruido), leituras


def _reimpor_perfil(
    reconstruido: InvestigationObjective,
    anterior: InvestigationObjective,
    *,
    objective_id: str,
    task_id: str,
    rejeicoes: list[dict[str, Any]] | None,
    ignorar: Sequence[str] = (),
) -> None:
    """Reimpõe as células de perfil da matriz anterior e REGISTRA o que mexeu.

    Quem decidiu que uma família não se aplica a este sistema foi o perfil de
    análise; o worker investigado não tem autoridade para revogar essa decisão
    nem para inventar uma nova em nome dela. Silenciar a reimposição seria
    aceitar a tentativa sem deixar rastro — o operador precisa saber que o
    agente tentou.
    """
    registro = reconstruido.matrix.reapply_profile_cells(anterior.matrix)
    # `ignorar` são as células que o SANEAMENTO tirou do payload por serem eco
    # idêntico do perfil: reimpô-las é operação interna, não tentativa do
    # agente, e contá-las aqui acusaria o worker de apagar o que ele copiou
    # certo.
    silenciar = {str(x) for x in ignorar}
    restauradas = [c for c in (registro.get("restored") or ()) if c not in silenciar]
    forjadas = list(registro.get("revoked_forged_provenance") or ())
    if not restauradas and not forjadas:
        return
    if rejeicoes is None:
        return
    partes: list[str] = []
    if forjadas:
        partes.append(
            "proveniência de perfil FORJADA pelo resultado, revogada (célula volta a "
            f"unresolved): {', '.join(forjadas)}"
        )
    if restauradas:
        partes.append(
            "exclusão de perfil que o resultado apagou ou alterou, reimposta: "
            f"{', '.join(restauradas)}"
        )
    rejeicoes.append(
        {
            "objective_id": objective_id,
            "task_id": task_id,
            "origem": "resultado",
            "erro": "MatrixProfileCellsReimposed",
            "contrato": True,
            # O custo é por CÉLULA: a matriz do agente foi aceita, só as
            # células de perfil voltaram ao que o pipeline decidiu.
            "descartado": "celula",
            "celulas": sorted({*restauradas, *forjadas}),
            "reimposicao": {
                "restored": restauradas,
                "revoked_forged_provenance": forjadas,
            },
            "motivo": "; ".join(partes),
        }
    )


def _objective_after_results(
    objective: InvestigationObjective,
    results: Sequence[AcceptedResult],
    snapshot: Snapshot | None = None,
    accumulated: Mapping[str, Any] | None = None,
    *,
    rejeicoes: list[dict[str, Any]] | None = None,
) -> tuple[InvestigationObjective, list[dict[str, Any]]]:
    """Dobra a cadeia de resultados sobre o objetivo — A, depois B, resulta A+B.

    Cada resultado é um DELTA sobre o objetivo que saiu do anterior (e não
    sobre o objetivo pristino do plano). Consequência direta e testável (T06):
    a rodada A fecha o campo `regra` e a rodada B fecha `impacto`; o objetivo
    integrado tem os DOIS, e a obrigação de leitura fechada por A continua
    fechada mesmo que B não a mencione — `_satisfy_readings` só marca
    `satisfied=True`, nunca `False`.
    """
    leituras: list[dict[str, Any]] = []
    atual = _seed_with_accumulated(objective, accumulated, rejeicoes=rejeicoes)
    for result in results:
        # A rodada que trouxe o payload malformado é NOMEADA (`task_id`): sem
        # isso o operador saberia que houve recusa mas não em qual rodada.
        atual, fechadas = _objective_after_result(
            atual, result.output, snapshot, task_id=result.task_id, rejeicoes=rejeicoes
        )
        leituras.extend(fechadas)
    if not results:
        atual, leituras = _objective_after_result(
            atual, {}, snapshot, rejeicoes=rejeicoes
        )
    return atual, leituras


def _lacunas_of(
    objective: InvestigationObjective,
    output: Mapping[str, Any],
    verdicts: Sequence[Verdict],
    claims_by_id: Mapping[str, Claim],
) -> list[dict[str, Any]]:
    """Tudo que ficou em aberto, com a origem de cada pendência."""
    out: list[dict[str, Any]] = []
    lacunas_field = objective.contract.get("lacunas")
    if lacunas_field is not None:
        for statement in _split_statements(lacunas_field.content):
            out.append({"origem": "contrato:lacunas", "detalhe": statement})
        if lacunas_field.status is ContractFieldStatus.UNRESOLVED and lacunas_field.impacto:
            out.append({"origem": "contrato:lacunas", "detalhe": lacunas_field.impacto})

    for name in CONTRACT_FIELDS:
        campo = objective.contract[name]
        if campo.status is ContractFieldStatus.UNRESOLVED:
            out.append(
                {
                    "origem": f"campo_nao_resolvido:{name}",
                    "detalhe": campo.impacto or "campo não resolvido sem impacto declarado",
                }
            )
    # Campo `pending` não entra aqui: já é a primeira linha de
    # `unmet_obligations()`, e repetir a mesma pendência em dois lugares faz o
    # relatório parecer maior do que a dívida real.

    for raw in (output.get("gaps"), output.get("unresolved")):
        if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
            for item in raw:
                detalhe = _norm_ws(item if not isinstance(item, Mapping) else item.get("detail") or item)
                if detalhe:
                    out.append({"origem": "worker", "detalhe": detalhe})
        elif isinstance(raw, str) and raw.strip():
            out.append({"origem": "worker", "detalhe": _norm_ws(raw)})

    for verdict in verdicts:
        if verdict.epistemic is not EpistemicStatus.UNRESOLVED:
            continue
        claim = claims_by_id.get(verdict.claim_id)
        out.append(
            {
                "origem": "afirmacao_sem_sustentacao",
                "claim_id": verdict.claim_id,
                "detalhe": (
                    (claim.statement if claim else verdict.claim_id)
                    + " — "
                    + "; ".join(verdict.reasons[:2])
                ),
            }
        )
    return out


# --------------------------------------------------------------------------
# Escrita em `knowledge.db`
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class FactWrite:
    """Um fato gravado (ou reconhecido como idêntico) por esta integração."""

    claim_id: str
    fact_id: str
    subject_id: str
    campo: str
    predicate: str
    epistemic: str
    nature: str
    changed: bool
    evidence_refs: tuple[str, ...] = ()
    entity_id: str | None = None
    #: Que PARTE da frase o veredito sustenta. Vazio quando a frase não tem
    #: consequência separável; preenchido quando só a condição ficou de pé
    #: (achado nº1) — o `value` gravado continua sendo a frase inteira, e é este
    #: campo que diz por que ela não é `supported`.
    escopo_sustentado: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "claim_id": self.claim_id,
            "fact_id": self.fact_id,
            "subject_id": self.subject_id,
            "campo": self.campo,
            "predicate": self.predicate,
            "epistemic": self.epistemic,
            "nature": self.nature,
            "changed": self.changed,
            "evidence_refs": list(self.evidence_refs),
            "entity_id": self.entity_id,
            "escopo_sustentado": self.escopo_sustentado,
        }


@dataclass
class ObjectiveOutcome:
    """Como UM objetivo terminou nesta integração."""

    objective_id: str
    capability_id: str
    subject_id: str | None
    state: str
    #: `ObjectiveKind` do objetivo (`capability`/`orphan_group`/`discovery`).
    #: O CLI precisa dele para nao contar objetivo de DESCOBERTA junto com
    #: objetivo de capacidade: no primeiro nao ha verificacao mecanica
    #: possivel, entao "0 supported" e o resultado CORRETO, nao uma falha.
    kind: str = ""
    #: Claims deste objetivo cuja citacao RESOLVEU no snapshot e apontava
    #: codigo (nem documento, nem comentario/docstring).
    claims_com_evidencia_resolvida: int = 0
    #: Claims que nao declararam nenhuma citacao. Lacuna declarada, nao erro.
    claims_sem_evidencia: int = 0
    #: Citacoes ACEITAS nesta integracao (`{path, line_start, line_end,
    #: content_kind}`), deduplicadas. Existem por uma razao operacional
    #: precisa: `runtime.state.apply_result` conta `evidence_accepted` a partir
    #: de `evidence_refs` do delta, e `Progress.has_progress` decide se a
    #: cadeia continua. Sem elas, uma rodada de DESCOBERTA que leu arquivos e
    #: citou codigo real — mas cujo campo do contrato ja estava preenchido —
    #: era classificada `no_progress` e a cadeia parava em cima de trabalho
    #: feito (§7.3). Sao as MESMAS faixas que viraram linha de `evidence` no
    #: banco: citacao recusada pelo portao nunca entra aqui.
    evidence_refs: list[dict[str, Any]] = field(default_factory=list)
    unmet: list[str] = field(default_factory=list)
    lacunas: list[dict[str, Any]] = field(default_factory=list)
    fatos: list[FactWrite] = field(default_factory=list)
    inconsistencias: list[dict[str, Any]] = field(default_factory=list)
    entidades: list[dict[str, Any]] = field(default_factory=list)
    relacoes: list[dict[str, Any]] = field(default_factory=list)
    bloqueios: list[str] = field(default_factory=list)
    #: Claims que NÃO viraram fato por incoerência de escopo (achado nº5):
    #: citação para caminho fora do snapshot corrente, sujeito de outro
    #: namespace. Cada item traz `claim_id` e `motivo`.
    rejeitados: list[dict[str, Any]] = field(default_factory=list)
    #: Obrigações de leitura fechadas por `reading_satisfied` COM evidência
    #: resolvida (achado nº3).
    leituras_satisfeitas: list[dict[str, Any]] = field(default_factory=list)
    task_id: str = ""
    execution_id: str | None = None
    integration_key: str = ""
    #: Contrato CONSOLIDADO depois desta integração (nome do campo ->
    #: `{content, status, evidence_refs, ...}`). É o "contrato acumulado com
    #: conteúdo" que o §7.2 manda enviar ao worker da rodada seguinte — e é
    #: também o delta que `runtime.state.apply_result` aplica sobre o estado.
    #: Sem ele, quem monta a continuação teria de reabrir o índice do plano e
    #: perderia justamente o que as rodadas anteriores apuraram.
    contract_state: dict[str, Any] = field(default_factory=dict)
    #: Obrigações de leitura AINDA ABERTAS, com alvo concreto — o formato que
    #: `coordinator.plan_continuations` e `runtime.state` consomem.
    reading_needs: list[dict[str, Any]] = field(default_factory=list)
    #: `task_id` de TODAS as rodadas da cadeia que esta integração dobrou.
    chain_task_ids: list[str] = field(default_factory=list)
    #: Chaves de TOPO do resultado fora de `KNOWN_OUTPUT_FIELDS` — o que o
    #: operador autorizou por perfil (`ResultSchema.with_extra`). Preservadas
    #: com o valor íntegro: o schema as aceitou, então descartá-las aqui seria
    #: aceitar na porta e jogar fora no corredor. Última rodada da cadeia
    #: vence em caso de repetição da mesma chave.
    extra: dict[str, Any] = field(default_factory=dict)
    #: Chaves dentro de `contract` fora do §6.3, com o claim degradado que
    #: cada uma gerou. Diagnóstico, não descarte.
    campos_nao_previstos: list[dict[str, Any]] = field(default_factory=list)
    #: Payloads deste objetivo que a reconstrução RECUSOU (matriz do §6.5 sem
    #: justificativa, contrato inválido): `{objective_id, task_id, origem,
    #: erro, contrato, motivo}`. O objetivo ficou no estado ANTERIOR; a recusa
    #: fica aqui em vez de virar crash da revisão inteira ou silêncio.
    resultados_rejeitados: list[dict[str, Any]] = field(default_factory=list)

    def _count(self, status: EpistemicStatus) -> int:
        return sum(1 for f in self.fatos if f.epistemic == status.value)

    def to_dict(self) -> dict[str, Any]:
        return {
            "objective_id": self.objective_id,
            "capability_id": self.capability_id,
            "subject_id": self.subject_id,
            "kind": self.kind,
            "claims_com_evidencia_resolvida": self.claims_com_evidencia_resolvida,
            "claims_sem_evidencia": self.claims_sem_evidencia,
            "evidence_refs": [dict(e) for e in self.evidence_refs],
            "task_id": self.task_id,
            "execution_id": self.execution_id,
            "integration_key": self.integration_key,
            "state": self.state,
            "fatos_gravados": len(self.fatos),
            "fatos_novos": sum(1 for f in self.fatos if f.changed),
            "supported": self._count(EpistemicStatus.SUPPORTED),
            "inferred": self._count(EpistemicStatus.INFERRED),
            "disputed": self._count(EpistemicStatus.DISPUTED),
            "unresolved": self._count(EpistemicStatus.UNRESOLVED),
            "unmet": list(self.unmet),
            "lacunas": list(self.lacunas),
            "inconsistencias": list(self.inconsistencias),
            "entidades": list(self.entidades),
            "relacoes": list(self.relacoes),
            "bloqueios": list(self.bloqueios),
            "rejeitados": list(self.rejeitados),
            "leituras_satisfeitas": list(self.leituras_satisfeitas),
            "contract_state": dict(self.contract_state),
            "reading_needs": list(self.reading_needs),
            "chain_task_ids": list(self.chain_task_ids),
            "extra": dict(self.extra),
            "campos_nao_previstos": list(self.campos_nao_previstos),
            "resultados_rejeitados": list(self.resultados_rejeitados),
            "fatos": [f.to_dict() for f in self.fatos],
        }


@dataclass
class IntegrationReport:
    """O que a integração fez — serializável, para o CLI consumir e imprimir."""

    objetivos: list[ObjectiveOutcome] = field(default_factory=list)
    revisao: str | None = None
    mudancas: int = 0
    reread_obligations: list[dict[str, Any]] = field(default_factory=list)
    verificacao: dict[str, Any] = field(default_factory=dict)
    bloqueios: list[str] = field(default_factory=list)
    #: Resultados aceitos pelo coordenador que esta integração NÃO processou,
    #: com o motivo (achado nº5): objetivo fora do escopo pedido, ou entradas
    #: que não são as do snapshot corrente. Descarte silencioso seria o mesmo
    #: erro com outra aparência.
    descartados: list[dict[str, Any]] = field(default_factory=list)
    #: Campos dentro de `contract` que NÃO estão no §6.3, um item por
    #: (objetivo, campo): `{objective_id, campo, claims, task_ids}`. Antes,
    #: `results_to_claims` iterava só `CONTRACT_FIELDS` e a chave nova sumia
    #: sem deixar rastro — o operador não tinha como saber que o worker havia
    #: respondido algo que a integração ignorou.
    campos_nao_previstos: list[dict[str, Any]] = field(default_factory=list)
    #: Reconstruções de objetivo RECUSADAS, de qualquer origem (`escopo`,
    #: `objective_payload`, `estado_acumulado`, `resultado`). Uma matriz do
    #: §6.5 sem justificativa num resultado de agente entra aqui com
    #: `erro="MatrixIntegrityError"`, `objective_id` e `task_id`; o objetivo
    #: permanece no estado anterior e os demais seguem sendo integrados.
    resultados_rejeitados: list[dict[str, Any]] = field(default_factory=list)

    @property
    def fatos_gravados(self) -> int:
        return sum(len(o.fatos) for o in self.objetivos)

    def to_dict(self) -> dict[str, Any]:
        return {
            "revisao": self.revisao,
            "mudancas": self.mudancas,
            "fatos_gravados": self.fatos_gravados,
            "objetivos": [o.to_dict() for o in self.objetivos],
            "reread_obligations": list(self.reread_obligations),
            "verificacao": dict(self.verificacao),
            "bloqueios": list(self.bloqueios),
            "descartados": list(self.descartados),
            "campos_nao_previstos": list(self.campos_nao_previstos),
            "resultados_rejeitados": list(self.resultados_rejeitados),
        }


def _objective_kind(objective: Any) -> str:
    """`kind` do objetivo como STRING, venha ele como enum ou como texto."""
    kind = getattr(objective, "kind", "")
    return str(getattr(kind, "value", kind) or "")


def _is_discovery(objective: Any) -> bool:
    """Objetivo de DESCOBERTA (modulo sem extrator)?

    O que muda para ele, e so para ele: (1) nao existe gramatica extraida
    contra a qual conferir a afirmacao, entao o teto epistemico e `inferred`
    (`_cap_discovery_verdict`); (2) a entidade que nasce dele e uma AFIRMACAO
    do agente, entao entra com ciclo de vida nao confirmado (`proposed`) e so
    existe se houver evidencia de codigo resolvida; (3) claim sem nenhuma
    evidencia de codigo valida e RECUSADO em vez de virar fato `unresolved` —
    num objetivo de descoberta, afirmacao sem citacao e so opiniao do modelo.
    """
    return _objective_kind(objective) == DISCOVERY_KIND


@dataclass(frozen=True)
class _EvidenceGate:
    """Veredito do portao de evidencia de UM claim (§5.4, F10).

    `locations` sao as citacoes que RESOLVERAM apontando codigo. `rejeicao` e
    o diagnostico TIPADO quando nenhuma resolveu — nunca `None` junto de
    `locations` vazio e `declarou=True`.
    """

    locations: tuple[LocationResult, ...] = ()
    problemas: tuple[Mapping[str, Any], ...] = ()
    declarou: bool = False

    @property
    def resolvida(self) -> bool:
        return bool(self.locations)


def _evidence_problem(
    tipo: str, path: str, start: int, end: int, motivo: str
) -> dict[str, Any]:
    """Diagnostico de UMA citacao recusada — sempre com path e linhas."""
    return {
        "tipo": tipo,
        "path": path,
        "line_start": int(start),
        "line_end": int(end),
        "motivo": motivo,
    }


def _location_is_code(loc: LocationResult) -> tuple[bool, str]:
    """A faixa RESOLVIDA e codigo? `(ok, motivo da recusa)`.

    Duas recusas distintas, ambas mecanicas: caminho de documento (extensao) e
    faixa cujo texto, removidos comentario e literal de documentacao, fica
    vazia. A segunda usa `is_comment_only`, que cobre familia por familia
    (C-like, Python, hash, SQL/Sybase, COBOL coluna 7 e `*>`, JCL `//*`, XML)
    e cai em `generic` no desconhecido.
    """
    if is_doc_path(loc.path):
        return False, (
            f"{loc.path} e arquivo de documentacao: documento registra intencao, "
            "nunca comportamento implementado (§5.4, F10)"
        )
    if is_comment_only(loc.snippet, loc.language or loc.path):
        return False, (
            f"faixa {loc.start_line}..{loc.end_line} de {loc.path} contem apenas "
            "comentario/docstring: removido o comentario nao sobra codigo — "
            "comentario afirma intencao do autor, nao comportamento do sistema"
        )
    return True, ""


def _code_evidence(
    claim: Claim, verdict: Verdict, versions: "_SourceVersions"
) -> _EvidenceGate:
    """Citacoes do claim que sustentam: resolvidas, no snapshot e em CODIGO.

    Reaproveita `Verdict.locations` (calculadas por `verify_claim` com o mesmo
    `check_location`) e so recalcula quando o veredito nao as traz — resolver
    duas vezes com criterios diferentes seria ter dois conceitos de "resolve".
    """
    refs = list(claim.evidence_refs or ())
    if not refs:
        return _EvidenceGate(declarou=False)

    locs = list(verdict.locations or ())
    if len(locs) != len(refs):
        locs = [check_location(ref, versions.snapshot) for ref in refs]

    boas: list[LocationResult] = []
    problemas: list[Mapping[str, Any]] = []
    for ref, loc in zip(refs, locs):
        if is_doc_path(ref.path):
            problemas.append(
                _evidence_problem(
                    REJECT_EVIDENCE_NOT_CODE,
                    ref.path,
                    ref.start_line,
                    ref.end_line,
                    f"{ref.path} e arquivo de documentacao (§5.4, F10)",
                )
            )
            continue
        if not versions.in_snapshot(ref.path):
            problemas.append(
                _evidence_problem(
                    REJECT_EVIDENCE_OUT_OF_SNAPSHOT,
                    ref.path,
                    ref.start_line,
                    ref.end_line,
                    f"{ref.path} nao esta no snapshot desta integracao",
                )
            )
            continue
        if loc is None or not loc.ok:
            problemas.append(
                _evidence_problem(
                    REJECT_EVIDENCE_UNRESOLVED,
                    ref.path,
                    ref.start_line,
                    ref.end_line,
                    (loc.reason if loc is not None else "citacao sem resultado de localizacao")
                    or "faixa nao resolve no snapshot",
                )
            )
            continue
        ok, motivo = _location_is_code(loc)
        if not ok:
            problemas.append(
                _evidence_problem(
                    REJECT_EVIDENCE_NOT_CODE, ref.path, ref.start_line, ref.end_line, motivo
                )
            )
            continue
        boas.append(loc)
    return _EvidenceGate(
        locations=tuple(boas), problemas=tuple(problemas), declarou=True
    )


def _cap_discovery_verdict(verdict: Verdict) -> Verdict:
    """Teto epistemico do objetivo de descoberta: NUNCA `supported`.

    Num modulo sem extrator nao existe gramatica extraida (simbolo, aresta,
    contrato) contra a qual conferir a afirmacao — o que existe e o texto do
    arquivo, lido pelo modelo. Uma checagem lexica que "passe" ali nao e
    verificacao mecanica do sistema, e por isso o resultado maximo e
    `inferred`: afirmacao DO AGENTE, com proveniencia e com a citacao
    resolvida anexada, jamais sustentacao registrada pelo pipeline.
    """
    if verdict.epistemic is not EpistemicStatus.SUPPORTED:
        return verdict
    return replace(
        verdict,
        epistemic=EpistemicStatus.INFERRED,
        nature=None,
        support_recorded_by=None,
        external_behavior_supported=False,
        reasons=tuple(verdict.reasons)
        + (
            "objetivo de descoberta (modulo sem extrator): nao ha gramatica "
            "extraida para verificacao mecanica — teto `inferred`, afirmacao do "
            "agente com proveniencia (§5.3)",
        ),
    )


def _nature_for(claim: Claim, verdict: Verdict, supported: bool) -> FactNature:
    """Natureza do fato. `implemented` SÓ com veredito `supported`.

    `verify_claim` só emite `SUPPORTED` com `nature=IMPLEMENTED` quando a
    localização é executável (§5.4); reproduzir a decisão aqui seria duplicar
    a regra — este ponto apenas se recusa a inventar natureza melhor do que o
    veredito. Sem sustentação, a afirmação continua sendo o que é: algo
    DECLARADO por quem investigou, que `knowledge.query._bucket_of` mantém
    fora de `implemented_current`.
    """
    if supported and verdict.nature is not None:
        return verdict.nature
    if claim.predicate_kind == "test":
        return FactNature.TEST_EXPECTATION
    return FactNature.DECLARED_REQUIREMENT


def _test_assertions(snippet: str) -> list[str]:
    lines = [l.strip() for l in (snippet or "").splitlines() if l.strip()]
    marked = [l for l in lines if _ASSERT_LINE.search(l)]
    return (marked or lines)[:20]


def _evidence_for_location(
    namespace: str, source_version_id: str | None, loc: LocationResult, symbol: str | None
) -> Evidence | None:
    """`LocationResult` verificado -> `knowledge.models.Evidence`.

    O `content_kind` usado é o DETECTADO por `check_location`, nunca o
    declarado: é o que impede um comentário citado de entrar no banco como
    evidência executável. Trecho de teste vira fonte `test` (cujo
    `supports_implemented` é `False` por construção), configuração vira fonte
    `config`.
    """
    if not loc.ok or not loc.locator or not source_version_id:
        return None
    kind = loc.content_kind
    try:
        if kind is ContentKind.TEST_ASSERTION:
            locator: dict[str, Any] = {
                "case": f"{loc.path}:{loc.start_line}-{loc.end_line}",
                "assertions": _test_assertions(loc.snippet),
                "version": source_version_id,
            }
            return ev_mod.make_evidence(
                namespace, SourceKind.TEST, kind, source_version_id, locator
            )
        if kind is ContentKind.CONFIG_VALUE:
            locator = {
                "file": loc.path,
                "key": symbol or f"lines:{loc.start_line}-{loc.end_line}",
                "version": source_version_id,
            }
            return ev_mod.make_evidence(
                namespace, SourceKind.CONFIG, kind, source_version_id, locator
            )
        locator = dict(loc.locator)
        if symbol and "symbol" not in locator:
            locator["symbol"] = symbol
        return ev_mod.make_evidence(
            namespace, SourceKind.CODE, kind, source_version_id, locator
        )
    except KnowledgeError:
        # `LocatorInvalid` (localizador fora do schema §5.4) e qualquer outro
        # invariante: citação que não vira evidência válida não vira evidência
        # aproximada — o claim segue sem sustentação.
        return None


class _SourceVersions:
    """Cache de `source_version_id` por arquivo, com a MESMA fórmula de
    `wk analyze` (`register_source(path)` + versão rotulada `sha256`).

    Sem a mesma fórmula, o fato de investigação citaria uma versão de fonte
    diferente da que a análise estrutural citou para o mesmo arquivo, e a
    invalidação por versão (`knowledge.invalidate`) pegaria só metade.
    """

    def __init__(self, repo: Any, namespace: str, snapshot: Snapshot) -> None:
        self.repo = repo
        self.namespace = namespace
        #: O snapshot inteiro (nao so o mapa de arquivos): o portao de
        #: evidencia (`_code_evidence`) precisa RESOLVER faixa de linhas
        #: (`check_location`), nao apenas saber que o caminho existe.
        self.snapshot = snapshot
        self.files = snapshot.file_map()
        self.cache: dict[str, str | None] = {}

    @staticmethod
    def normalize(path: str) -> str:
        return (path or "").replace("\\", "/").strip("/")

    def in_snapshot(self, path: str) -> bool:
        """O caminho pertence ao snapshot corrente? (achado nº5)

        Citação para arquivo que não está aqui é citação de OUTRO escopo — o
        outro repositório do mesmo store, ou uma árvore anterior.
        """
        return self.normalize(path) in self.files

    def sha_of(self, path: str) -> str:
        entry = self.files.get(self.normalize(path))
        return str(getattr(entry, "sha256", "") or "") if entry is not None else ""

    def get(self, path: str) -> str | None:
        norm = self.normalize(path)
        if norm in self.cache:
            return self.cache[norm]
        entry = self.files.get(norm)
        sha = getattr(entry, "sha256", None) if entry is not None else None
        if not sha:
            self.cache[norm] = None
            return None
        source = self.repo.register_source(self.namespace, SourceKind.CODE, uri=norm)
        version = self.repo.register_source_version(
            source, version_label="sha256", content_hash=sha
        )
        self.cache[norm] = version.source_version_id
        return version.source_version_id


def integrate(
    repo: Any,
    task_store: rt_tasks.TaskStore,
    snapshot: Snapshot,
    extraction: Any,
    namespace: str,
    *,
    objectives: Iterable[Any] | None = None,
    capability_entity_map: Mapping[str, str] | None = None,
    results: Sequence[AcceptedResult] | None = None,
    objective_ids: Sequence[str] | None = None,
    expected_input_versions_hash: str | Mapping[str, str] | None = None,
    accumulated_state: Mapping[str, Any] | None = None,
    reason: str = "integração de resultados de investigação",
    create_missing_capability: bool = True,
) -> IntegrationReport:
    """Resultados aceitos -> fatos verificados, numa ÚNICA revisão atômica.

    ESCOPO É OBRIGATÓRIO (achado nº5). `objectives` (ou `objective_ids`) define
    o conjunto que esta integração pode gravar; resultado de objetivo fora dele
    vai para `IntegrationReport.descartados` com motivo, e nada é escrito por
    ele. Sem escopo declarado, a integração inteira é bloqueada: um `runtime.db`
    compartilhado guarda tarefas de todos os repositórios já analisados, e
    `collect_results` sem filtro devolvia as do repositório vizinho — foi assim
    que a integração do repositório B recriou a regra do A no namespace do B.

    Também é descartado o resultado cujas ENTRADAS não são as do snapshot
    corrente: `expected_input_versions_hash` quando o chamador o conhece, e, em
    todo caso, os `source_version_ids` (`caminho@sha256`) declarados na tarefa,
    conferidos arquivo a arquivo contra `snapshot.file_map()`.

    `capability_entity_map` (capability_id -> entity_id) é parâmetro, e não
    consulta ao `wk`: quem chama já resolveu a identidade da capacidade e este
    módulo não deve reimplementar aquela decisão. Sem entrada no mapa, tenta-se
    o próprio `capability_id` como `entity_id` (é a convenção que
    `wk._write_structural_knowledge` usa ao gravar `EntityDraft(entity_id=
    cap.capability_id)`), e só então cria-se a Capability que falta — sempre
    conferindo que a entidade reutilizada é DESTE namespace.

    A revisão é uma só de propósito: ou todos os fatos desta integração entram
    com a mesma proveniência, ou nenhum entra (§4.2).
    """
    report = IntegrationReport()
    index = _objectives_index(objectives, rejeicoes=report.resultados_rejeitados)
    scope = _scope_of(objectives, objective_ids)
    if not scope:
        report.bloqueios.append(
            "integração sem escopo: `objectives` (ou `objective_ids`) é obrigatório — "
            "sem ele, resultados de OUTRO repositório no mesmo runtime.db seriam gravados "
            "neste namespace (achado nº5). Nada foi integrado."
        )
        return report

    versions = _SourceVersions(repo, namespace, snapshot)
    # Coleta SEM filtro e descarta aqui: o filtro dentro de `collect_results`
    # deixaria o descarte invisível, e "não integrei o resultado do outro
    # repositório" é informação, não silêncio (achado nº5).
    # `latest_only=False` (§7.1): a cadeia INTEIRA de rodadas do objetivo, não
    # só a última tarefa. Manter só a última era descartar o que a rodada
    # anterior já tinha apurado.
    collected = (
        list(results) if results is not None else collect_results(task_store, latest_only=False)
    )
    accepted: list[AcceptedResult] = []
    for result in collected:
        motivo = _out_of_scope_reason(result, scope, versions, expected_input_versions_hash)
        if motivo:
            report.descartados.append(
                {
                    "objective_id": result.objective_id,
                    "task_id": result.task_id,
                    "capability_id": result.capability_id,
                    "input_versions_hash": result.input_versions_hash,
                    "motivo": motivo,
                }
            )
            continue
        accepted.append(result)

    if not accepted:
        report.bloqueios.append(
            "nenhum resultado aceito em runtime.db (tarefas done de investigação/verificação "
            "com resultado) dentro do escopo pedido: nada a integrar"
            + (f"; {len(report.descartados)} resultado(s) descartado(s)" if report.descartados else "")
        )
        return report

    prepared: list[dict[str, Any]] = []
    all_verdicts: list[Verdict] = []
    estado = dict(accumulated_state or {})
    for objective_id, chain in sorted(results_by_objective(accepted).items()):
        # O ÚLTIMO resultado da cadeia é quem assina a gravação (proveniência,
        # task_id, execution_id): é a rodada corrente. O CONTEÚDO, porém, vem
        # da cadeia inteira dobrada sobre o estado acumulado.
        result = chain[-1]
        # Recusas de reconstrução DESTE objetivo, para viajarem no outcome (e
        # não só no relatório agregado): o CLI imprime por objetivo.
        rejeicoes: list[dict[str, Any]] = []
        base = index.get(objective_id)
        if base is None:
            base = _objective_of(
                result.objective_payload or None,
                result.output,
                objective_id,
                task_id=result.task_id,
                rejeicoes=rejeicoes,
            )
        objective, leituras = _objective_after_results(
            base, chain, snapshot, estado.get(objective_id), rejeicoes=rejeicoes
        )
        for item in rejeicoes:
            # O `objective_id` do payload pode estar ausente justamente porque
            # o payload é inválido: o id do ESCOPO é a identidade confiável.
            if not item.get("objective_id"):
                item["objective_id"] = objective_id
        claims: list[Claim] = []
        vistos: set[str] = set()
        for rodada in chain:
            for claim in results_to_claims(rodada, objective):
                if claim.claim_id in vistos:
                    continue
                vistos.add(claim.claim_id)
                claims.append(claim)
        verdicts = [verify_claim(c, snapshot, extraction) for c in claims]
        inconsistencies: list[Inconsistency] = check_consistency(claims, verdicts)
        verdicts = apply_inconsistencies(verdicts, inconsistencies)
        by_id = {c.claim_id: c for c in claims}
        # Achado nº1: a frase inteira é reavaliada pelo componente mais fraco
        # ANTES do resumo e da gravação, para que relatório e banco contem a
        # mesma história.
        guarded: list[Verdict] = []
        escopos: dict[str, str] = {}
        for verdict in verdicts:
            claim = by_id.get(verdict.claim_id)
            if claim is None:
                guarded.append(verdict)
                continue
            adjusted, escopo = _consequence_guard(claim, verdict)
            guarded.append(adjusted)
            if escopo:
                escopos[verdict.claim_id] = escopo
        verdicts = guarded
        if _is_discovery(objective):
            # Antes do resumo E antes da gravacao: relatorio e banco contam a
            # mesma historia (mesmo motivo do achado nº1 acima).
            verdicts = [_cap_discovery_verdict(v) for v in verdicts]
        all_verdicts.extend(verdicts)
        prepared.append(
            {
                "result": result,
                "chain": chain,
                "objective": objective,
                "claims": by_id,
                "ordered_claims": claims,
                "verdicts": verdicts,
                "inconsistencies": inconsistencies,
                "escopos": escopos,
                "leituras": leituras,
                "rejeicoes": rejeicoes,
            }
        )

    report.verificacao = verification_summary(all_verdicts)
    report.reread_obligations = _reread_with_objective(prepared)

    with repo.revision(author=INTEGRATOR, reason=reason) as rev:
        for item in prepared:
            report.objetivos.append(
                _write_objective(
                    rev=rev,
                    repo=repo,
                    namespace=namespace,
                    versions=versions,
                    result=item["result"],
                    objective=item["objective"],
                    claims=item["claims"],
                    verdicts=item["verdicts"],
                    inconsistencies=item["inconsistencies"],
                    capability_entity_map=capability_entity_map or {},
                    create_missing_capability=create_missing_capability,
                    escopos=item["escopos"],
                    leituras=item["leituras"],
                    chain=item.get("chain") or (),
                    rejeicoes=item.get("rejeicoes") or (),
                )
            )
        report.revisao = rev.revision_id
        report.mudancas = rev.change_count
    for outcome in report.objetivos:
        report.campos_nao_previstos.extend(dict(c) for c in outcome.campos_nao_previstos)
        report.resultados_rejeitados.extend(dict(r) for r in outcome.resultados_rejeitados)
    return report


def _scope_of(
    objectives: Iterable[Any] | None, objective_ids: Sequence[str] | None
) -> set[str]:
    """Ids que esta integração pode gravar.

    Lê o `objective_id` do objeto BRUTO (e não só do índice reconstruído) de
    propósito: um objetivo que `InvestigationObjective.from_dict` recuse ainda
    delimita escopo — perder o id ali alargaria silenciosamente o filtro.
    """
    ids: set[str] = set()
    for raw in objectives or ():
        oid = getattr(raw, "objective_id", None)
        if oid is None and isinstance(raw, Mapping):
            oid = raw.get("objective_id")
        if oid:
            ids.add(str(oid))
    ids.update(str(o) for o in (objective_ids or ()) if str(o))
    return ids


def _out_of_scope_reason(
    result: AcceptedResult,
    scope: set[str],
    versions: "_SourceVersions",
    expected: str | Mapping[str, str] | None,
) -> str:
    """Por que este resultado NÃO pertence a esta integração (ou ``""``)."""
    if result.objective_id not in scope:
        return (
            f"objective_id {result.objective_id!r} fora do escopo desta integração "
            f"({len(scope)} objetivo(s)): resultado de outro conjunto/repositório"
        )
    want = _expected_hash_for(expected, result.objective_id)
    if want and want != result.input_versions_hash:
        return (
            f"input_versions_hash {result.input_versions_hash[:12]!r} difere do esperado "
            f"{want[:12]!r}: o resultado descreve outra versão das entradas"
        )
    declared = result.input_versions.get("source_version_ids")
    entries = (
        [str(s) for s in declared]
        if isinstance(declared, Sequence) and not isinstance(declared, (str, bytes))
        else []
    )
    foreign: list[str] = []
    for entry in entries:
        path, sep, sha = entry.rpartition("@")
        if not sep:
            path, sha = entry, ""
        if not versions.in_snapshot(path):
            foreign.append(f"{path} (fora do snapshot atual)")
        elif sha and versions.sha_of(path) != sha:
            foreign.append(f"{path} (sha divergente do snapshot atual)")
    if foreign:
        return (
            "entradas declaradas pela tarefa não pertencem ao snapshot corrente: "
            + "; ".join(foreign[:3])
            + (f" (+{len(foreign) - 3})" if len(foreign) > 3 else "")
        )
    return ""


def _objectives_index(
    objectives: Iterable[Any] | None,
    *,
    rejeicoes: list[dict[str, Any]] | None = None,
) -> dict[str, InvestigationObjective]:
    """Índice do ESCOPO. Objetivo irreconstruível não some: vira diagnóstico.

    O id continua delimitando escopo por `_scope_of` (que lê o payload bruto),
    então o resultado dele ainda é integrado — só que a partir do objetivo
    mínimo. Antes, o `continue` mudo escondia justamente a diferença entre
    "objetivo simples" e "objetivo cuja matriz o §6.5 recusou".
    """
    out: dict[str, InvestigationObjective] = {}
    for raw in objectives or ():
        if isinstance(raw, InvestigationObjective):
            out[raw.objective_id] = raw
            continue
        if not isinstance(raw, Mapping):
            continue
        obj = _rebuild_objective(
            raw, origem="escopo", trust_profile_cells=True, rejeicoes=rejeicoes
        )
        if obj is None:
            continue
        out[obj.objective_id] = obj
    return out


def _reread_with_objective(prepared: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Obrigações de releitura, cada uma sabendo de qual objetivo veio."""
    out: list[dict[str, Any]] = []
    for item in prepared:
        objective: InvestigationObjective = item["objective"]
        claims: Mapping[str, Claim] = item["claims"]
        for obligation in reread_obligations(item["verdicts"]):
            claim = claims.get(str(obligation.get("claim_id")))
            obligation["objective_id"] = objective.objective_id
            obligation["capability_id"] = objective.capability_id
            obligation["statement"] = claim.statement if claim else ""
            out.append(obligation)
    return out


def _entity_namespace(repo: Any, entity_id: str) -> str | None:
    """Namespace da entidade, ou `None` quando ela não existe/não é legível.

    `repository.entity_exists` responde só "existe" — e existir em OUTRO
    namespace foi o que permitiu, com dois repositórios no mesmo store, um fato
    do namespace B apontar a entidade do A (achado nº5).
    """
    getter = getattr(repo, "get_entity", None)
    if getter is None:
        return None
    try:
        entity = getter(entity_id, lifecycle=None)
    except TypeError:
        try:
            entity = getter(entity_id)
        except Exception:
            return None
    except Exception:
        return None
    return getattr(entity, "namespace", None)


def _same_namespace(repo: Any, entity_id: str, namespace: str) -> bool:
    """A entidade EXISTENTE pertence a este namespace?"""
    found = _entity_namespace(repo, entity_id)
    if found is None:
        return False
    try:
        return id_mod.normalize_namespace(found) == id_mod.normalize_namespace(namespace)
    except KnowledgeError:
        return False


def _capability_subject(
    rev: Any,
    repo: Any,
    namespace: str,
    objective: InvestigationObjective,
    capability_entity_map: Mapping[str, str],
    create_missing: bool,
    outcome: ObjectiveOutcome,
    discovery: bool = False,
    support_evidence: Sequence[str] = (),
    asserted_by: str = "",
) -> str | None:
    """Entidade à qual os fatos deste objetivo se prendem.

    Só cria a Capability quando ela NÃO existe: recriar com título próprio
    geraria uma revisão de entidade a cada integração sobre uma base que
    `wk analyze` já povoou — idempotência aparente é pior que nenhuma.

    Toda entidade REUTILIZADA passa por `_same_namespace`: prender os fatos
    desta integração a uma entidade de outro namespace é exatamente o cruzamento
    que o achado nº5 descreve, e existir não é pertencer.
    """
    mapped = str(capability_entity_map.get(objective.capability_id) or "").strip()
    if mapped:
        if repo.entity_exists(mapped):
            if _same_namespace(repo, mapped, namespace):
                return mapped
            outcome.bloqueios.append(
                f"capability_entity_map aponta para entidade {mapped!r} do namespace "
                f"{_entity_namespace(repo, mapped)!r}, não de {namespace!r}: recusada "
                "(fato deste namespace não aponta entidade de outro)"
            )
        else:
            outcome.bloqueios.append(
                f"capability_entity_map aponta para entidade inexistente: {mapped!r}"
            )
    existing = bool(objective.capability_id) and repo.entity_exists(objective.capability_id)
    if existing:
        if _same_namespace(repo, objective.capability_id, namespace):
            return objective.capability_id
        outcome.bloqueios.append(
            f"capacidade {objective.capability_id!r} já existe no namespace "
            f"{_entity_namespace(repo, objective.capability_id)!r}: este objetivo não é "
            f"de {namespace!r} ou o id colide — nova identidade será derivada do namespace"
        )
    if not create_missing:
        outcome.bloqueios.append(
            f"capacidade {objective.capability_id!r} sem entidade em knowledge.db: "
            "fatos deste objetivo não foram gravados"
        )
        return None

    if discovery and not support_evidence:
        # F10/§5.4: entidade tecnica nao nasce de afirmacao sem lastro. O
        # analogo em `ingestion.correlate.CREATABLE_TYPES` e mais forte (texto
        # de documento NUNCA cria `Capability`, com ou sem citacao); aqui a
        # origem e analise de codigo, e o que autoriza a criacao e a citacao
        # RESOLVIDA no snapshot — sem nenhuma, nada e criado.
        outcome.bloqueios.append(
            f"objetivo de descoberta {objective.objective_id!r} sem nenhuma citacao de "
            "codigo resolvida: nenhuma entidade criada (afirmacao de agente sem "
            "evidencia no snapshot nao cria entidade tecnica — §5.4, F10)"
        )
        return None

    stable_key = f"cap:{objective.capability_id or objective.objective_id}"
    attributes: dict[str, Any] = {
        "objective_id": objective.objective_id,
        "origem": "descoberta" if discovery else "integracao",
    }
    if discovery:
        # `EntityDraft` nao tem campo de autoria (autoria e eixo de FATO e de
        # RELACAO). Registrar o agente aqui e o que preserva a proveniencia da
        # DESCOBERTA: a entidade existe porque um agente afirmou, e isso fica
        # legivel na propria entidade, nao so nos fatos pendurados nela.
        attributes["asserted_by"] = asserted_by or DEFAULT_ASSERTED_BY
        attributes["descoberta_por_analise_de_codigo"] = True
    write = rev.put_entity(
        EntityDraft(
            namespace=namespace,
            entity_type=EntityType.CAPABILITY,
            stable_key=stable_key,
            title=objective.name or objective.capability_id or objective.objective_id,
            # `entity_id` explícito SÓ quando ninguém mais o reivindicou: reusar
            # o id de uma entidade de outro namespace seria `IdentityConflict`
            # (e, sem a checagem, seria contaminação silenciosa).
            entity_id=(objective.capability_id or None) if not existing else None,
            attributes=attributes,
            # Descoberta NAO e confirmacao: a entidade nasce `proposed` e so
            # vira `current` por decisao humana ou por analise estrutural que a
            # reafirme. `current` aqui faria a consulta padrao trata-la como
            # conhecimento vigente do sistema (`DEFAULT_LIFECYCLE`).
            lifecycle_status=(
                LifecycleStatus.PROPOSED if discovery else LifecycleStatus.CURRENT
            ),
            evidence_refs=tuple(support_evidence),
        )
    )
    outcome.entidades.append(
        {
            "entity_id": write.target_id,
            "tipo": EntityType.CAPABILITY.value,
            "novo": write.changed,
            "lifecycle": (
                LifecycleStatus.PROPOSED.value if discovery else LifecycleStatus.CURRENT.value
            ),
            "asserted_by": attributes.get("asserted_by", ""),
        }
    )
    return write.target_id


def _rule_entity(
    rev: Any,
    namespace: str,
    objective: InvestigationObjective,
    campo: str,
    subject_name: str,
    capability_subject: str,
    evidence_ids: Sequence[str],
    verdict: Verdict,
    outcome: ObjectiveOutcome,
    statement: str = "",
    discovery: bool = False,
    asserted_by: str = "",
) -> str:
    """`BusinessRule`/`Flow` nomeada, com `implements` para a capacidade.

    Identidade determinística por `capability + assunto` (`stable_key`), então
    a mesma regra reafirmada em outra execução revisita a MESMA entidade em vez
    de criar uma homônima.

    Quando o nome (ou o enunciado) traz um id explícito do corpus — `RN-023`,
    `CAP-007` —, esse id vira ALIAS canônico (achado nº4). A `stable_key` é
    derivada (`businessrule:<capability>:rn-023`) e a ingestão resolve pela
    chave LITERAL "RN-023": sem o alias, os dois lados nunca se encontram e a
    mesma regra existe duas vezes. Com ele, `repository.put_entity` chama
    `identity.resolve_identity`, que casa pelo alias e REUSA a entidade.
    """
    entity_type = RULE_ENTITY_TYPE.get(campo, EntityType.BUSINESS_RULE)
    stable_key = f"{entity_type.value.lower()}:{objective.capability_id or objective.objective_id}:{_slug(subject_name)}"
    aliases = id_mod.explicit_id_aliases(subject_name, statement)
    attributes: dict[str, Any] = {"campo": campo, "objective_id": objective.objective_id}
    if discovery:
        attributes["origem"] = "descoberta"
        attributes["asserted_by"] = asserted_by or verdict.asserted_by or DEFAULT_ASSERTED_BY
    write = rev.put_entity(
        EntityDraft(
            namespace=namespace,
            entity_type=entity_type,
            stable_key=stable_key,
            title=subject_name,
            aliases=aliases,
            attributes=attributes,
            evidence_refs=tuple(evidence_ids),
            lifecycle_status=(
                LifecycleStatus.PROPOSED if discovery else LifecycleStatus.CURRENT
            ),
        )
    )
    outcome.entidades.append(
        {
            "entity_id": write.target_id,
            "tipo": entity_type.value,
            "novo": write.changed,
            "titulo": subject_name,
            "aliases": [a.alias for a in aliases],
            "lifecycle": (
                LifecycleStatus.PROPOSED.value if discovery else LifecycleStatus.CURRENT.value
            ),
            "asserted_by": attributes.get("asserted_by", ""),
        }
    )

    supported = verdict.epistemic is EpistemicStatus.SUPPORTED and bool(evidence_ids)
    relation = rev.put_relation(
        RelationDraft(
            namespace=namespace,
            source_entity_id=write.target_id,
            relation_type=RelationType.IMPLEMENTS,
            target_entity_id=capability_subject,
            scope=objective.objective_id,
            epistemic_status=EpistemicStatus.SUPPORTED if supported else EpistemicStatus.INFERRED,
            lifecycle_status=LifecycleStatus.CURRENT,
            asserted_by=verdict.asserted_by or DEFAULT_ASSERTED_BY,
            support_recorded_by=SUPPORT_RECORDER if supported else None,
            evidence_refs=tuple(evidence_ids) if supported else (),
        )
    )
    outcome.relacoes.append(
        {
            "relation_id": relation.target_id,
            "tipo": RelationType.IMPLEMENTS.value,
            "de": write.target_id,
            "para": capability_subject,
            "novo": relation.changed,
        }
    )
    return write.target_id


def _verifies_relation(
    rev: Any,
    namespace: str,
    objective: InvestigationObjective,
    subject_id: str,
    verdict: Verdict,
    versions: _SourceVersions,
    evidence_ids: Sequence[str],
    outcome: ObjectiveOutcome,
) -> None:
    """Arquivo de teste citado -> `Source` -> `verifies` o sujeito do claim.

    Só nasce de citação que RESOLVEU e cujo conteúdo foi classificado como
    assertion de teste: sem isso, "existe teste" viraria afirmação sem fonte.
    """
    test_locations = [
        loc for loc in verdict.locations if loc.ok and loc.content_kind is ContentKind.TEST_ASSERTION
    ]
    if not test_locations:
        return
    loc = test_locations[0]
    write = rev.put_entity(
        EntityDraft(
            namespace=namespace,
            entity_type=EntityType.SOURCE,
            stable_key=f"source:{loc.path}",
            title=loc.path.rsplit("/", 1)[-1],
            source_version_id=versions.get(loc.path),
            attributes={"path": loc.path, "papel": "teste"},
            evidence_refs=tuple(evidence_ids),
        )
    )
    supported = verdict.epistemic is EpistemicStatus.SUPPORTED and bool(evidence_ids)
    relation = rev.put_relation(
        RelationDraft(
            namespace=namespace,
            source_entity_id=write.target_id,
            relation_type=RelationType.VERIFIES,
            target_entity_id=subject_id,
            scope=objective.objective_id,
            epistemic_status=EpistemicStatus.SUPPORTED if supported else EpistemicStatus.INFERRED,
            lifecycle_status=LifecycleStatus.CURRENT,
            asserted_by=verdict.asserted_by or DEFAULT_ASSERTED_BY,
            support_recorded_by=SUPPORT_RECORDER if supported else None,
            evidence_refs=tuple(evidence_ids) if supported else (),
        )
    )
    outcome.entidades.append(
        {"entity_id": write.target_id, "tipo": EntityType.SOURCE.value, "novo": write.changed}
    )
    outcome.relacoes.append(
        {
            "relation_id": relation.target_id,
            "tipo": RelationType.VERIFIES.value,
            "de": write.target_id,
            "para": subject_id,
            "novo": relation.changed,
        }
    )


def _contract_state_dict(objective: InvestigationObjective) -> dict[str, Any]:
    """Contrato consolidado em forma serializável — só campos COM conteúdo.

    Campo vazio não viaja: o §7.2 pede o contrato acumulado *com conteúdo*, e
    mandar `{"regra": {"content": ""}}` é o mesmo "lista de IDs sem conteúdo"
    que ele proíbe, só que com mais bytes.
    """
    out: dict[str, Any] = {}
    for name in CONTRACT_FIELDS:
        campo = objective.contract.get(name)
        if campo is None:
            continue
        data = campo.to_dict()
        if not str(data.get("content") or "").strip():
            continue
        out[name] = data
    return out


def _extra_fields(chain: Sequence[AcceptedResult]) -> dict[str, Any]:
    """Chaves de topo fora de `KNOWN_OUTPUT_FIELDS`, dobradas sobre a cadeia.

    A rodada mais recente vence, pela mesma razão que ela assina a gravação:
    é a apuração corrente. Valor vai INTEIRO (nenhuma normalização), porque
    este módulo não sabe o que a chave significa — só sabe que o schema do
    coordenador a aceitou e que perdê-la aqui seria descarte silencioso.
    """
    out: dict[str, Any] = {}
    for result in chain:
        for name, value in dict(result.output or {}).items():
            if str(name) in KNOWN_OUTPUT_FIELDS:
                continue
            out[str(name)] = value
    return out


def _campos_nao_previstos(
    objective_id: str, chain: Sequence[AcceptedResult], claims: Mapping[str, Claim]
) -> list[dict[str, Any]]:
    """Um item por campo de `contract` fora do §6.3, com os claims que gerou."""
    por_campo: dict[str, dict[str, Any]] = {}
    for result in chain:
        for campo in unforeseen_contract_fields(result):
            registro = por_campo.setdefault(
                campo,
                {
                    "objective_id": objective_id,
                    "campo": campo,
                    "claims": [],
                    "task_ids": [],
                },
            )
            if result.task_id and result.task_id not in registro["task_ids"]:
                registro["task_ids"].append(result.task_id)
    prefixos = {campo: f"{objective_id}:{campo}:" for campo in por_campo}
    for claim_id in sorted(claims):
        for campo, prefixo in prefixos.items():
            if claim_id.startswith(prefixo):
                por_campo[campo]["claims"].append(claim_id)
    return [por_campo[c] for c in sorted(por_campo)]


def _open_needs_dicts(objective: InvestigationObjective) -> list[dict[str, Any]]:
    """Obrigações de leitura ainda abertas, com `target` resolvível.

    Sem `target` a obrigação não é despachável (`plan_continuations` a recusa),
    então mandá-la aqui só inflaria o relatório sem criar trabalho possível.
    """
    out: list[dict[str, Any]] = []
    for need in objective.open_needs():
        data = need.to_dict() if hasattr(need, "to_dict") else dict(need)
        if not str(data.get("target") or "").strip():
            continue
        out.append(data)
    return out


def _write_objective(
    *,
    rev: Any,
    repo: Any,
    namespace: str,
    versions: _SourceVersions,
    result: AcceptedResult,
    objective: InvestigationObjective,
    claims: Mapping[str, Claim],
    verdicts: Sequence[Verdict],
    inconsistencies: Sequence[Inconsistency],
    capability_entity_map: Mapping[str, str],
    create_missing_capability: bool,
    escopos: Mapping[str, str] | None = None,
    leituras: Sequence[Mapping[str, Any]] = (),
    chain: Sequence[AcceptedResult] = (),
    rejeicoes: Sequence[Mapping[str, Any]] = (),
) -> ObjectiveOutcome:
    """Grava os fatos de UM objetivo e recalcula seu estado (§6.6)."""
    state = objective.evaluate()
    discovery = _is_discovery(objective)
    outcome = ObjectiveOutcome(
        objective_id=objective.objective_id,
        capability_id=objective.capability_id,
        subject_id=None,
        state=state.value,
        kind=_objective_kind(objective),
        unmet=objective.unmet_obligations(),
        lacunas=_lacunas_of(objective, result.output, verdicts, claims),
        inconsistencias=[i.as_dict() for i in inconsistencies],
        leituras_satisfeitas=[dict(l) for l in leituras],
        task_id=result.task_id,
        execution_id=result.execution_id,
        integration_key=result.integration_key,
        contract_state=_contract_state_dict(objective),
        reading_needs=_open_needs_dicts(objective),
        chain_task_ids=[r.task_id for r in chain] or [result.task_id],
        extra=_extra_fields(list(chain) or [result]),
        campos_nao_previstos=_campos_nao_previstos(
            objective.objective_id, list(chain) or [result], claims
        ),
        resultados_rejeitados=[dict(r) for r in rejeicoes],
    )

    # PORTAO DE EVIDENCIA — roda ANTES de qualquer escrita, por dois motivos
    # que não são de estilo:
    #
    # 1. a entidade de DESCOBERTA só pode nascer se sobrar alguma citação de
    #    código resolvida, e isso só se sabe depois de examinar TODOS os claims;
    # 2. a recusa de um claim precisa ser registrada mesmo quando a entidade
    #    não chega a ser criada — do contrário "nada foi criado" apareceria sem
    #    dizer QUAIS citações caíram e por quê, que é o silêncio que o §6.6
    #    proíbe.
    aceitos: list[tuple[Claim, Verdict, _EvidenceGate]] = []
    for verdict in verdicts:
        claim = claims.get(verdict.claim_id)
        if claim is None:
            continue
        gate = _code_evidence(claim, verdict, versions)
        if gate.resolvida:
            outcome.claims_com_evidencia_resolvida += 1
        elif not gate.declarou:
            outcome.claims_sem_evidencia += 1
        recusa = _gate_rejection(claim, gate, discovery)
        if recusa is not None:
            outcome.rejeitados.append(recusa)
            outcome.bloqueios.append(f"{claim.claim_id}: {recusa['motivo']}")
            continue
        aceitos.append((claim, verdict, gate))

    outcome.evidence_refs = _accepted_evidence_refs(aceitos)

    # Entidade de DESCOBERTA so nasce com evidencia de codigo RESOLVIDA. A
    # regra F10/§5.4 de `ingestion.correlate.CREATABLE_TYPES` diz que
    # `Capability` nao e criavel a partir do TEXTO de um documento; aqui a
    # origem e outra — analise do proprio codigo, com citacao que resolve no
    # snapshot corrente. E a citacao resolvida (nao a origem declarada) que
    # separa os dois casos.
    support_evidence: list[str] = (
        _support_evidence_ids(rev, namespace, versions, aceitos) if discovery else []
    )

    capability_subject = _capability_subject(
        rev,
        repo,
        namespace,
        objective,
        capability_entity_map,
        create_missing_capability,
        outcome,
        discovery=discovery,
        support_evidence=support_evidence,
        asserted_by=result.asserted_by or DEFAULT_ASSERTED_BY,
    )
    outcome.subject_id = capability_subject
    if capability_subject is None:
        return outcome

    for claim, verdict, _gate in aceitos:
        _write_claim(
            rev=rev,
            namespace=namespace,
            versions=versions,
            result=result,
            objective=objective,
            claim=claim,
            verdict=verdict,
            capability_subject=capability_subject,
            outcome=outcome,
            escopo_sustentado=(escopos or {}).get(claim.claim_id, ""),
            discovery=discovery,
        )
    return outcome


def _accepted_evidence_refs(
    aceitos: Sequence[tuple[Claim, Verdict, "_EvidenceGate"]]
) -> list[dict[str, Any]]:
    """Citações aceitas, deduplicadas por `(path, faixa)` e em ordem estável.

    Ordem estável (primeira aparição) porque este dicionário viaja para
    `runtime.state`, onde vira chave de "evidência nova": reordenar a lista
    não pode mudar o que conta como progresso.
    """
    out: list[dict[str, Any]] = []
    vistas: set[tuple[str, int, int]] = set()
    for _claim, _verdict, gate in aceitos:
        for loc in gate.locations:
            chave = (loc.path, int(loc.start_line), int(loc.end_line))
            if chave in vistas:
                continue
            vistas.add(chave)
            out.append(
                {
                    "path": loc.path,
                    "line_start": int(loc.start_line),
                    "line_end": int(loc.end_line),
                    "content_kind": loc.content_kind.value,
                }
            )
    return out


def _gate_rejection(
    claim: Claim, gate: _EvidenceGate, discovery: bool
) -> dict[str, Any] | None:
    """Recusa TIPADA do claim, ou `None` quando ele pode ser gravado.

    Três recusas distintas, para que o operador saiba qual regra caiu sem ler
    prosa (os códigos são estáveis e vão em `rejeitados[*]["tipo"]`):

    * `evidencia_fora_do_snapshot` — citação para arquivo que não está neste
      snapshot é prova de OUTRO escopo (o repositório vizinho no mesmo store,
      ou uma árvore anterior); gravar apontaria conhecimento deste namespace
      para evidência que ele não pode reabrir;
    * `evidencia_nao_resolvida` — o caminho existe, a FAIXA não resolve (linha
      além do fim, conteúdo mudou sob a citação). Aceitar em silêncio seria
      gravar uma referência que nunca mais volta a ser verificável;
    * `evidencia_nao_codigo` — documento (.md/.rst/...) ou faixa que, removido
      o comentário/docstring, não tem código. Comentário afirma a intenção do
      autor; só o código afirma o comportamento do sistema (§5.4, F10).

    Claim SEM nenhuma citação: em objetivo de capacidade continua virando fato
    `unresolved` (invariante 4 — ausência de prova é lacuna DECLARADA, não
    descarte); em objetivo de DESCOBERTA é recusado, porque ali a afirmação
    nasce da leitura livre do modelo e sem citação nada a distingue de opinião.
    """
    campo = claim.scope.rsplit(":", 1)[-1] if ":" in claim.scope else ""
    if gate.resolvida:
        return None
    if gate.declarou:
        problemas = list(gate.problemas)
        return {
            "claim_id": claim.claim_id,
            "campo": campo,
            "tipo": str(problemas[0]["tipo"]) if problemas else REJECT_EVIDENCE_UNRESOLVED,
            "tipos": sorted({str(pr["tipo"]) for pr in problemas}),
            "evidencias": problemas,
            "motivo": "; ".join(str(pr["motivo"]) for pr in problemas)[:400],
        }
    if discovery:
        return {
            "claim_id": claim.claim_id,
            "campo": campo,
            "tipo": REJECT_EVIDENCE_UNRESOLVED,
            "tipos": [REJECT_EVIDENCE_UNRESOLVED],
            "evidencias": [],
            "motivo": (
                "objetivo de descoberta: afirmacao sem nenhuma citacao de codigo — "
                "leitura livre do modelo sem path/linhas nao entra no corpus"
            ),
        }
    return None


def _support_evidence_ids(
    rev: Any,
    namespace: str,
    versions: "_SourceVersions",
    aceitos: Sequence[tuple[Claim, Verdict, "_EvidenceGate"]],
) -> list[str]:
    """Ids de evidencia de CODIGO deste objetivo, em ordem estavel.

    Sao as mesmas linhas que `_record_evidence` gravaria por claim (mesmo
    `evidence_id` derivado do localizador, logo `add_evidence` e idempotente):
    aqui elas so sao antecipadas porque a ENTIDADE de descoberta precisa
    nascer ja apontando a sustentacao — criar primeiro e prender evidencia
    depois deixaria uma entidade sem lastro visivel na mesma revisao.
    """
    ids: list[str] = []
    for claim, _verdict, gate in aceitos:
        if not gate.resolvida:
            continue
        for loc in gate.locations:
            svid = versions.get(loc.path)
            evidence = _evidence_for_location(namespace, svid, loc, claim.symbol)
            if evidence is None:
                continue
            eid = rev.add_evidence(evidence)
            if eid not in ids:
                ids.append(eid)
    return ids


def _record_evidence(
    rev: Any, namespace: str, versions: _SourceVersions, verdict: Verdict, symbol: str | None
) -> tuple[list[str], str | None]:
    """Localizações do veredito -> linhas de `evidence`, com a versão da fonte.

    Só localização RESOLVIDA vira evidência; citação que não resolve some daqui
    e reaparece como lacuna no relatório — o oposto de gravar uma referência
    que não volta a ser verificável.
    """
    evidence_ids: list[str] = []
    source_version_id: str | None = None
    for loc in verdict.locations:
        svid = versions.get(loc.path)
        evidence = _evidence_for_location(namespace, svid, loc, symbol)
        if evidence is None:
            continue
        evidence_ids.append(rev.add_evidence(evidence))
        source_version_id = source_version_id or svid
    return evidence_ids, source_version_id


def _write_claim(
    *,
    rev: Any,
    namespace: str,
    versions: _SourceVersions,
    result: AcceptedResult,
    objective: InvestigationObjective,
    claim: Claim,
    verdict: Verdict,
    capability_subject: str,
    outcome: ObjectiveOutcome,
    escopo_sustentado: str = "",
    discovery: bool = False,
) -> None:
    """Um claim ACEITO pelo portão de evidência -> um fato, com o `epistemic`
    do VEREDITO.

    O portão (`_gate_rejection`, chamado por `_write_objective`) já recusou o
    que não podia ser gravado: aqui não há mais decisão sobre citação.
    """
    campo = claim.scope.rsplit(":", 1)[-1] if ":" in claim.scope else ""

    evidence_ids, source_version_id = _record_evidence(
        rev, namespace, versions, verdict, claim.symbol
    )

    subject_id = capability_subject
    subject_name = _subject_name_of(claim, result.output, campo)
    entity_id: str | None = None
    # Regra/fluxo DESCOBERTO so vira entidade com evidencia de codigo
    # resolvida (mesma porta da capacidade acima): sem lastro, a afirmacao
    # continua sendo um fato preso a capacidade, nunca uma entidade nova.
    if subject_name and campo in RULE_ENTITY_TYPE and not (discovery and not evidence_ids):
        entity_id = _rule_entity(
            rev, namespace, objective, campo, subject_name, capability_subject,
            evidence_ids, verdict, outcome, claim.statement,
            discovery=discovery,
            asserted_by=result.asserted_by or DEFAULT_ASSERTED_BY,
        )
        subject_id = entity_id

    # Veredito `supported` cuja citação não virou linha de `evidence` (arquivo
    # fora do snapshot, localizador irrecuperável) é rebaixado AQUI:
    # `repository._check_support` recusaria depois, e recusar tarde derrubaria
    # a revisão inteira em vez de registrar o que se sabe.
    #
    # O `value` gravado é a frase INTEIRA (é ela que a consulta e a publicação
    # mostram), e por isso o `epistemic` já chega aqui julgado pelo componente
    # mais fraco: `_consequence_guard` rebaixou/contradisse antes. Gravar a
    # frase toda com o `epistemic` da condição isolada é o achado nº1.
    supported = verdict.epistemic is EpistemicStatus.SUPPORTED and bool(evidence_ids)
    epistemic = (
        verdict.epistemic
        if supported or verdict.epistemic is not EpistemicStatus.SUPPORTED
        else EpistemicStatus.INFERRED
    )
    nature = _nature_for(claim, verdict, supported)
    predicate = f"{PREDICATE_PREFIX}.{campo}" if campo else PREDICATE_PREFIX

    try:
        write = rev.put_fact(
            FactDraft(
                namespace=namespace,
                subject_id=subject_id,
                predicate=predicate,
                value=claim.statement or claim.claim_id,
                scope=claim.claim_id,
                nature=nature,
                epistemic_status=epistemic,
                lifecycle_status=LifecycleStatus.CURRENT,
                asserted_by=claim.asserted_by or result.asserted_by,
                evidence_refs=tuple(evidence_ids),
                source_version_id=source_version_id,
                support_recorded_by=(
                    (verdict.support_recorded_by or SUPPORT_RECORDER) if supported else None
                ),
                approval_state=ApprovalState.NONE,
                nature_change_recorded_by=INTEGRATOR,
            )
        )
    except KnowledgeError as exc:
        # Invariante do repositório recusou ESTE fato: registra a recusa e
        # segue. Deixar a exceção subir abortaria a revisão inteira e perderia
        # os fatos legítimos do mesmo objetivo.
        outcome.bloqueios.append(f"{claim.claim_id}: {type(exc).__name__}: {exc}")
        return

    outcome.fatos.append(
        FactWrite(
            claim_id=claim.claim_id,
            fact_id=write.target_id,
            subject_id=subject_id,
            campo=campo,
            predicate=predicate,
            epistemic=epistemic.value,
            nature=nature.value,
            changed=write.changed,
            evidence_refs=tuple(evidence_ids),
            entity_id=entity_id,
            escopo_sustentado=escopo_sustentado,
        )
    )

    if claim.predicate_kind == "test":
        _verifies_relation(
            rev, namespace, objective, subject_id, verdict, versions, evidence_ids, outcome
        )


def _subject_name_of(claim: Claim, output: Mapping[str, Any], campo: str) -> str:
    """Assunto nomeado da afirmação, recuperado do enunciado.

    Reconstruído (e não guardado do parsing) porque `Claim` é o contrato de
    `analysis.verification` e não carrega vocabulário de entidade — colocar o
    nome lá acoplaria os dois módulos por um campo que a verificação ignora.
    """
    raw = _field_payload(output, campo)
    if isinstance(raw, Mapping):
        for key in _ASSERTION_KEYS:
            items = raw.get(key)
            if isinstance(items, Sequence) and not isinstance(items, (str, bytes)):
                for item in items:
                    if not isinstance(item, Mapping):
                        continue
                    for skey in _STATEMENT_KEYS:
                        if _norm_ws(item.get(skey)) == claim.statement:
                            return _named_subject(item, claim.statement)
    return _named_subject({}, claim.statement)
