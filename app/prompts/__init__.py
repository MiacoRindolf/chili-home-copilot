"""Load prompt text from app/prompts/*.txt. Used by openai_client, llm_planner, vision, chat_service."""
from pathlib import Path

_PROMPTS_DIR = Path(__file__).resolve().parent


def load_prompt(name: str) -> str:
    """Load a prompt by name (e.g. 'system_base') from app/prompts/{name}.txt."""
    path = _PROMPTS_DIR / f"{name}.txt"
    if not path.is_file():
        raise FileNotFoundError(f"Prompt file not found: {path}")
    return path.read_text(encoding="utf-8").strip()
