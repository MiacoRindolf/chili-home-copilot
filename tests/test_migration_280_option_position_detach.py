from app import migrations


class _Result:
    rowcount = 0


class _FakeConn:
    def __init__(self):
        self.executed_sql = []
        self.commit_count = 0

    def execute(self, statement, *_args, **_kwargs):
        self.executed_sql.append(str(statement))
        return _Result()

    def commit(self):
        self.commit_count += 1


def test_migration_280_registered_between_279_and_281():
    ids = [version_id for version_id, _fn in migrations.MIGRATIONS]

    assert "280_option_trade_position_identity_detach" in ids
    assert ids.index("279_project_autonomy_architect_reviews") < ids.index(
        "280_option_trade_position_identity_detach"
    )
    assert ids.index("280_option_trade_position_identity_detach") < ids.index(
        "281_llm_cost_observability"
    )


def test_migration_280_detaches_option_envelopes_from_non_option_positions(
    monkeypatch,
):
    monkeypatch.setattr(
        migrations,
        "_tables",
        lambda _conn: {"trading_trades", "trading_positions"},
    )
    monkeypatch.setattr(
        migrations,
        "_columns",
        lambda _conn, table: {"position_id", "asset_kind"}
        if table == "trading_trades"
        else set(),
    )
    conn = _FakeConn()

    migrations._migration_280_option_trade_position_identity_detach(conn)

    update_sql = "\n".join(conn.executed_sql)
    assert "SET position_id = NULL" in update_sql
    assert "LOWER(COALESCE(t.asset_kind, '')) IN ('option', 'options')" in update_sql
    assert (
        "LOWER(COALESCE(p.asset_kind, '')) NOT IN ('option', 'options')"
        in update_sql
    )
    assert conn.commit_count == 1
