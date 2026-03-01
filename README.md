# 🌶️ CHILI Home Copilot
**CHILI = Conversational Home Interface & Life Intelligence**

Local-first household copilot (text-first) with a minimal web UI.

## Day 1 Features
- FastAPI local web UI
- SQLite storage (SQLAlchemy)
- Chores: add + mark done
- Birthday reminders: add + list

## Run locally
```bash
conda activate chili-env
uvicorn app.main:app --reload