"""Self-distillation pipeline.

Reads llm_call_log → trains local Ollama coder → evaluates → promotes via
ensemble_code_promotion_check (mirrors trading promotion gate).
"""
from .promotion_gate import ensemble_code_promotion_check
from .exporter import build_jsonl_dataset
from .evaluator import run_eval

__all__ = ["ensemble_code_promotion_check", "build_jsonl_dataset", "run_eval"]
