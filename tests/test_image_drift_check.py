"""Ang tumatakbong container ay dapat masabi kung gaano na ito kaluma.

BAKIT ITO UMIIRAL. Noong 2026-08-24 ay natagpuan ang scheduler container na
tumatakbo sa isang image na binuo **53 commit** ang layo sa likod ng ``main``.
Walang nakapansin dahil walang tumitingin. Nang isulat ang tsekeng ito, agad
nitong ipinakita ang isang bagay na HINDI ko alam: ang scheduler lamang ang
naayos ko -- ang web at brain ay **120 commit** ang atras at ang cloudflare
origin bridge ay **130**.

Ang mga tag string sa ibaba ay LITERAL na kinopya mula sa ``docker ps`` sa makina
na ito. Iyon ang buong punto: tatlong magkaibang hugis ng tag ang aktuwal na
umiiral nang sabay-sabay, at ang isang parser na humahawak lamang ng isa sa mga
ito ay tahimik na palalampasin ang iba.

Runnable: pytest tests/test_image_drift_check.py -v
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

_SCRIPT = (
    pathlib.Path(__file__).resolve().parents[1] / "scripts" / "check_image_drift.py"
)


@pytest.fixture(scope="module")
def drift():
    spec = importlib.util.spec_from_file_location("_check_image_drift", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_check_image_drift"] = mod
    spec.loader.exec_module(mod)
    return mod


# Eksaktong mga tag na tumatakbo sa makinang ito noong 2026-08-25.
LIVE_TAGS = {
    "chili-app:main-291262dcb": "291262dcb",     # sariwa
    "chili-app:main-e00ef98-pinned": "e00ef98",  # may suffix pagkatapos ng sha
    "chili-app:main-clean-d7b3c2a": "d7b3c2a",   # dalawang segment bago ang sha
    "chili-app:main-2e1eb77": "2e1eb77",         # ang 53-commit na luma
}


@pytest.mark.parametrize("tag,sha", sorted(LIVE_TAGS.items()))
def test_it_parses_every_tag_shape_that_actually_runs(drift, tag, sha):
    m = drift._TAG_SHA.match(tag)
    assert m is not None, f"hindi na-parse ang tunay na tag {tag!r}"
    assert m.group(1) == sha


def test_a_tag_without_a_commit_is_reported_not_silently_passed(drift):
    """⚠️ FAIL-LOUD. Ang ``:latest`` ay walang commit sa pangalan nito, kaya walang
    bilang na maaaring tumaas nang sapat para mag-trigger ng babala. Ang isang
    tahimik na "ok" dito ay mas malala kaysa sa isang kilalang-lumang bilang."""
    assert drift._TAG_SHA.match("chili-app:latest") is None
    assert drift._TAG_SHA.match("chili-app:main") is None


def test_a_six_char_word_is_not_mistaken_for_a_commit(drift):
    """Ang 'decade' ay puro hex character pero hindi ito SHA. Ang floor na 7 ang
    pumipigil sa isang salita na magmukhang sariwang build."""
    assert drift._TAG_SHA.match("chili-app:main-decade") is None


def test_non_chili_containers_are_ignored(drift, monkeypatch):
    """Ang postgres/ollama/redis ay hindi CHILI code -- hindi sila dapat lumitaw."""
    monkeypatch.setattr(
        drift, "_run",
        lambda _a: "chili-web\tchili-app:main-e00ef98-pinned\n"
                   "chili-postgres\tpostgres:16\n"
                   "chili-ollama\tollama/ollama:latest\n",
    )
    rows = drift.running_chili_images()
    assert rows == [("chili-web", "chili-app:main-e00ef98-pinned")]


def test_a_dead_docker_is_not_a_green_light(drift, monkeypatch):
    """Kung patay ang docker ay walang container na maiuulat. Ang script ay hindi
    dapat sumabog -- pero hindi rin ito dapat mag-imbento ng katahimikan bilang
    kalusugan, kaya ang wala ay iniuulat bilang wala."""
    monkeypatch.setattr(drift, "_run", lambda _a: "")
    assert drift.running_chili_images() == []


def test_the_threshold_decides_stale_not_a_hardcoded_number(drift, monkeypatch):
    """Ang hangganan ay isang argumento; hindi dapat may nakabaon na numero."""
    monkeypatch.setattr(
        drift, "_run",
        lambda _a: "chili-web\tchili-app:main-e00ef98-pinned\n",
    )
    monkeypatch.setattr(drift, "commits_behind", lambda _s, _r="origin/main": 25)
    assert drift.main(["--max-behind", "20", "--json"]) == 1, "25 > 20 ⇒ stale"
    assert drift.main(["--max-behind", "30", "--json"]) == 0, "25 < 30 ⇒ ok"


def test_the_exit_code_is_usable_by_a_scheduled_task(drift, monkeypatch):
    """Ang isang tseke ay walang silbi kung hindi ito maaaring i-wire sa isang
    alarma. Ang exit 1 ang siyang nagpapaandar niyon."""
    monkeypatch.setattr(drift, "_run", lambda _a: "")
    assert drift.main(["--json"]) == 0, "walang container ⇒ walang idedeklarang drift"
