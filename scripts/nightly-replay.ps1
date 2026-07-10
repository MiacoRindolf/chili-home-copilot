# CHILI nightly replay counterfactual launcher (greenlit #2, 2026-07-10).
# Daily 17:30 PT (tapos na ang extended hours) via CHILI-Nightly-Replay task.
$ErrorActionPreference = 'SilentlyContinue'
& 'C:\Users\rindo\miniconda3\envs\chili-env\python.exe' 'D:\dev\chili-home-copilot\scripts\nightly_replay_report.py' *>> 'D:\CHILI-Docker\chili-data\nightly_replay\runner.log'
