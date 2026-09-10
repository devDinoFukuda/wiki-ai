from __future__ import annotations

import json
from pathlib import Path

from wiki_ai.repository.snapshot import RepositorySnapshot, SnapshotSpec, take_snapshot

__all__ = [
    "write",
    "snapshot_of",
    "java_repo",
    "python_repo",
    "web_repo",
    "mainframe_repo",
    "sql_repo",
    "mixed_repo",
]


def write(root: Path, relative: str, content: str) -> Path:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target


def snapshot_of(root: Path) -> RepositorySnapshot:
    return take_snapshot(SnapshotSpec(root=root))


_JAVA_CONTROLLER = """package com.acme.order;

import org.springframework.web.bind.annotation.RestController;
import org.springframework.beans.factory.annotation.Value;
import com.acme.order.OrderService;

@RestController
public class OrderController {
    @Value("${order.topic}")
    private String topic;
    private final OrderService service;
    public OrderController(OrderService service) {
        this.service = service;
    }
    public String place(String reference) {
        return service.place(reference);
    }
}
"""

_JAVA_SERVICE = """package com.acme.order;

import com.acme.order.OrderRepository;

public class OrderService {
    private final OrderRepository repository;
    public OrderService(OrderRepository repository) {
        this.repository = repository;
    }
    public String place(String reference) {
        return repository.save(reference);
    }
}
"""

_JAVA_REPOSITORY = """package com.acme.order;

public interface OrderRepository {
    String save(String reference);
}
"""

_JAVA_PRODUCER = """package com.acme.order;

import org.apache.kafka.clients.producer.KafkaProducer;

public class OrderProducer {
    private final KafkaProducer<String, String> producer;
    public OrderProducer(KafkaProducer<String, String> producer) {
        this.producer = producer;
    }
    public void emit(String payload) {
        producer.send(null);
    }
}
"""

_JAVA_TEST = """package com.acme.order;

public class OrderServiceTest {
    public void placesOrder() {
        OrderService service = new OrderService(null);
        service.place("R-1");
    }
}
"""

_POM = """<project>
  <dependencies>
    <dependency>
      <groupId>org.springframework.boot</groupId>
      <artifactId>spring-boot-starter-web</artifactId>
      <version>3.2.0</version>
    </dependency>
    <dependency>
      <groupId>org.apache.kafka</groupId>
      <artifactId>kafka-clients</artifactId>
      <version>3.6.1</version>
    </dependency>
  </dependencies>
</project>
"""

_APPLICATION_YML = """spring:
  datasource:
    url: jdbc:postgresql://db.acme.internal/orders
order:
  topic: orders-v1
  endpoint: https://api.acme.com/orders
"""


def java_repo(root: Path) -> RepositorySnapshot:
    base = "src/main/java/com/acme/order"
    write(root, f"{base}/OrderController.java", _JAVA_CONTROLLER)
    write(root, f"{base}/OrderService.java", _JAVA_SERVICE)
    write(root, f"{base}/OrderRepository.java", _JAVA_REPOSITORY)
    write(root, f"{base}/OrderProducer.java", _JAVA_PRODUCER)
    write(root, "src/test/java/com/acme/order/OrderServiceTest.java", _JAVA_TEST)
    write(root, "pom.xml", _POM)
    write(root, "src/main/resources/application.yml", _APPLICATION_YML)
    return snapshot_of(root)


_PY_SERVICE = """import os

from billing.repository import save_invoice

INVOICE_TOPIC = os.environ["INVOICE_TOPIC"]
GATEWAY_URL = os.getenv("GATEWAY_URL", "https://payments.acme.com/v1")


class InvoiceService:
    def issue(self, amount):
        return save_invoice(amount)
"""

_PY_REPOSITORY = """def save_invoice(amount):
    return {"amount": amount}
"""

_PY_TEST = """from billing.service import InvoiceService


def test_issue_returns_amount():
    assert InvoiceService().issue(10)["amount"] == 10
"""

_PYPROJECT = """[project]
name = "billing"
version = "0.1.0"
dependencies = ["requests>=2.31", "kafka-python==2.0.2"]
"""


def python_repo(root: Path) -> RepositorySnapshot:
    write(root, "billing/service.py", _PY_SERVICE)
    write(root, "billing/repository.py", _PY_REPOSITORY)
    write(root, "tests/test_service.py", _PY_TEST)
    write(root, "pyproject.toml", _PYPROJECT)
    write(root, ".env", "INVOICE_TOPIC=invoices-v1\nGATEWAY_URL=https://payments.acme.com/v1\n")
    return snapshot_of(root)


