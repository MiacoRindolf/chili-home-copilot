$out = "scripts/dispatch-r23-pytest-output.txt"
"# r23 pytest + smoke $(Get-Date)" | Out-File $out -Encoding utf8
function S { param([string]$T,[scriptblock]$B); "" | Add-Content $out; "===== $T =====" | Add-Content $out; try { (& $B 2>&1 | Out-String) | Add-Content $out } catch { "ERR $_" | Add-Content $out } }

S "git status -s (modified files)" {
    git status -s
}

S "git diff --stat (touched files)" {
    git diff --stat app/config.py app/services/broker_service.py app/services/trading/venue/robinhood_spot.py app/services/trading/bracket_writer_g2.py app/services/trading/bracket_reconciliation_service.py tests/test_bracket_writer_g2.py
}

S "py-compile gate" {
    conda run -n chili-env python -m py_compile `
        app/config.py `
        app/services/broker_service.py `
        app/services/trading/venue/robinhood_spot.py `
        app/services/trading/bracket_writer_g2.py `
        app/services/trading/bracket_reconciliation_service.py
    if ($LASTEXITCODE -eq 0) { "py-compile OK" } else { "py-compile FAILED ($LASTEXITCODE)" }
}

S "pytest tests/test_bracket_writer_g2.py" {
    $env:TEST_DATABASE_URL = "postgresql://chili:chili@localhost:5433/chili_test"
    conda run -n chili-env python -m pytest tests/test_bracket_writer_g2.py -v
}

S "pytest tests/test_bracket_reconciliation_service.py (if present)" {
    if (Test-Path tests/test_bracket_reconciliation_service.py) {
        $env:TEST_DATABASE_URL = "postgresql://chili:chili@localhost:5433/chili_test"
        conda run -n chili-env python -m pytest tests/test_bracket_reconciliation_service.py -v
    } else {
        "no file"
    }
}

S "import smoke (writer + adapter resolves)" {
    conda run -n chili-env python -c @"
from app.services.trading import bracket_writer_g2 as g2
from app.services.trading.venue.robinhood_spot import RobinhoodSpotAdapter
print('writer module:', g2.__name__)
print('writer __all__:', g2.__all__)
print('place_stop_loss_sell_order on adapter:', hasattr(RobinhoodSpotAdapter, 'place_stop_loss_sell_order'))
import inspect
sig = inspect.signature(RobinhoodSpotAdapter.place_stop_loss_sell_order)
print('signature:', sig)
"@
}

S "config flag visible" {
    conda run -n chili-env python -c @"
from app.config import settings
print('chili_bracket_sweep_writer_enabled:', settings.chili_bracket_sweep_writer_enabled)
print('chili_bracket_writer_g2_enabled:', settings.chili_bracket_writer_g2_enabled)
print('chili_bracket_writer_g2_place_missing_stop:', settings.chili_bracket_writer_g2_place_missing_stop)
print('brain_live_brackets_mode:', settings.brain_live_brackets_mode)
"@
}

S "git push retry" { git push origin main }

Write-Host "done — see $out"
