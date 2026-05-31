from app.services.code_brain import repo_resolver


def test_configured_host_roots_use_precompiled_patterns(monkeypatch):
    monkeypatch.setenv("CHILI_HOST_DEV_ROOTS", "D:/dev;C:/dev,E:/work;not-a-drive")

    def fail_split(*_args, **_kwargs):
        raise AssertionError("_configured_host_dev_roots should not call module-level re.split")

    def fail_match(*_args, **_kwargs):
        raise AssertionError("_configured_host_dev_roots should not call module-level re.match")

    monkeypatch.setattr(repo_resolver.re, "split", fail_split)
    monkeypatch.setattr(repo_resolver.re, "match", fail_match)

    assert repo_resolver._configured_host_dev_roots() == ["d:/dev", "c:/dev", "e:/work"]


def test_windows_to_container_path_uses_precompiled_pattern(monkeypatch):
    monkeypatch.setenv("CHILI_HOST_DEV_ROOTS", "D:/dev")

    def fail_match(*_args, **_kwargs):
        raise AssertionError("_windows_to_container_path should not call module-level re.match")

    monkeypatch.setattr(repo_resolver.re, "match", fail_match)

    assert repo_resolver._windows_to_container_path(r"D:\dev\project") == "/host_dev/project"


def test_parse_input_bare_repo_name_uses_precompiled_pattern(monkeypatch):
    def fail_match(*_args, **_kwargs):
        raise AssertionError("parse_input should not call module-level re.match")

    monkeypatch.setattr(repo_resolver.re, "match", fail_match)

    parsed = repo_resolver.parse_input("chili-home-copilot")

    assert parsed.kind is repo_resolver.InputKind.REPO_NAME
    assert parsed.repo_name == "chili-home-copilot"


def test_clone_token_helpers_use_precompiled_patterns(monkeypatch, tmp_path):
    monkeypatch.setenv("CHILI_DISPATCH_GITHUB_TOKEN", "secret-token")

    calls: list[list[str]] = []

    class _Proc:
        returncode = 1
        stderr = "fatal: https://x-access-token:secret-token@github.com/acme/repo.git failed"
        stdout = ""

    def fake_run(args, **_kwargs):
        calls.append(list(args))
        return _Proc()

    def fail_sub(*_args, **_kwargs):
        raise AssertionError("_clone_with_pat should not call module-level re.sub")

    monkeypatch.setattr(repo_resolver.subprocess, "run", fake_run)
    monkeypatch.setattr(repo_resolver.re, "sub", fail_sub)

    ok, message = repo_resolver._clone_with_pat(
        "https://github.com/acme/repo.git",
        str(tmp_path / "repo"),
    )

    assert ok is False
    assert "secret-token" not in message
    assert "x-access-token:***@" in message
    assert calls[0][2].startswith("https://x-access-token:secret-token@")
