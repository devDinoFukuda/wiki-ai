from __future__ import annotations

import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

from tests.benchmark.ground_truth.truth import (
    Anchor,
    ContradictionTruth,
    EdgeCaseTruth,
    EntryPointTruth,
    FailureModeTruth,
    IntegrationTruth,
    InvariantTruth,
    RepositoryTruth,
    RuleTruth,
    StateTruth,
    TransitionTruth,
)

__all__ = [
    "CorpusUnknown",
    "GeneratedCorpus",
    "CORPUS_NAMES",
    "materialize",
    "build_java",
    "build_python",
    "build_cobol",
]

_MARK = "@@"


class CorpusUnknown(KeyError):
    def __init__(self, name: str) -> None:
        super().__init__(f"corpus desconhecido: {name}; disponiveis: {list(CORPUS_NAMES)}")
        self.name = name


@dataclass(frozen=True)
class GeneratedCorpus:
    root: Path
    truth: RepositoryTruth
    documents: tuple[Path, ...] = ()


def _bare(line: str) -> str:
    if not line.startswith(_MARK):
        return line
    return line.split(_MARK, 2)[2]


def _strip(lines: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(_bare(line) for line in lines)


class _Sheet:
    def __init__(self, path: str, lines: tuple[str, ...]) -> None:
        self.path = path
        self.lines = lines
        self._marks: dict[str, int] = {}
        for number, line in enumerate(lines, start=1):
            if _MARK in line:
                token = line.split(_MARK, 1)[1].split(_MARK, 1)[0]
                self._marks[token] = number

    def text(self) -> str:
        return "\n".join(_strip(self.lines)) + "\n"

    def at(self, token: str) -> int:
        if token not in self._marks:
            raise CorpusUnknown(f"{self.path}#{token}")
        return self._marks[token]

    def span(self, first: str, last: str) -> Anchor:
        return Anchor(self.path, self.at(first), self.at(last))

    def line(self, token: str) -> Anchor:
        number = self.at(token)
        return Anchor(self.path, number, number)


def _mark(token: str, text: str) -> str:
    return f"{_MARK}{token}{_MARK}{text}"


JAVA_CONTROLLER_PATH = "src/main/java/com/acme/renewal/RenewalController.java"
JAVA_SERVICE_PATH = "src/main/java/com/acme/renewal/RenewalService.java"
JAVA_ELIGIBILITY_PATH = "src/main/java/com/acme/renewal/EligibilityPolicy.java"
JAVA_REPOSITORY_PATH = "src/main/java/com/acme/renewal/ContractRepository.java"
JAVA_PUBLISHER_PATH = "src/main/java/com/acme/renewal/RenewalEventPublisher.java"
JAVA_TEST_PATH = "src/test/java/com/acme/renewal/EligibilityPolicyTest.java"
JAVA_CONFIG_PATH = "src/main/resources/application.yml"

_JAVA_CONTROLLER = (
    "package com.acme.renewal;",
    "",
    "import org.springframework.http.ResponseEntity;",
    "import org.springframework.web.bind.annotation.PathVariable;",
    "import org.springframework.web.bind.annotation.PostMapping;",
    "import org.springframework.web.bind.annotation.RequestMapping;",
    "import org.springframework.web.bind.annotation.RestController;",
    "",
    "@RestController",
    _mark("mapping", '@RequestMapping("/contracts")'),
    "public class RenewalController {",
    "",
    "    private final RenewalService service;",
    "",
    "    public RenewalController(RenewalService service) {",
    "        this.service = service;",
    "    }",
    "",
    _mark("post", '    @PostMapping("/{contractId}/renewals")'),
    _mark("handler", "    public ResponseEntity<RenewalResult> renew(@PathVariable String contractId) {"),
    "        RenewalResult result = service.renew(contractId);",
    "        if (!result.accepted()) {",
    "            return ResponseEntity.unprocessableEntity().body(result);",
    "        }",
    "        return ResponseEntity.accepted().body(result);",
    "    }",
    "}",
)

_JAVA_ELIGIBILITY = (
    "package com.acme.renewal;",
    "",
    "import java.time.Duration;",
    "import java.time.Instant;",
    "import java.math.BigDecimal;",
    "",
    "public final class EligibilityPolicy {",
    "",
    _mark("window", "    public static final Duration RENEWAL_WINDOW = Duration.ofDays(30);"),
    "",
    _mark("evaluate", "    public EligibilityDecision evaluate(Contract contract, Instant now) {"),
    _mark("debt", "        if (contract.outstandingDebt().compareTo(BigDecimal.ZERO) > 0) {"),
    _mark("debt_effect", '            return EligibilityDecision.blocked("outstanding_debt");'),
    "        }",
    _mark("premium", '        if (contract.plan().equals("premium")) {',),
    _mark("premium_effect", "            return EligibilityDecision.allowed();"),
    "        }",
    _mark("expiry", "        Duration remaining = Duration.between(now, contract.expiresAt());"),
    _mark("window_check", "        if (remaining.compareTo(RENEWAL_WINDOW) > 0) {"),
    _mark("window_effect", '            return EligibilityDecision.blocked("outside_renewal_window");'),
    "        }",
    _mark("expired", "        if (remaining.isNegative()) {"),
    _mark("expired_effect", '            return EligibilityDecision.blocked("contract_expired");'),
    "        }",
    "        return EligibilityDecision.allowed();",
    "    }",
    "}",
)

_JAVA_SERVICE = (
    "package com.acme.renewal;",
    "",
    "import java.time.Duration;",
    "import java.time.Instant;",
    "import org.springframework.stereotype.Service;",
    "import org.springframework.transaction.annotation.Transactional;",
    "",
    "@Service",
    "public class RenewalService {",
    "",
    "    private final ContractRepository contracts;",
    "    private final EligibilityPolicy policy;",
    "    private final RenewalEventPublisher publisher;",
    _mark("timeout", "    private static final Duration PUBLISH_TIMEOUT = Duration.ofSeconds(5);"),
    _mark("attempts", "    private static final int MAX_ATTEMPTS = 3;"),
    _mark("backoff", "    private static final long BACKOFF_MILLIS = 250L;"),
    "",
    "    public RenewalService(",
    "            ContractRepository contracts,",
    "            EligibilityPolicy policy,",
    "            RenewalEventPublisher publisher) {",
    "        this.contracts = contracts;",
    "        this.policy = policy;",
    "        this.publisher = publisher;",
    "    }",
    "",
    "    @Transactional",
    _mark("renew", "    public RenewalResult renew(String contractId) {"),
    _mark("load", "        Contract contract = contracts.findByIdForUpdate(contractId)"),
    _mark("missing", "                .orElseThrow(() -> new ContractNotFoundException(contractId));"),
    _mark("decide", "        EligibilityDecision decision = policy.evaluate(contract, Instant.now());"),
    "        if (!decision.allowed()) {",
    _mark("reject", "            return RenewalResult.rejected(decision.reason());"),
    "        }",
    _mark("persist", "        Contract renewed = contracts.save(contract.extendBy(EligibilityPolicy.RENEWAL_WINDOW));"),
    _mark("publish_call", "        publishWithRetry(new RenewalPerformed(renewed.id(), renewed.expiresAt()));"),
    "        return RenewalResult.accepted(renewed.expiresAt());",
    "    }",
    "",
    _mark("retry", "    private void publishWithRetry(RenewalPerformed event) {"),
    _mark("loop", "        for (int attempt = 1; attempt <= MAX_ATTEMPTS; attempt++) {"),
    "            try {",
    _mark("publish", "                publisher.publish(event, PUBLISH_TIMEOUT);"),
    "                return;",
    _mark("catch", "            } catch (PublishFailedException failure) {"),
    _mark("exhausted", "                if (attempt == MAX_ATTEMPTS) {"),
    _mark("rethrow", "                    throw new RenewalNotNotifiedException(event.contractId(), failure);"),
    "                }",
    _mark("sleep", "                sleepQuietly(BACKOFF_MILLIS * (1L << (attempt - 1)));"),
    "            }",
    "        }",
    "    }",
    "",
    "    private void sleepQuietly(long millis) {",
    "        try {",
    "            Thread.sleep(millis);",
    "        } catch (InterruptedException interrupted) {",
    "            Thread.currentThread().interrupt();",
    "        }",
    "    }",
    "}",
)

_JAVA_REPOSITORY = (
    "package com.acme.renewal;",
    "",
    "import java.util.Optional;",
    "import jakarta.persistence.LockModeType;",
    "import org.springframework.data.jpa.repository.JpaRepository;",
    "import org.springframework.data.jpa.repository.Lock;",
    "import org.springframework.data.jpa.repository.Query;",
    "",
    _mark("iface", "public interface ContractRepository extends JpaRepository<Contract, String> {"),
    "",
    _mark("lock", "    @Lock(LockModeType.PESSIMISTIC_WRITE)"),
    _mark("query", '    @Query("select c from Contract c where c.id = :id")'),
    _mark("find", "    Optional<Contract> findByIdForUpdate(String id);"),
    "}",
)

_JAVA_PUBLISHER = (
    "package com.acme.renewal;",
    "",
    "import java.time.Duration;",
    "import java.util.concurrent.TimeUnit;",
    "import java.util.concurrent.TimeoutException;",
    "import org.apache.kafka.clients.producer.ProducerRecord;",
    "import org.springframework.kafka.core.KafkaTemplate;",
    "import org.springframework.stereotype.Component;",
    "",
    "@Component",
    "public class RenewalEventPublisher {",
    "",
    _mark("topic", '    public static final String TOPIC = "contract.renewal.performed";'),
    "",
    "    private final KafkaTemplate<String, RenewalPerformed> template;",
    "",
    "    public RenewalEventPublisher(KafkaTemplate<String, RenewalPerformed> template) {",
    "        this.template = template;",
    "    }",
    "",
    _mark("publish", "    public void publish(RenewalPerformed event, Duration timeout) {"),
    _mark("record", "        ProducerRecord<String, RenewalPerformed> record ="),
    "                new ProducerRecord<>(TOPIC, event.contractId(), event);",
    "        try {",
    _mark("send", "            template.send(record).get(timeout.toMillis(), TimeUnit.MILLISECONDS);"),
    _mark("timeout_catch", "        } catch (TimeoutException expired) {"),
    _mark("timeout_effect", '            throw new PublishFailedException("publish_timeout", expired);'),
    "        } catch (Exception broken) {",
    _mark("failure_effect", '            throw new PublishFailedException("publish_failed", broken);'),
    "        }",
    "    }",
    "}",
)

_JAVA_TEST = (
    "package com.acme.renewal;",
    "",
    "import static org.junit.jupiter.api.Assertions.assertEquals;",
    "import static org.junit.jupiter.api.Assertions.assertTrue;",
    "",
    "import java.math.BigDecimal;",
    "import java.time.Instant;",
    "import org.junit.jupiter.api.Test;",
    "",
    "class EligibilityPolicyTest {",
    "",
    "    @Test",
    _mark("debt_test", "    void blocksWhenOutstandingDebtExists() {"),
    "        Contract contract = Contract.of(\"c-1\", \"basic\", new BigDecimal(\"12.50\"),",
    "                Instant.parse(\"2026-01-10T00:00:00Z\"));",
    "        EligibilityDecision decision =",
    "                new EligibilityPolicy().evaluate(contract, Instant.parse(\"2026-01-01T00:00:00Z\"));",
    "        assertEquals(\"outstanding_debt\", decision.reason());",
    "    }",
    "",
    "    @Test",
    _mark("premium_test", "    void premiumPlanSkipsWindowCheck() {"),
    "        Contract contract = Contract.of(\"c-2\", \"premium\", BigDecimal.ZERO,",
    "                Instant.parse(\"2027-01-10T00:00:00Z\"));",
    "        EligibilityDecision decision =",
    "                new EligibilityPolicy().evaluate(contract, Instant.parse(\"2026-01-01T00:00:00Z\"));",
    "        assertTrue(decision.allowed());",
    "    }",
    "}",
)

_JAVA_CONFIG = (
    "spring:",
    "  kafka:",
    "    bootstrap-servers: broker-1.acme.internal:9092",
    "renewal:",
    _mark("window_config", "  window-days: 30"),
    _mark("attempts_config", "  max-attempts: 3"),
    _mark("timeout_config", "  publish-timeout-seconds: 5"),
)

_JAVA_SUPPORT: Mapping[str, tuple[str, ...]] = {
    "src/main/java/com/acme/renewal/Contract.java": (
        "package com.acme.renewal;",
        "",
        "import java.math.BigDecimal;",
        "import java.time.Duration;",
        "import java.time.Instant;",
        "",
        "public record Contract(String id, String plan, BigDecimal outstandingDebt, Instant expiresAt) {",
        "    public static Contract of(String id, String plan, BigDecimal debt, Instant expiresAt) {",
        "        return new Contract(id, plan, debt, expiresAt);",
        "    }",
        "",
        "    public Contract extendBy(Duration window) {",
        "        return new Contract(id, plan, outstandingDebt, expiresAt.plus(window));",
        "    }",
        "}",
    ),
    "src/main/java/com/acme/renewal/EligibilityDecision.java": (
        "package com.acme.renewal;",
        "",
        "public record EligibilityDecision(boolean allowed, String reason) {",
        "    public static EligibilityDecision allowed() {",
        '        return new EligibilityDecision(true, "");',
        "    }",
        "",
        "    public static EligibilityDecision blocked(String reason) {",
        "        return new EligibilityDecision(false, reason);",
        "    }",
        "}",
    ),
    "src/main/java/com/acme/renewal/RenewalResult.java": (
        "package com.acme.renewal;",
        "",
        "import java.time.Instant;",
        "",
        "public record RenewalResult(boolean accepted, String reason, Instant expiresAt) {",
        "    public static RenewalResult accepted(Instant expiresAt) {",
        '        return new RenewalResult(true, "", expiresAt);',
        "    }",
        "",
        "    public static RenewalResult rejected(String reason) {",
        "        return new RenewalResult(false, reason, null);",
        "    }",
        "}",
    ),
    "src/main/java/com/acme/renewal/RenewalPerformed.java": (
        "package com.acme.renewal;",
        "",
        "import java.time.Instant;",
        "",
        "public record RenewalPerformed(String contractId, Instant expiresAt) {",
        "}",
    ),
    "src/main/java/com/acme/renewal/PublishFailedException.java": (
        "package com.acme.renewal;",
        "",
        "public class PublishFailedException extends RuntimeException {",
        "    public PublishFailedException(String reason, Throwable cause) {",
        "        super(reason, cause);",
        "    }",
        "}",
    ),
    "src/main/java/com/acme/renewal/RenewalNotNotifiedException.java": (
        "package com.acme.renewal;",
        "",
        "public class RenewalNotNotifiedException extends RuntimeException {",
        "    public RenewalNotNotifiedException(String contractId, Throwable cause) {",
        "        super(contractId, cause);",
        "    }",
        "}",
    ),
    "src/main/java/com/acme/renewal/ContractNotFoundException.java": (
        "package com.acme.renewal;",
        "",
        "public class ContractNotFoundException extends RuntimeException {",
        "    public ContractNotFoundException(String contractId) {",
        "        super(contractId);",
        "    }",
        "}",
    ),
    "pom.xml": (
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<project xmlns="http://maven.apache.org/POM/4.0.0">',
        "  <modelVersion>4.0.0</modelVersion>",
        "  <groupId>com.acme</groupId>",
        "  <artifactId>renewal-service</artifactId>",
        "  <version>1.0.0</version>",
        "</project>",
    ),
}

JAVA_CONTRADICTION_DOCUMENT = "renewal-policy.docx"


def build_java(root: Path) -> GeneratedCorpus:
    controller = _Sheet(JAVA_CONTROLLER_PATH, _JAVA_CONTROLLER)
    eligibility = _Sheet(JAVA_ELIGIBILITY_PATH, _JAVA_ELIGIBILITY)
    service = _Sheet(JAVA_SERVICE_PATH, _JAVA_SERVICE)
    repository = _Sheet(JAVA_REPOSITORY_PATH, _JAVA_REPOSITORY)
    publisher = _Sheet(JAVA_PUBLISHER_PATH, _JAVA_PUBLISHER)
    suite = _Sheet(JAVA_TEST_PATH, _JAVA_TEST)
    config = _Sheet(JAVA_CONFIG_PATH, _JAVA_CONFIG)
    sheets = (controller, eligibility, service, repository, publisher, suite, config)
    for sheet in sheets:
        _write(root / sheet.path, sheet.text())
    for path, lines in _JAVA_SUPPORT.items():
        _write(root / path, "\n".join(lines) + "\n")
    document = root / JAVA_CONTRADICTION_DOCUMENT
    _write_docx(document, _JAVA_DOCUMENT_PARAGRAPHS)
    truth = RepositoryTruth(
        name="java",
        language="java",
        business_rules=(
            RuleTruth(
                key="renewal_blocked_by_outstanding_debt",
                statement="renewal is blocked when the contract has outstanding debt",
                key_terms=("outstandingDebt", "outstanding_debt", "blocked"),
                conditions=("outstandingDebt greater than zero",),
                effects=("blocked with reason outstanding_debt",),
                anchors=(eligibility.span("debt", "debt_effect"),),
            ),
            RuleTruth(
                key="renewal_window_of_thirty_days",
                statement="renewal is only allowed inside a window of 30 days before expiry",
                key_terms=("RENEWAL_WINDOW", "ofDays", "30", "outside_renewal_window"),
                conditions=("remaining time greater than RENEWAL_WINDOW of 30 days",),
                effects=("blocked with reason outside_renewal_window",),
                anchors=(
                    eligibility.line("window"),
                    eligibility.span("window_check", "window_effect"),
                ),
            ),
            RuleTruth(
                key="premium_plan_exempt_from_window",
                statement="premium plan contracts are exempt from the renewal window check",
                key_terms=("premium", "plan", "allowed"),
                conditions=("contract plan equals premium",),
                effects=("allowed without evaluating the renewal window",),
                anchors=(eligibility.span("premium", "premium_effect"),),
            ),
        ),
        edge_cases=(
            EdgeCaseTruth(
                key="already_expired_contract",
                statement="an already expired contract is blocked instead of renewed",
                key_terms=("isNegative", "contract_expired", "remaining"),
                condition="remaining duration is negative",
                expected="blocked with reason contract_expired",
                anchors=(eligibility.span("expired", "expired_effect"),),
            ),
            EdgeCaseTruth(
                key="contract_not_found",
                statement="a renewal for an unknown contract raises ContractNotFoundException",
                key_terms=("ContractNotFoundException", "orElseThrow", "findByIdForUpdate"),
                condition="findByIdForUpdate returns empty",
                expected="ContractNotFoundException is thrown",
                anchors=(service.span("load", "missing"),),
            ),
            EdgeCaseTruth(
                key="publish_exhausts_attempts",
                statement="when every publish attempt fails the renewal is reported as not notified",
                key_terms=("MAX_ATTEMPTS", "RenewalNotNotifiedException", "attempt"),
                condition="attempt equals MAX_ATTEMPTS and publish still fails",
                expected="RenewalNotNotifiedException is thrown",
                anchors=(service.span("exhausted", "rethrow"),),
            ),
        ),
        invariants=(
            InvariantTruth(
                key="renewal_row_locked_for_update",
                statement="the contract row is locked for update while the renewal runs",
                key_terms=("PESSIMISTIC_WRITE", "Lock", "findByIdForUpdate"),
                anchors=(repository.span("lock", "find"),),
            ),
            InvariantTruth(
                key="renewal_is_transactional",
                statement="the renewal executes inside a single transaction",
                key_terms=("Transactional", "renew", "save"),
                anchors=(service.span("renew", "persist"),),
            ),
        ),
        integrations=(
            IntegrationTruth(
                key="kafka_renewal_performed_topic",
                statement="the service publishes RenewalPerformed to the kafka topic contract.renewal.performed",
                key_terms=("kafka", "contract.renewal.performed", "KafkaTemplate", "send"),
                direction="outbound",
                protocol="kafka",
                anchors=(
                    publisher.line("topic"),
                    publisher.span("publish", "send"),
                ),
            ),
            IntegrationTruth(
                key="jpa_contract_repository",
                statement="contracts are persisted through a JPA repository",
                key_terms=("JpaRepository", "ContractRepository", "Query"),
                direction="outbound",
                protocol="jpa",
                anchors=(repository.span("iface", "find"),),
            ),
        ),
        entrypoints=(
            EntryPointTruth(
                key="post_contract_renewal",
                statement="POST /contracts/{contractId}/renewals starts a renewal",
                key_terms=("PostMapping", "renewals", "RestController", "renew"),
                mechanism="http",
                anchors=(controller.span("mapping", "handler"),),
            ),
        ),
        failure_modes=(
            FailureModeTruth(
                key="publish_timeout",
                statement="a publish that exceeds the timeout raises PublishFailedException publish_timeout",
                key_terms=("TimeoutException", "publish_timeout", "PublishFailedException"),
                trigger="kafka send exceeds the configured timeout",
                effect="PublishFailedException with reason publish_timeout",
                anchors=(publisher.span("timeout_catch", "timeout_effect"),),
            ),
            FailureModeTruth(
                key="publish_retry_with_backoff",
                statement="publish failures are retried up to three times with exponential backoff",
                key_terms=("MAX_ATTEMPTS", "BACKOFF_MILLIS", "attempt", "sleepQuietly"),
                trigger="PublishFailedException raised by the publisher",
                effect="retry with doubled backoff until MAX_ATTEMPTS",
                anchors=(
                    service.span("attempts", "backoff"),
                    service.span("loop", "sleep"),
                ),
            ),
        ),
        contradictions=(
            ContradictionTruth(
                key="window_days_contradiction",
                document_claim="the renewal window is 45 days",
                code_reality="the renewal window is 30 days",
                key_terms=("45", "30", "window"),
                code_anchors=(eligibility.line("window"), config.line("window_config")),
                document_name=JAVA_CONTRADICTION_DOCUMENT,
            ),
        ),
        symbols_by_path={
            JAVA_CONTROLLER_PATH: ("RenewalController", "renew", "PostMapping"),
            JAVA_ELIGIBILITY_PATH: ("EligibilityPolicy", "RENEWAL_WINDOW", "evaluate"),
            JAVA_SERVICE_PATH: ("RenewalService", "publishWithRetry", "MAX_ATTEMPTS"),
            JAVA_REPOSITORY_PATH: ("ContractRepository", "findByIdForUpdate"),
            JAVA_PUBLISHER_PATH: ("RenewalEventPublisher", "publish", "TOPIC"),
            JAVA_TEST_PATH: ("EligibilityPolicyTest", "blocksWhenOutstandingDebtExists"),
        },
    )
    return GeneratedCorpus(root=root, truth=truth, documents=(document,))


_JAVA_DOCUMENT_PARAGRAPHS = (
    ("Title", "Contract Renewal Policy"),
    ("Heading1", "Renewal window"),
    ("", "A contract may be renewed at any moment inside the 45 days that precede its expiry date."),
    ("", "The renewal window of 45 days applies to every plan without exception."),
    ("Heading1", "Debt"),
    ("", "A contract with outstanding debt cannot be renewed."),
)

PYTHON_API_PATH = "orders/api.py"
PYTHON_STATE_PATH = "orders/state.py"
PYTHON_VALIDATION_PATH = "orders/validation.py"
PYTHON_IDEMPOTENCY_PATH = "orders/idempotency.py"
PYTHON_QUEUE_PATH = "orders/dispatch.py"

_PYTHON_STATE = (
    "from __future__ import annotations",
    "",
    "from enum import Enum",
    "",
    "",
    _mark("enum", "class OrderState(str, Enum):"),
    _mark("draft", '    DRAFT = "draft"'),
    _mark("placed", '    PLACED = "placed"'),
    _mark("paid", '    PAID = "paid"'),
    _mark("cancelled", '    CANCELLED = "cancelled"'),
    "",
    "",
    _mark("table", "TRANSITIONS: dict[OrderState, tuple[OrderState, ...]] = {"),
    _mark("from_draft", "    OrderState.DRAFT: (OrderState.PLACED, OrderState.CANCELLED),"),
    _mark("from_placed", "    OrderState.PLACED: (OrderState.PAID, OrderState.CANCELLED),"),
    _mark("from_paid", "    OrderState.PAID: (),"),
    _mark("from_cancelled", "    OrderState.CANCELLED: (),"),
    "}",
    "",
    "",
    "class TransitionRejected(Exception):",
    "    def __init__(self, current: OrderState, target: OrderState) -> None:",
    '        super().__init__(f"{current.value} -> {target.value}")',
    "        self.current = current",
    "        self.target = target",
    "",
    "",
    _mark("advance", "def advance(current: OrderState, target: OrderState) -> OrderState:"),
    _mark("guard", "    if target not in TRANSITIONS[current]:"),
    _mark("reject", "        raise TransitionRejected(current, target)"),
    "    return target",
)

_PYTHON_VALIDATION = (
    "from __future__ import annotations",
    "",
    "from decimal import Decimal",
    "from typing import Any, Mapping",
    "",
    _mark("max_items", "MAX_ITEMS = 50"),
    _mark("min_total", 'MIN_TOTAL = Decimal("0.01")'),
    "",
    "",
    "class ValidationFailed(Exception):",
    "    def __init__(self, reason: str) -> None:",
    "        super().__init__(reason)",
    "        self.reason = reason",
    "",
    "",
    _mark("validate", "def validate_order(payload: Mapping[str, Any]) -> Decimal:"),
    _mark("items", '    items = payload.get("items") or []',),
    _mark("empty", "    if not items:"),
    _mark("empty_effect", '        raise ValidationFailed("order_without_items")'),
    _mark("too_many", "    if len(items) > MAX_ITEMS:"),
    _mark("too_many_effect", '        raise ValidationFailed("too_many_items")'),
    _mark("total", '    total = sum((Decimal(str(item["price"])) * int(item["quantity"]) for item in items), Decimal("0"))'),
    _mark("min_check", "    if total < MIN_TOTAL:"),
    _mark("min_effect", '        raise ValidationFailed("total_below_minimum")'),
    "    return total",
)

_PYTHON_IDEMPOTENCY = (
    "from __future__ import annotations",
    "",
    "from typing import Any, Mapping, MutableMapping",
    "",
    "",
    _mark("store", "class IdempotencyStore:"),
    "    def __init__(self) -> None:",
    "        self._entries: MutableMapping[str, Mapping[str, Any]] = {}",
    "",
    _mark("lookup", "    def replay(self, key: str) -> Mapping[str, Any] | None:"),
    _mark("get", "        return self._entries.get(key)"),
    "",
    _mark("remember", "    def remember(self, key: str, response: Mapping[str, Any]) -> None:"),
    _mark("write", "        self._entries[key] = dict(response)"),
)

_PYTHON_QUEUE = (
    "from __future__ import annotations",
    "",
    "from typing import Any, Mapping, Protocol",
    "",
    "",
    "class DispatchUnavailable(Exception):",
    "    pass",
    "",
    "",
    "class FulfilmentClient(Protocol):",
    "    def submit(self, order: Mapping[str, Any]) -> str: ...",
    "",
    "",
    _mark("queue", "class PendingQueue:"),
    "    def __init__(self) -> None:",
    "        self._pending: list[Mapping[str, Any]] = []",
    "",
    _mark("enqueue", "    def enqueue(self, order: Mapping[str, Any]) -> None:"),
    _mark("append", "        self._pending.append(dict(order))"),
    "",
    "    def size(self) -> int:",
    "        return len(self._pending)",
    "",
    "",
    _mark("dispatch", "def dispatch(order: Mapping[str, Any], client: FulfilmentClient, queue: PendingQueue) -> str:"),
    "    try:",
    _mark("submit", "        return client.submit(order)"),
    _mark("catch", "    except DispatchUnavailable:"),
    _mark("fallback", "        queue.enqueue(order)"),
    _mark("fallback_effect", '        return "queued"'),
)

_PYTHON_API = (
    "from __future__ import annotations",
    "",
    "from typing import Any, Mapping, MutableMapping",
    "",
    "from orders.dispatch import FulfilmentClient, PendingQueue, dispatch",
    "from orders.idempotency import IdempotencyStore",
    "from orders.state import OrderState, TransitionRejected, advance",
    "from orders.validation import ValidationFailed, validate_order",
    "",
    "",
    _mark("api", "class OrderApi:"),
    "    def __init__(self, client: FulfilmentClient) -> None:",
    "        self._orders: MutableMapping[str, dict[str, Any]] = {}",
    "        self._idempotency = IdempotencyStore()",
    "        self._queue = PendingQueue()",
    "        self._client = client",
    "",
    _mark("place", "    def place_order(self, payload: Mapping[str, Any], idempotency_key: str) -> Mapping[str, Any]:"),
    _mark("replay", "        replayed = self._idempotency.replay(idempotency_key)"),
    _mark("replay_return", "        if replayed is not None:"),
    _mark("replay_effect", "            return replayed"),
    "        try:",
    _mark("validate_call", "            total = validate_order(payload)"),
    _mark("invalid", "        except ValidationFailed as failure:"),
    _mark("invalid_effect", '            return {"status": "rejected", "reason": failure.reason}'),
    _mark("transition", "        state = advance(OrderState.DRAFT, OrderState.PLACED)"),
    _mark("record", '        order = {"id": payload["id"], "state": state.value, "total": str(total)}'),
    _mark("store_order", '        self._orders[str(payload["id"])] = order'),
    _mark("dispatch_call", "        outcome = dispatch(order, self._client, self._queue)"),
    _mark("response", '        response = {"status": "accepted", "order": order, "dispatch": outcome}'),
    _mark("remember", "        self._idempotency.remember(idempotency_key, response)"),
    "        return response",
    "",
    _mark("pay", "    def pay_order(self, order_id: str) -> Mapping[str, Any]:"),
    "        order = self._orders[order_id]",
    "        try:",
    _mark("pay_transition", "            state = advance(OrderState(order[\"state\"]), OrderState.PAID)"),
    _mark("pay_reject", "        except TransitionRejected as rejected:"),
    _mark("pay_reject_effect", '            return {"status": "rejected", "reason": "invalid_transition", "from": rejected.current.value}'),
    '        order["state"] = state.value',
    "        return order",
    "",
    _mark("cancel", "    def cancel_order(self, order_id: str) -> Mapping[str, Any]:"),
    "        order = self._orders[order_id]",
    _mark("cancel_transition", "        state = advance(OrderState(order[\"state\"]), OrderState.CANCELLED)"),
    '        order["state"] = state.value',
    "        return order",
)

_PYTHON_SUPPORT: Mapping[str, tuple[str, ...]] = {
    "orders/__init__.py": ("",),
    "pyproject.toml": (
        "[project]",
        'name = "orders-api"',
        'version = "1.0.0"',
    ),
}


def build_python(root: Path) -> GeneratedCorpus:
    api = _Sheet(PYTHON_API_PATH, _PYTHON_API)
    state = _Sheet(PYTHON_STATE_PATH, _PYTHON_STATE)
    validation = _Sheet(PYTHON_VALIDATION_PATH, _PYTHON_VALIDATION)
    idempotency = _Sheet(PYTHON_IDEMPOTENCY_PATH, _PYTHON_IDEMPOTENCY)
    queue = _Sheet(PYTHON_QUEUE_PATH, _PYTHON_QUEUE)
    for sheet in (api, state, validation, idempotency, queue):
        _write(root / sheet.path, sheet.text())
    for path, lines in _PYTHON_SUPPORT.items():
        _write(root / path, "\n".join(lines) + "\n")
    truth = RepositoryTruth(
        name="python",
        language="python",
        business_rules=(
            RuleTruth(
                key="order_requires_items",
                statement="an order without items is rejected",
                key_terms=("items", "order_without_items", "ValidationFailed"),
                conditions=("items list is empty",),
                effects=("ValidationFailed with reason order_without_items",),
                anchors=(validation.span("empty", "empty_effect"),),
            ),
            RuleTruth(
                key="order_total_minimum",
                statement="an order whose total is below the minimum is rejected",
                key_terms=("MIN_TOTAL", "total_below_minimum", "total"),
                conditions=("computed total lower than MIN_TOTAL",),
                effects=("ValidationFailed with reason total_below_minimum",),
                anchors=(
                    validation.line("min_total"),
                    validation.span("min_check", "min_effect"),
                ),
            ),
            RuleTruth(
                key="idempotent_place_order",
                statement="placing an order twice with the same idempotency key replays the first response",
                key_terms=("idempotency_key", "replay", "remember"),
                conditions=("idempotency key already stored",),
                effects=("the stored response is returned without creating a new order",),
                anchors=(
                    api.span("replay", "replay_effect"),
                    idempotency.span("lookup", "get"),
                ),
            ),
        ),
        edge_cases=(
            EdgeCaseTruth(
                key="too_many_items",
                statement="an order above the item limit is rejected",
                key_terms=("MAX_ITEMS", "too_many_items", "len"),
                condition="number of items greater than MAX_ITEMS",
                expected="ValidationFailed with reason too_many_items",
                anchors=(
                    validation.line("max_items"),
                    validation.span("too_many", "too_many_effect"),
                ),
            ),
            EdgeCaseTruth(
                key="paying_a_cancelled_order",
                statement="paying an order that is not placed is rejected as an invalid transition",
                key_terms=("TransitionRejected", "invalid_transition", "advance"),
                condition="current state has no transition to PAID",
                expected="rejected with reason invalid_transition",
                anchors=(api.span("pay_transition", "pay_reject_effect"),),
            ),
        ),
        invariants=(
            InvariantTruth(
                key="transition_table_is_closed",
                statement="only transitions declared in the transition table are allowed",
                key_terms=("TRANSITIONS", "advance", "TransitionRejected"),
                anchors=(
                    state.span("table", "from_cancelled"),
                    state.span("advance", "reject"),
                ),
            ),
            InvariantTruth(
                key="terminal_states_have_no_successor",
                statement="paid and cancelled are terminal states",
                key_terms=("PAID", "CANCELLED", "TRANSITIONS"),
                anchors=(state.span("from_paid", "from_cancelled"),),
            ),
        ),
        integrations=(
            IntegrationTruth(
                key="fulfilment_client",
                statement="orders are submitted to the fulfilment client",
                key_terms=("FulfilmentClient", "submit", "dispatch"),
                direction="outbound",
                protocol="rpc",
                anchors=(queue.span("dispatch", "submit"),),
            ),
        ),
        entrypoints=(
            EntryPointTruth(
                key="place_order",
                statement="place_order is the entry point that creates an order",
                key_terms=("place_order", "OrderApi", "idempotency_key"),
                mechanism="api",
                anchors=(api.span("place", "replay"),),
            ),
            EntryPointTruth(
                key="pay_order",
                statement="pay_order is the entry point that settles an order",
                key_terms=("pay_order", "OrderApi", "PAID"),
                mechanism="api",
                anchors=(api.span("pay", "pay_transition"),),
            ),
        ),
        states=(
            StateTruth(
                key="state_draft",
                statement="draft is the initial order state",
                key_terms=("DRAFT", "draft", "OrderState"),
                anchors=(state.line("draft"),),
            ),
            StateTruth(
                key="state_placed",
                statement="placed is the state of an accepted order",
                key_terms=("PLACED", "placed", "OrderState"),
                anchors=(state.line("placed"),),
            ),
            StateTruth(
                key="state_paid",
                statement="paid is the state of a settled order",
                key_terms=("PAID", "paid", "OrderState"),
                anchors=(state.line("paid"),),
            ),
            StateTruth(
                key="state_cancelled",
                statement="cancelled is the state of an abandoned order",
                key_terms=("CANCELLED", "cancelled", "OrderState"),
                anchors=(state.line("cancelled"),),
            ),
        ),
        transitions=(
            TransitionTruth(
                key="draft_to_placed",
                statement="a draft order becomes placed when it is accepted",
                key_terms=("DRAFT", "PLACED", "TRANSITIONS"),
                from_state="draft",
                to_state="placed",
                anchors=(state.line("from_draft"), api.line("transition")),
            ),
            TransitionTruth(
                key="placed_to_paid",
                statement="a placed order becomes paid when it is settled",
                key_terms=("PLACED", "PAID", "TRANSITIONS"),
                from_state="placed",
                to_state="paid",
                anchors=(state.line("from_placed"), api.line("pay_transition")),
            ),
            TransitionTruth(
                key="placed_to_cancelled",
                statement="a placed order can be cancelled",
                key_terms=("PLACED", "CANCELLED", "TRANSITIONS"),
                from_state="placed",
                to_state="cancelled",
                anchors=(state.line("from_placed"), api.line("cancel_transition")),
            ),
        ),
        failure_modes=(
            FailureModeTruth(
                key="dispatch_fallback_to_queue",
                statement="when the fulfilment client is unavailable the order is queued",
                key_terms=("DispatchUnavailable", "enqueue", "queued"),
                trigger="DispatchUnavailable raised by the fulfilment client",
                effect="the order is enqueued and the dispatch result is queued",
                anchors=(queue.span("catch", "fallback_effect"),),
            ),
        ),
        symbols_by_path={
            PYTHON_API_PATH: ("OrderApi", "place_order", "pay_order", "cancel_order"),
            PYTHON_STATE_PATH: ("OrderState", "TRANSITIONS", "advance"),
            PYTHON_VALIDATION_PATH: ("validate_order", "MAX_ITEMS", "MIN_TOTAL"),
            PYTHON_IDEMPOTENCY_PATH: ("IdempotencyStore", "replay", "remember"),
            PYTHON_QUEUE_PATH: ("PendingQueue", "dispatch", "DispatchUnavailable"),
        },
    )
    return GeneratedCorpus(root=root, truth=truth)


COBOL_PROGRAM_PATH = "src/cobol/BILLRUN.cbl"
COBOL_SUBPROGRAM_PATH = "src/cobol/CHKLIMIT.cbl"
COBOL_COPYBOOK_PATH = "src/copybook/ACCTREC.cpy"
COBOL_JCL_PATH = "jcl/BILLRUN.jcl"

_COBOL_COPYBOOK = (
    "       01  ACCT-RECORD.",
    _mark("acct_id", "           05  ACCT-ID              PIC X(10)."),
    _mark("acct_balance", "           05  ACCT-BALANCE         PIC S9(9)V99 COMP-3."),
    _mark("acct_limit", "           05  ACCT-LIMIT           PIC S9(9)V99 COMP-3."),
    _mark("acct_status", "           05  ACCT-STATUS          PIC X(01)."),
    _mark("acct_cycle", "           05  ACCT-CYCLE-DAY       PIC 9(02)."),
)

_COBOL_SUBPROGRAM = (
    "       IDENTIFICATION DIVISION.",
    _mark("program", "       PROGRAM-ID. CHKLIMIT."),
    "       DATA DIVISION.",
    "       LINKAGE SECTION.",
    _mark("copy_link", "       COPY ACCTREC."),
    "       01  LS-RESULT                PIC X(01).",
    _mark("procedure", "       PROCEDURE DIVISION USING ACCT-RECORD LS-RESULT."),
    _mark("check", "           IF ACCT-BALANCE > ACCT-LIMIT"),
    _mark("check_effect", '               MOVE "R" TO LS-RESULT'),
    "           ELSE",
    _mark("ok_effect", '               MOVE "A" TO LS-RESULT'),
    "           END-IF.",
    "           GOBACK.",
)

_COBOL_PROGRAM = (
    "       IDENTIFICATION DIVISION.",
    _mark("program", "       PROGRAM-ID. BILLRUN."),
    "       ENVIRONMENT DIVISION.",
    "       INPUT-OUTPUT SECTION.",
    "       FILE-CONTROL.",
    _mark("select", "           SELECT ACCT-FILE ASSIGN TO ACCTIN"),
    "               ORGANIZATION IS SEQUENTIAL.",
    "       DATA DIVISION.",
    "       FILE SECTION.",
    "       FD  ACCT-FILE.",
    _mark("copy_fd", "       COPY ACCTREC."),
    "       WORKING-STORAGE SECTION.",
    _mark("eof", '       01  WS-EOF                   PIC X(01) VALUE "N".'),
    "       01  WS-RESULT                PIC X(01).",
    _mark("charged", "       01  WS-CHARGED               PIC 9(07) VALUE ZERO."),
    _mark("sqlcode", "       01  WS-SQLCODE               PIC S9(09) COMP VALUE ZERO."),
    _mark("fee", "       01  WS-FEE                   PIC S9(05)V99 COMP-3 VALUE 12.50."),
    "       EXEC SQL INCLUDE SQLCA END-EXEC.",
    _mark("proc", "       PROCEDURE DIVISION."),
    _mark("main", "       MAIN-PARA."),
    "           OPEN INPUT ACCT-FILE",
    _mark("perform", '           PERFORM PROCESS-ACCOUNT UNTIL WS-EOF = "Y"'),
    "           CLOSE ACCT-FILE",
    "           STOP RUN.",
    "",
    _mark("process", "       PROCESS-ACCOUNT."),
    "           READ ACCT-FILE",
    _mark("at_end", '               AT END MOVE "Y" TO WS-EOF'),
    "               NOT AT END",
    _mark("call", "                   CALL \"CHKLIMIT\" USING ACCT-RECORD WS-RESULT"),
    _mark("rejected", '                   IF WS-RESULT = "R"'),
    _mark("rejected_effect", "                       PERFORM LOG-REJECTED"),
    "                   ELSE",
    _mark("charge", "                       PERFORM CHARGE-FEE"),
    "                   END-IF",
    "           END-READ.",
    "",
    _mark("charge_para", "       CHARGE-FEE."),
    _mark("sql", "           EXEC SQL"),
    _mark("update", "               UPDATE ACCOUNT_LEDGER"),
    _mark("set", "                  SET BALANCE = BALANCE + :WS-FEE"),
    _mark("where", "                WHERE ACCT_ID = :ACCT-ID"),
    _mark("sql_end", "           END-EXEC."),
    _mark("sqlcheck", "           IF SQLCODE NOT = 0"),
    _mark("sqlfail", "               MOVE SQLCODE TO WS-SQLCODE"),
    _mark("rollback", "               PERFORM ABORT-RUN"),
    "           ELSE",
    "               ADD 1 TO WS-CHARGED",
    "           END-IF.",
    "",
    "       LOG-REJECTED.",
    _mark("log", '           DISPLAY "REJECTED " ACCT-ID.'),
    "",
    _mark("abort_para", "       ABORT-RUN."),
    _mark("abort_sql", "           EXEC SQL ROLLBACK END-EXEC"),
    _mark("abort_effect", "           MOVE 12 TO RETURN-CODE"),
    "           STOP RUN.",
)

_COBOL_JCL = (
    _mark("job", "//BILLRUN  JOB (ACCT),'BILLING RUN',CLASS=A,MSGCLASS=X"),
    _mark("step1", "//STEP010  EXEC PGM=SORT"),
    "//SORTIN   DD DSN=ACME.ACCT.RAW,DISP=SHR",
    _mark("sortout", "//SORTOUT  DD DSN=ACME.ACCT.SORTED,DISP=(NEW,PASS)"),
    "//SYSIN    DD *",
    "  SORT FIELDS=(1,10,CH,A)",
    "/*",
    _mark("step2", "//STEP020  EXEC PGM=BILLRUN,COND=(0,NE,STEP010)"),
    _mark("acctin", "//ACCTIN   DD DSN=ACME.ACCT.SORTED,DISP=(OLD,DELETE)"),
    "//SYSOUT   DD SYSOUT=*",
)


def build_cobol(root: Path) -> GeneratedCorpus:
    program = _Sheet(COBOL_PROGRAM_PATH, _COBOL_PROGRAM)
    subprogram = _Sheet(COBOL_SUBPROGRAM_PATH, _COBOL_SUBPROGRAM)
    copybook = _Sheet(COBOL_COPYBOOK_PATH, _COBOL_COPYBOOK)
    jcl = _Sheet(COBOL_JCL_PATH, _COBOL_JCL)
    for sheet in (program, subprogram, copybook, jcl):
        _write(root / sheet.path, sheet.text())
    truth = RepositoryTruth(
        name="cobol",
        language="cobol",
        business_rules=(
            RuleTruth(
                key="reject_account_above_limit",
                statement="an account whose balance exceeds its limit is rejected",
                key_terms=("ACCT-BALANCE", "ACCT-LIMIT", "CHKLIMIT"),
                conditions=("ACCT-BALANCE greater than ACCT-LIMIT",),
                effects=("result R meaning rejected",),
                anchors=(subprogram.span("check", "check_effect"),),
            ),
            RuleTruth(
                key="charge_fee_for_accepted_account",
                statement="an accepted account has the billing fee added to its ledger balance",
                key_terms=("CHARGE-FEE", "WS-FEE", "ACCOUNT_LEDGER", "BALANCE"),
                conditions=("CHKLIMIT result different from R",),
                effects=("ACCOUNT_LEDGER balance increased by WS-FEE",),
                anchors=(
                    program.span("charge", "charge"),
                    program.span("sql", "sql_end"),
                ),
            ),
        ),
        edge_cases=(
            EdgeCaseTruth(
                key="end_of_input_file",
                statement="the run stops when the account file reaches the end",
                key_terms=("AT END", "WS-EOF", "PERFORM"),
                condition="READ reaches AT END",
                expected="WS-EOF becomes Y and the PERFORM loop ends",
                anchors=(program.span("perform", "perform"), program.line("at_end")),
            ),
            EdgeCaseTruth(
                key="rejected_account_is_logged",
                statement="a rejected account is logged instead of charged",
                key_terms=("WS-RESULT", "LOG-REJECTED", "DISPLAY"),
                condition="WS-RESULT equals R",
                expected="LOG-REJECTED is performed and no fee is charged",
                anchors=(program.span("rejected", "rejected_effect"),),
            ),
        ),
        invariants=(
            InvariantTruth(
                key="ledger_layout_from_copybook",
                statement="the account layout comes from the ACCTREC copybook in every program",
                key_terms=("ACCTREC", "COPY", "ACCT-RECORD"),
                anchors=(program.line("copy_fd"), subprogram.line("copy_link")),
            ),
        ),
        integrations=(
            IntegrationTruth(
                key="account_ledger_sql_update",
                statement="the batch updates the ACCOUNT_LEDGER table through embedded SQL",
                key_terms=("EXEC SQL", "UPDATE", "ACCOUNT_LEDGER"),
                direction="outbound",
                protocol="sql",
                anchors=(program.span("sql", "sql_end"),),
            ),
            IntegrationTruth(
                key="chklimit_subprogram_call",
                statement="BILLRUN calls the CHKLIMIT subprogram for every account",
                key_terms=("CALL", "CHKLIMIT", "ACCT-RECORD"),
                direction="internal",
                protocol="cobol_call",
                anchors=(program.line("call"), subprogram.span("program", "procedure")),
            ),
        ),
        entrypoints=(
            EntryPointTruth(
                key="billrun_batch_step",
                statement="STEP020 of the BILLRUN job executes the BILLRUN program",
                key_terms=("STEP020", "EXEC PGM=BILLRUN", "BILLRUN"),
                mechanism="batch",
                anchors=(jcl.span("step2", "acctin"),),
            ),
            EntryPointTruth(
                key="billrun_main_para",
                statement="MAIN-PARA is the procedure entry of BILLRUN",
                key_terms=("MAIN-PARA", "PROCEDURE DIVISION", "BILLRUN"),
                mechanism="batch",
                anchors=(program.span("proc", "main"),),
            ),
        ),
        failure_modes=(
            FailureModeTruth(
                key="sql_failure_aborts_run",
                statement="a non zero SQLCODE rolls the run back and ends with return code 12",
                key_terms=("SQLCODE", "ROLLBACK", "RETURN-CODE", "12"),
                trigger="SQLCODE different from zero after the ledger update",
                effect="ABORT-RUN rolls back and sets RETURN-CODE 12",
                anchors=(
                    program.span("sqlcheck", "rollback"),
                    program.span("abort_para", "abort_effect"),
                ),
            ),
            FailureModeTruth(
                key="step_condition_guard",
                statement="STEP020 is skipped when STEP010 does not end with return code zero",
                key_terms=("COND", "STEP010", "STEP020"),
                trigger="STEP010 ends with a non zero return code",
                effect="STEP020 is not executed",
                anchors=(jcl.line("step2"),),
            ),
        ),
        symbols_by_path={
            COBOL_PROGRAM_PATH: ("BILLRUN", "MAIN-PARA", "CHARGE-FEE", "ABORT-RUN"),
            COBOL_SUBPROGRAM_PATH: ("CHKLIMIT", "PROCEDURE DIVISION"),
            COBOL_COPYBOOK_PATH: ("ACCT-RECORD", "ACCT-BALANCE", "ACCT-LIMIT"),
            COBOL_JCL_PATH: ("STEP010", "STEP020", "BILLRUN"),
        },
    )
    return GeneratedCorpus(root=root, truth=truth)


_BUILDERS: Mapping[str, Callable[[Path], GeneratedCorpus]] = {
    "java": build_java,
    "python": build_python,
    "cobol": build_cobol,
}

CORPUS_NAMES: tuple[str, ...] = tuple(sorted(_BUILDERS))


def materialize(name: str, root: Path) -> GeneratedCorpus:
    builder = _BUILDERS.get(name)
    if builder is None:
        raise CorpusUnknown(name)
    target = Path(root)
    target.mkdir(parents=True, exist_ok=True)
    return builder(target)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


_W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
_RELS_NS = 'xmlns="http://schemas.openxmlformats.org/package/2006/relationships"'


def _docx_paragraph(style: str, text: str) -> str:
    holder = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
    return f"<w:p>{holder}<w:r><w:t>{text}</w:t></w:r></w:p>"


def _write_docx(path: Path, paragraphs: tuple[tuple[str, str], ...]) -> Path:
    body = "".join(_docx_paragraph(style, text) for style, text in paragraphs)
    document = f'<?xml version="1.0"?><w:document {_W}><w:body>{body}</w:body></w:document>'
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types/>')
        archive.writestr("word/document.xml", document)
        archive.writestr(
            "word/_rels/document.xml.rels",
            f'<?xml version="1.0"?><Relationships {_RELS_NS}></Relationships>',
        )
        archive.writestr(
            "docProps/core.xml",
            '<?xml version="1.0"?><cp:coreProperties '
            'xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/">'
            "<dc:title>Contract Renewal Policy</dc:title>"
            "<dc:creator>Contract Office</dc:creator>"
            "</cp:coreProperties>",
        )
    return path
