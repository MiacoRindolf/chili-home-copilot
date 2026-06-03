"""Guard tests for the CHILI OS default Space (WS-66).

A saved Space can be marked "default" (star toggle in the Spaces menu). On a
fresh session with no saved layout to restore, the default Space auto-opens.
The default follows a rename and is cleared when its Space is deleted. DB-free
source guards.
"""
import os

_HERE = os.path.dirname(__file__)
_STATIC = os.path.join(_HERE, "..", "app", "static")


def _read(*parts):
    with open(os.path.join(*parts), encoding="utf-8") as f:
        return f.read()


def test_default_space_storage_and_api():
    src = _read(_STATIC, "js", "os.js")
    assert "DEFAULT_KEY = 'chili-os-default-space'" in src, "default-space storage key is gone"
    assert "function getDefaultSpace" in src and "function setDefaultSpace" in src, "default-space helpers are gone"
    assert "getDefault: getDefaultSpace" in src and "setDefault: setDefaultSpace" in src, "ChiliOS.spaces default API is gone"


def test_default_space_auto_opens_on_fresh_load():
    src = _read(_STATIC, "js", "os.js")
    # On load, when nothing was restored, open the default Space.
    assert "var _restored = restoreLayout();" in src
    assert "if (!_restored)" in src and "openSpace(_dn)" in src, "default Space is not opened on a fresh session"


def test_default_follows_rename_and_clears_on_remove():
    src = _read(_STATIC, "js", "os.js")
    assert "if (getDefaultSpace() === name) setDefaultSpace('')" in src, "removing a Space must clear it as default"
    assert "if (getDefaultSpace() === oldName) setDefaultSpace(newName)" in src, "default must follow a rename"


def test_spaces_menu_has_default_star():
    src = _read(_STATIC, "js", "workspace.js")
    assert "ws-space-star" in src, "the default-Space star button is gone from the Spaces menu"
    assert "api.setDefault" in src, "the star no longer toggles the default Space"