_TS_CLIENT = """import axios from 'axios';
import { buildPayload } from './payload';

export interface Cart {
  id: string;
}

export const submitCart = async (cart: Cart) =>
  axios.post('https://api.acme.com/carts', buildPayload(cart));

export function resetCart(cart: Cart): Cart {
  return { id: cart.id };
}
"""

_TS_PAYLOAD = """export function buildPayload(cart) {
  return { id: cart.id };
}
"""

_JS_WORKER = """const amqp = require('amqplib');

async function consumeQueue(name) {
  const connection = await amqp.connect('amqp://broker.acme.internal');
  const queue = name;
  return connection.createChannel(queue);
}

module.exports = { consumeQueue };
"""

_TS_SPEC = """import { submitCart } from './client';

describe('submitCart', () => {
  it('submits', () => submitCart({ id: '1' }));
});
"""


def web_repo(root: Path) -> RepositorySnapshot:
    write(root, "src/client.ts", _TS_CLIENT)
    write(root, "src/payload.ts", _TS_PAYLOAD)
    write(root, "src/worker.js", _JS_WORKER)
    write(root, "src/client.spec.ts", _TS_SPEC)
    write(
        root,
        "package.json",
        json.dumps(
            {
                "name": "storefront",
                "dependencies": {"axios": "^1.6.0", "amqplib": "^0.10.3"},
                "devDependencies": {"jest": "^29.7.0"},
            },
            indent=2,
        )
        + "\n",
    )
    return snapshot_of(root)


_COBOL_PROGRAM = """       IDENTIFICATION DIVISION.
       PROGRAM-ID. PAYRUN.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       COPY PAYREC.
       PROCEDURE DIVISION.
       MAIN-LOGIC SECTION.
           PERFORM CALC-TOTAL
           CALL 'TAXCALC' USING WS-GROSS WS-NET
           STOP RUN.
       CALC-TOTAL SECTION.
           COMPUTE WS-GROSS = WS-HOURS * WS-RATE.
"""

_COPYBOOK = """       01  PAY-RECORD.
           05  WS-HOURS   PIC 9(3).
           05  WS-RATE    PIC 9(5)V99.
           05  WS-GROSS   PIC 9(7)V99.
           05  WS-NET     PIC 9(7)V99.
"""

_JCL = """//PAYJOB   JOB (ACCT),'PAYROLL RUN',CLASS=A
//STEP01   EXEC PGM=PAYRUN
//STEPLIB  DD DSN=PROD.LOADLIB,DISP=SHR
//INFILE   DD DSN=PROD.PAYROLL.MASTER,DISP=SHR
//STEP02   EXEC PGM=TAXCALC
"""


def mainframe_repo(root: Path) -> RepositorySnapshot:
    write(root, "cobol/PAYRUN.cbl", _COBOL_PROGRAM)
    write(root, "copybook/PAYREC.cpy", _COPYBOOK)
    write(root, "jcl/PAYJOB.jcl", _JCL)
    return snapshot_of(root)


_SQL_SCHEMA = """CREATE TABLE PAYROLL_MASTER (
  EMPLOYEE_ID INT NOT NULL,
  GROSS DECIMAL(9,2)
);

CREATE TABLE EMPLOYEE (
  EMPLOYEE_ID INT NOT NULL,
  NAME VARCHAR(80)
);

CREATE PROCEDURE CALC_TAX AS
  SELECT E.NAME, P.GROSS
  FROM PAYROLL_MASTER P
  JOIN EMPLOYEE E ON E.EMPLOYEE_ID = P.EMPLOYEE_ID;

CREATE FUNCTION NET_AMOUNT (GROSS DECIMAL) RETURNS DECIMAL AS RETURN GROSS;
"""


def sql_repo(root: Path) -> RepositorySnapshot:
    write(root, "db/schema.sql", _SQL_SCHEMA)
    return snapshot_of(root)


_UNKNOWN_LANGUAGE = """widget PayrollWidget = {
  compute_total(hours)
  emit_event(hours)
}
threshold = 42
"""

_GO_SERVICE = """package ledger

import (
	"fmt"
	"github.com/acme/ledger/store"
)

type Ledger struct {
	name string
}

func Post(entry string) string {
	return fmt.Sprintf("%s", store.Save(entry))
}
"""


def mixed_repo(root: Path) -> RepositorySnapshot:
    java_repo(root / "services" / "orders")
    python_repo(root / "services" / "billing")
    web_repo(root / "apps" / "storefront")
    mainframe_repo(root / "mainframe")
    sql_repo(root / "database")
    write(root, "edge/payroll.zzz", _UNKNOWN_LANGUAGE)
    write(root, "ledger/post.go", _GO_SERVICE)
    write(root, "go.mod", "module github.com/acme/ledger\n\ngo 1.22\n\nrequire (\n\tgithub.com/google/uuid v1.6.0\n)\n")
    return snapshot_of(root)
