"""Standalone verifier (no docker exec, runs locally on host with conda)."""
import subprocess
import sys
import textwrap

PY_CHILI = textwrap.dedent("""
    from app.config import settings
    print('database_url set:', bool(getattr(settings, 'database_url', None)))
    print('coinbase_api_key set:', bool(settings.coinbase_api_key))
    print('coinbase_api_secret set:', bool(settings.coinbase_api_secret))
    print('chili_autotrader_enabled:', settings.chili_autotrader_enabled)
    print('chili_robinhood_spot_adapter_enabled:', settings.chili_robinhood_spot_adapter_enabled)
    print('chili_coinbase_autotrader_live:', settings.chili_coinbase_autotrader_live)
    print('chili_autotrader_kill_switch:', settings.chili_autotrader_kill_switch)
    print('chili_coinbase_max_notional_usd:', settings.chili_coinbase_max_notional_usd)
    print('chili_coinbase_max_concurrent_positions:', settings.chili_coinbase_max_concurrent_positions)
    print('chili_coinbase_taker_fee_bps_round_trip:', settings.chili_coinbase_taker_fee_bps_round_trip)
    print('chili_min_edge_safety_buffer_bps:', settings.chili_min_edge_safety_buffer_bps)
    print('chili_autotrader_user_id:', getattr(settings, 'chili_autotrader_user_id', '<missing>'))
    print('chili_autotrader_min_projected_profit_pct:', getattr(settings, 'chili_autotrader_min_projected_profit_pct', '<missing>'))
    print('pattern_imminent_min_readiness:', getattr(settings, 'pattern_imminent_min_readiness', '<missing>'))
    print('chili_fast_path_universe_min_volume_24h_usd:', getattr(settings, 'chili_fast_path_universe_min_volume_24h_usd', '<missing>'))
    print('chili_autotrader_max_concurrent:', getattr(settings, 'chili_autotrader_max_concurrent', '<missing>'))
    print('brain_macro_regime_cron_hour:', getattr(settings, 'brain_macro_regime_cron_hour', '<missing>'))
    print('chili_pattern_survival_sizing_enabled:', getattr(settings, 'chili_pattern_survival_sizing_enabled', '<missing>'))
    print('chili_pattern_survival_classifier_enabled:', getattr(settings, 'chili_pattern_survival_classifier_enabled', '<missing>'))
    print('brain_pattern_regime_autopilot_enabled:', getattr(settings, 'brain_pattern_regime_autopilot_enabled', '<missing>'))
    print('chili_dispatch_git_push_enabled:', getattr(settings, 'chili_dispatch_git_push_enabled', '<missing>'))
    print()
    from app.services import coinbase_service as cb
    p = cb.get_portfolio()
    if isinstance(p, dict):
        print('cash:', p.get('cash'))
        print('equity:', p.get('equity'))
        print('buying_power:', p.get('buying_power'))
    print()
    from sqlalchemy import text
    from app.db import SessionLocal
    db = SessionLocal()
    try:
        rows = db.execute(text("SELECT count(*) FROM trading_autotrader_runs WHERE created_at >= now() - interval '5 minutes'")).fetchall()
        print('autotrader_runs last 5min:', rows[0][0])
        db.rollback()
        rows = db.execute(text("SELECT count(*) FROM trading_alerts WHERE created_at >= now() - interval '5 minutes'")).fetchall()
        print('trading_alerts last 5min:', rows[0][0])
        db.rollback()
    finally:
        db.close()
""").strip()


def main() -> int:
    # Write probe to a temp file
    from pathlib import Path
    probe_file = Path("scripts/_env_verify_probe.py")
    probe_file.write_text(PY_CHILI, encoding="utf-8")

    # Copy probe into chili container then exec
    print("# A: container statuses")
    sys.stdout.flush()
    subprocess.run(
        ["docker", "ps", "--filter", "name=chili-home-copilot",
         "--format", "{{.Names}}: {{.Status}}"],
        check=False
    )

    print("# B: line count of .env")
    env_bytes = Path(".env").read_bytes()
    lines = env_bytes.count(b"\n") + 1
    print(f"  bytes={len(env_bytes)} lines={lines}")

    print("# C: pydantic settings + portfolio + activity (chili-1)")
    sys.stdout.flush()
    subprocess.run(
        ["docker", "cp", "scripts/_env_verify_probe.py",
         "chili-home-copilot-chili-1:/app/_env_verify_probe.py"],
        check=False
    )
    subprocess.run(
        ["docker", "exec", "-w", "/app", "chili-home-copilot-chili-1",
         "python", "/app/_env_verify_probe.py"],
        check=False
    )
    subprocess.run(
        ["docker", "exec", "chili-home-copilot-chili-1",
         "rm", "-f", "/app/_env_verify_probe.py"],
        check=False
    )

    probe_file.unlink(missing_ok=True)
    print("# end")
    return 0


if __name__ == "__main__":
    sys.exit(main())
