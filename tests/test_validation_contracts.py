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
