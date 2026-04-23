import ast
for p in [
    "app/services/trading/robinhood_exit_execution.py",
    "app/services/trading/stop_engine.py",
]:
    try:
        ast.parse(open(p).read())
        print(f"OK  {p}")
    except SyntaxError as e:
        print(f"FAIL {p}: {e}")
