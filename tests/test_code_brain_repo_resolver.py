from app.services.code_brain import repo_resolver


def test_windows_host_path_defaults_to_d_dev_mount(monkeypatch):
    monkeypatch.delenv("CHILI_HOST_DEV_ROOTS", raising=False)
    parsed = repo_resolver.parse_input(r"D:\dev\chili-home-copilot")

    assert parsed.container_path == "/host_dev/chili-home-copilot"


def test_windows_host_path_rejects_legacy_c_dev_by_default(monkeypatch):
    monkeypatch.delenv("CHILI_HOST_DEV_ROOTS", raising=False)
    parsed = repo_resolver.parse_input(r"C:\dev\chili-home-copilot")

    assert parsed.container_path is None


def test_windows_host_path_can_enable_legacy_c_dev_alias(monkeypatch):
    monkeypatch.setenv("CHILI_HOST_DEV_ROOTS", "D:/dev;C:/dev")
    parsed = repo_resolver.parse_input(r"C:\dev\chili-home-copilot")

    assert parsed.container_path == "/host_dev/chili-home-copilot"


def test_windows_host_path_rejects_unmounted_drive(monkeypatch):
    monkeypatch.setenv("CHILI_HOST_DEV_ROOTS", "D:/dev")
    parsed = repo_resolver.parse_input(r"E:\dev\some-project")

    assert parsed.container_path is None
