$out = "scripts/dispatch-r23-r24-validate-output.txt"
"# r23 + r24 validate $(Get-Date)" | Out-File $out -Encoding utf8
function S { param([string]$T,[scriptblock]$B); "" | Add-Content $out; "===== $T =====" | Add-Content $out; try { (& $B 2>&1 | Out-String) | Add-Content $out } catch { "ERR $_" | Add-Content $out } }

S "git status -s (touched files only)" {
    git status -s | Where-Object { $_ -match 'app/(config|migrations|services/(broker_service|trading/(venue/robinhood_spot|bracket_writer_g2|bracket_reconciliation_service)))\.py|tests/test_bracket_writer_g2|scripts/_(fix_rh_spot|rewrite_bracket_writer_g2|wire_g2_into_sweep)|scripts/dispatch-r23' }
}

S "py-compile gate (R23 + R24 touched files)" {
    conda run -n chili-env python -m py_compile `
        app/config.py `
        app/migrations.py `
        app/services/broker_service.py `
        app/services/trading/venue/robinhood_spot.py `
        app/services/trading/bracket_writer_g2.py `
        app/services/trading/bracket_reconciliation_service.py
    if ($LASTEXITCODE -eq 0) { "py-compile OK" } else { "py-compile FAILED ($LASTEXITCODE)" }
}

S "verify migration ids unique" {
    if (Test-Path .\scripts\verify-migration-ids.ps1) {
        .\scripts\verify-migration-ids.ps1
    } else {
        conda run -n chili-env python -c @"
import re
from pathlib import Path
src = Path('app/migrations.py').read_text(encoding='utf-8')
ids = re.findall(r'\("(\d+)_[a-z0-9_]+",', src)
dup = [x for x in ids if ids.count(x) > 1]
print('count:', len(ids), 'unique:', len(set(ids)), 'dups:', sorted(set(dup)))
print('last 5:', ids[-5:])
"@
    }
}

S "import smoke (writer + adapter resolves)" {
    conda run -n chili-env python -c @"
from app.services.trading import bracket_writer_g2 as g2
from app.services.trading.venue.robinhood_spot import RobinhoodSpotAdapter
import inspect
print('writer __all__:', g2.__all__)
print('place_stop_loss_sell_order on adapter:', hasattr(RobinhoodSpotAdapter, 'place_stop_loss_sell_order'))
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

S "pytest tests/test_bracket_writer_g2.py" {
    $env:TEST_DATABASE_URL = "postgresql://chili:chili@localhost:5433/chili_test"
    conda run -n chili-env python -m pytest tests/test_bracket_writer_g2.py -v --no-header 2>&1
}

S "git diff stat (touched files)" {
    git diff --stat HEAD -- app/config.py app/migrations.py app/services/broker_service.py app/services/trading/venue/robinhood_spot.py app/services/trading/bracket_writer_g2.py app/services/trading/bracket_reconciliation_service.py tests/test_bracket_writer_g2.py
}

Write-Host "validate complete -- see $out"
