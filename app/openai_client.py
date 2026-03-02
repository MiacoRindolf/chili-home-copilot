"""OpenAI API client for CHILI's general chat fallback.

When the local llama3 planner returns type=unknown (can't map to a tool action),
this module provides a full conversational response via OpenAI's API.
"""
import os
from openai import OpenAI
from dotenv import load_dotenv

from .logger import log_info

load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

SYSTEM_PROMPT = """You are CHILI (Conversational Home Interface & Life Intelligence), a friendly household assistant for a shared living space.

Your personality:
- Warm, approachable, and slightly witty -- like a helpful housemate who's really good at Google
- Use the housemate's name when you know it
- Reference household context naturally (chores, house rules, recipes) when relevant
- Keep responses clear and well-formatted -- use markdown: headers, bullet points, code blocks when appropriate
- Be concise for simple questions, thorough for complex ones
- If you know the housemate's preferences (dietary, interests, tone), adapt your responses accordingly

You are NOT a generic AI chatbot. You are CHILI, the household's personal assistant. When a housemate asks you anything -- from cooking tips to coding help to life advice -- answer as CHILI would: knowledgeable, personalized, and grounded in the household context you have."""


def is_configured() -> bool:
    return bool(OPENAI_API_KEY and OPENAI_API_KEY.strip())


def chat(
    messages: list[dict],
    system_prompt: str | None = None,
    trace_id: str = "openai",
) -> dict:
    """Send a conversation to OpenAI and return the response.

    Args:
        messages: list of {"role": "user"|"assistant", "content": str}
        system_prompt: override the default system prompt
        trace_id: for structured logging

    Returns:
        {"reply": str, "tokens_used": int, "model": str}
        On failure: {"reply": str, "tokens_used": 0, "model": "error"}
    """
    if not is_configured():
        return {
            "reply": "",
            "tokens_used": 0,
            "model": "none",
        }

    try:
        client = OpenAI(api_key=OPENAI_API_KEY)

        api_messages = [
            {"role": "system", "content": system_prompt or SYSTEM_PROMPT},
        ]
        api_messages.extend(messages)

        response = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=api_messages,
            temperature=0.7,
            max_completion_tokens=1024,
        )

        reply = response.choices[0].message.content.strip()
        tokens = response.usage.total_tokens if response.usage else 0

        log_info(trace_id, f"openai_reply model={OPENAI_MODEL} tokens={tokens}")

        return {
            "reply": reply,
            "tokens_used": tokens,
            "model": OPENAI_MODEL,
        }

    except Exception as e:
        log_info(trace_id, f"openai_error={e}")
        return {
            "reply": "",
            "tokens_used": 0,
            "model": "error",
        }


def chat_stream(
    messages: list[dict],
    system_prompt: str | None = None,
    trace_id: str = "openai-stream",
):
    """Stream a conversation response from OpenAI, yielding token deltas."""
    if not is_configured():
        return

    try:
        client = OpenAI(api_key=OPENAI_API_KEY)

        api_messages = [
            {"role": "system", "content": system_prompt or SYSTEM_PROMPT},
        ]
        api_messages.extend(messages)

        stream = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=api_messages,
            temperature=0.7,
            max_completion_tokens=1024,
            stream=True,
        )

        for chunk in stream:
            delta = chunk.choices[0].delta if chunk.choices else None
            if delta and delta.content:
                yield delta.content

        log_info(trace_id, f"openai_stream_complete model={OPENAI_MODEL}")

    except Exception as e:
        log_info(trace_id, f"openai_stream_error={e}")
