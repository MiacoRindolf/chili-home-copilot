from app.services.coding_task import validation_contracts


def test_pytest_verbose_output_preserves_stable_contract_identities():
    evidence = validation_contracts.test_contract_evidence(
        {
            "step_key": "pytest_targeted",
            "stdout": (
                "tests/test_owner.py::test_alpha PASSED [ 50%]\n"
                "tests/test_owner.py::test_beta[value] FAILED [100%]\n"
            ),
        }
    )

    assert evidence["complete"] is True
    assert evidence["passed_ids"] == ["tests/test_owner.py::test_alpha"]
    assert evidence["failed_ids"] == ["tests/test_owner.py::test_beta[value]"]


def test_quiet_pytest_failure_summary_is_not_a_complete_contract_inventory():
    evidence = validation_contracts.test_contract_evidence(
        {
            "step_key": "pytest_targeted",
            "stdout": "FAILED tests/test_owner.py::test_beta - AssertionError",
        }
    )

    assert evidence["failed_ids"] == ["tests/test_owner.py::test_beta"]
    assert evidence["complete"] is False


def test_pytest_verbose_parser_does_not_join_summary_to_next_failed_row():
    evidence = validation_contracts.test_contract_evidence(
        {
            "runner": "pytest",
            "output": (
                "tests/test_owner.py::test_alpha FAILED [ 50%]\n"
                "tests/test_owner.py::test_beta FAILED [100%]\n"
                "FAILED tests/test_owner.py::test_alpha - TypeError: bad input\n"
                "FAILED tests/test_owner.py::test_beta - AssertionError\n"
            ),
        }
    )

    assert evidence["failed_ids"] == [
        "tests/test_owner.py::test_alpha",
        "tests/test_owner.py::test_beta",
    ]


def test_sectioned_node_output_scopes_equal_names_to_their_test_files():
    evidence = validation_contracts.test_contract_evidence(
        {
            "runner": "node_test",
            "output": (
                "[tests/one.test.mjs]\n"
                "\u2714 preserves value (1.2ms)\n\n"
                "[tests/two.test.mjs]\n"
                "\u2716 preserves value (2.4ms)\n"
            ),
        }
    )

    assert evidence["passed_ids"] == [
        "tests/one.test.mjs::preserves value"
    ]
    assert evidence["failed_ids"] == [
        "tests/two.test.mjs::preserves value"
    ]


def test_contract_progress_requires_resolved_identity_and_preserves_green_set():
    before = {
        "passed_ids": ["alpha"],
        "failed_ids": ["beta", "gamma"],
        "complete": True,
    }
    progress = {
        "passed_ids": ["alpha", "beta"],
        "failed_ids": ["gamma"],
        "complete": True,
    }
    swap = {
        "passed_ids": ["beta", "gamma"],
        "failed_ids": ["alpha"],
        "complete": True,
    }

    assert validation_contracts.contract_progressed(before, progress) is True
    assert validation_contracts.contract_progressed(before, swap) is False
    assert validation_contracts.contract_regressions(before, swap) == ["alpha"]


def test_incomplete_after_inventory_treats_omitted_pass_as_unknown():
    before = {
        "passed_ids": ["alpha"],
        "failed_ids": ["beta"],
        "complete": True,
    }
    after = {
        "passed_ids": ["beta"],
        "failed_ids": [],
        "observed_ids": ["beta"],
        "complete": False,
    }

    assert validation_contracts.contract_regressions(before, after) == []
    assert validation_contracts.contract_progressed(before, after) is True


def test_incomplete_after_inventory_preserves_explicit_pass_to_fail_regression():
    before = {
        "passed_ids": ["alpha"],
        "failed_ids": ["beta"],
        "complete": True,
    }
    after = {
        "passed_ids": ["beta"],
        "failed_ids": ["alpha"],
        "observed_ids": ["alpha", "beta"],
        "complete": False,
    }

    assert validation_contracts.contract_regressions(before, after) == ["alpha"]
    assert validation_contracts.contract_progressed(before, after) is False


def test_complete_after_inventory_keeps_omitted_pass_as_regression():
    before = {
        "passed_ids": ["alpha"],
        "failed_ids": ["beta"],
        "complete": True,
    }
    after = {
        "passed_ids": ["beta"],
        "failed_ids": [],
        "observed_ids": ["beta"],
        "complete": True,
    }

    assert validation_contracts.contract_regressions(before, after) == ["alpha"]
    assert validation_contracts.contract_progressed(before, after) is False


