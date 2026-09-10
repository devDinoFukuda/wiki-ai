from __future__ import annotations

from pathlib import Path

from tests.benchmark.ground_truth.sheets import GeneratedCorpus, Sheet, mark, write
from tests.benchmark.ground_truth.truth import (
    EdgeCaseTruth,
    EntryPointTruth,
    FailureModeTruth,
    IntegrationTruth,
    InvariantTruth,
    RepositoryTruth,
    RuleTruth,
)

__all__ = [
    "COBOL_PROGRAM_PATH",
    "COBOL_SUBPROGRAM_PATH",
    "COBOL_COPYBOOK_PATH",
    "COBOL_JCL_PATH",
    "build_cobol",
]


COBOL_PROGRAM_PATH = "src/cobol/BILLRUN.cbl"
COBOL_SUBPROGRAM_PATH = "src/cobol/CHKLIMIT.cbl"
COBOL_COPYBOOK_PATH = "src/copybook/ACCTREC.cpy"
COBOL_JCL_PATH = "jcl/BILLRUN.jcl"

_COBOL_COPYBOOK = (
    "       01  ACCT-RECORD.",
    mark("acct_id", "           05  ACCT-ID              PIC X(10)."),
    mark("acct_balance", "           05  ACCT-BALANCE         PIC S9(9)V99 COMP-3."),
    mark("acct_limit", "           05  ACCT-LIMIT           PIC S9(9)V99 COMP-3."),
    mark("acct_status", "           05  ACCT-STATUS          PIC X(01)."),
    mark("acct_cycle", "           05  ACCT-CYCLE-DAY       PIC 9(02)."),
)

_COBOL_SUBPROGRAM = (
    "       IDENTIFICATION DIVISION.",
    mark("program", "       PROGRAM-ID. CHKLIMIT."),
    "       DATA DIVISION.",
    "       LINKAGE SECTION.",
    mark("copy_link", "       COPY ACCTREC."),
    "       01  LS-RESULT                PIC X(01).",
    mark("procedure", "       PROCEDURE DIVISION USING ACCT-RECORD LS-RESULT."),
    mark("check", "           IF ACCT-BALANCE > ACCT-LIMIT"),
    mark("check_effect", '               MOVE "R" TO LS-RESULT'),
    "           ELSE",
    mark("ok_effect", '               MOVE "A" TO LS-RESULT'),
    "           END-IF.",
    "           GOBACK.",
)

_COBOL_PROGRAM = (
    "       IDENTIFICATION DIVISION.",
    mark("program", "       PROGRAM-ID. BILLRUN."),
    "       ENVIRONMENT DIVISION.",
    "       INPUT-OUTPUT SECTION.",
    "       FILE-CONTROL.",
    mark("select", "           SELECT ACCT-FILE ASSIGN TO ACCTIN"),
    "               ORGANIZATION IS SEQUENTIAL.",
    "       DATA DIVISION.",
    "       FILE SECTION.",
    "       FD  ACCT-FILE.",
    mark("copy_fd", "       COPY ACCTREC."),
    "       WORKING-STORAGE SECTION.",
    mark("eof", '       01  WS-EOF                   PIC X(01) VALUE "N".'),
    "       01  WS-RESULT                PIC X(01).",
    mark("charged", "       01  WS-CHARGED               PIC 9(07) VALUE ZERO."),
    mark("sqlcode", "       01  WS-SQLCODE               PIC S9(09) COMP VALUE ZERO."),
    mark("fee", "       01  WS-FEE                   PIC S9(05)V99 COMP-3 VALUE 12.50."),
    "       EXEC SQL INCLUDE SQLCA END-EXEC.",
    mark("proc", "       PROCEDURE DIVISION."),
    mark("main", "       MAIN-PARA."),
    "           OPEN INPUT ACCT-FILE",
    mark("perform", '           PERFORM PROCESS-ACCOUNT UNTIL WS-EOF = "Y"'),
    "           CLOSE ACCT-FILE",
    "           STOP RUN.",
    "",
    mark("process", "       PROCESS-ACCOUNT."),
    "           READ ACCT-FILE",
    mark("at_end", '               AT END MOVE "Y" TO WS-EOF'),
    "               NOT AT END",
    mark("call", "                   CALL \"CHKLIMIT\" USING ACCT-RECORD WS-RESULT"),
    mark("rejected", '                   IF WS-RESULT = "R"'),
    mark("rejected_effect", "                       PERFORM LOG-REJECTED"),
    "                   ELSE",
    mark("charge", "                       PERFORM CHARGE-FEE"),
    "                   END-IF",
    "           END-READ.",
    "",
    mark("charge_para", "       CHARGE-FEE."),
    mark("sql", "           EXEC SQL"),
    mark("update", "               UPDATE ACCOUNT_LEDGER"),
    mark("set", "                  SET BALANCE = BALANCE + :WS-FEE"),
    mark("where", "                WHERE ACCT_ID = :ACCT-ID"),
    mark("sql_end", "           END-EXEC."),
    mark("sqlcheck", "           IF SQLCODE NOT = 0"),
    mark("sqlfail", "               MOVE SQLCODE TO WS-SQLCODE"),
    mark("rollback", "               PERFORM ABORT-RUN"),
    "           ELSE",
    "               ADD 1 TO WS-CHARGED",
    "           END-IF.",
    "",
    "       LOG-REJECTED.",
    mark("log", '           DISPLAY "REJECTED " ACCT-ID.'),
    "",
    mark("abort_para", "       ABORT-RUN."),
    mark("abort_sql", "           EXEC SQL ROLLBACK END-EXEC"),
    mark("abort_effect", "           MOVE 12 TO RETURN-CODE"),
    "           STOP RUN.",
)

_COBOL_JCL = (
    mark("job", "//BILLRUN  JOB (ACCT),'BILLING RUN',CLASS=A,MSGCLASS=X"),
    mark("step1", "//STEP010  EXEC PGM=SORT"),
    "//SORTIN   DD DSN=ACME.ACCT.RAW,DISP=SHR",
    mark("sortout", "//SORTOUT  DD DSN=ACME.ACCT.SORTED,DISP=(NEW,PASS)"),
    "//SYSIN    DD *",
    "  SORT FIELDS=(1,10,CH,A)",
    "/*",
    mark("step2", "//STEP020  EXEC PGM=BILLRUN,COND=(0,NE,STEP010)"),
    mark("acctin", "//ACCTIN   DD DSN=ACME.ACCT.SORTED,DISP=(OLD,DELETE)"),
    "//SYSOUT   DD SYSOUT=*",
)


def build_cobol(root: Path) -> GeneratedCorpus:
    program = Sheet(COBOL_PROGRAM_PATH, _COBOL_PROGRAM)
    subprogram = Sheet(COBOL_SUBPROGRAM_PATH, _COBOL_SUBPROGRAM)
    copybook = Sheet(COBOL_COPYBOOK_PATH, _COBOL_COPYBOOK)
    jcl = Sheet(COBOL_JCL_PATH, _COBOL_JCL)
    for sheet in (program, subprogram, copybook, jcl):
        write(root / sheet.path, sheet.text())
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