def test_failure_normalization_ignores_addresses_times_and_temp_paths():
    first = (
        "C:/Temp/chili-fix-one/repo/tests/test_owner.py:12 "
        "object at 0x001122 took 1.2s"
    )
    second = (
        "C:/Temp/chili-fix-two/repo/tests/test_owner.py:99 "
        "object at 0xAABBCC took 9.8s"
    )

    assert validation_contracts.normalize_failure_text(first) == (
        validation_contracts.normalize_failure_text(second)
    )


def test_node_failure_delta_extracts_full_output_diff_and_reference_error():
    evidence = validation_contracts.failure_delta_evidence(
        {
            "runner": "node_test",
            "contract_status": {
                "tests/transform.test.mjs::returns normalized value": "failed",
            },
            "output": (
                "Expected: { total: 5 }\n"
                "Actual: { total: 4 }\n"
                "ReferenceError: cacheKey is not defined\n"
                + "\n".join(f"diagnostic noise {index}" for index in range(400))
            ),
        }
    )

    assert evidence == {
        "failed_ids": [
            "tests/transform.test.mjs::returns normalized value",
        ],
        "facts": [
            "expected: { total: 5 }; actual: { total: 4 }",
            "ReferenceError: cacheKey is not defined",
        ],
    }


def test_python_failure_delta_extracts_assertion_and_name_error():
    evidence = validation_contracts.failure_delta_evidence(
        {
            "step_key": "pytest_targeted",
            "stdout": (
                "tests/test_math.py::test_total FAILED [100%]\n"
                "E       assert 4 == 5\n"
                "E       NameError: name 'subtotal' is not defined\n"
            ),
        }
    )

    assert evidence["failed_ids"] == ["tests/test_math.py::test_total"]
    assert evidence["facts"] == [
        "assertion: 4 == 5",
        "NameError: name 'subtotal' is not defined",
    ]


def test_dart_failure_delta_extracts_bad_state_and_type_error():
    evidence = validation_contracts.failure_delta_evidence(
        {
            "runner": "dart_test",
            "test_contract_status": {
                "test/parser_test.dart::rejects invalid record": "failed",
            },
            "stderr": (
                "Bad state: No element\n"
                "type 'String' is not a subtype of type 'int' in type cast\n"
            ),
        }
    )

    assert evidence["failed_ids"] == [
        "test/parser_test.dart::rejects invalid record",
    ]
    assert evidence["facts"] == [
        "Bad state: No element",
        "TypeError: type 'String' is not a subtype of type 'int' in type cast",
    ]


def test_sql_failure_delta_extracts_no_such_column_operational_error():
    evidence = validation_contracts.failure_delta_evidence(
        {
            "contract_status": {"schema/orders.sql": "error"},
            "stderr": (
                "sqlite3.OperationalError: no such column: orders.legacy_total"
            ),
        }
    )

    assert evidence == {
        "failed_ids": ["schema/orders.sql"],
        "facts": [
            "OperationalError: no such column: orders.legacy_total",
        ],
    }


def test_failure_delta_normalizes_deduplicates_and_bounds_evidence():
    duplicate_errors = "\n".join(
        (
            "ReferenceError: token is not defined at "
            f"C:/Temp/chili-fix-{index}/repo/app.js:{index + 1}:2 "
            f"(object 0x{index + 1000:X}, {index + 1}ms)"
        )
        for index in range(6)
    )
    unique_errors = "\n".join(
        f"TypeError: distinct diagnostic {index} " + ("x" * 80)
        for index in range(6)
    )
    evidence = validation_contracts.failure_delta_evidence(
        {
            "contract_status": {
                f"tests/test_{index}.py::test_contract": "failed"
                for index in range(5)
            },
            "output": duplicate_errors + "\n" + unique_errors,
        },
        max_failed_ids=2,
        max_facts=3,
        max_contract_id_chars=32,
        max_fact_chars=48,
    )

    assert len(evidence["failed_ids"]) == 2
    assert len(evidence["facts"]) == 3
    assert sum(fact.startswith("ReferenceError:") for fact in evidence["facts"]) == 1
    assert all(len(value) <= 32 for value in evidence["failed_ids"])
    assert all(len(value) <= 48 for value in evidence["facts"])
    assert evidence["facts"][0] == (
        "ReferenceError: token is not defined at <path..."
    )
