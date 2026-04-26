"""Out-of-process fine-tune trigger.

We do not train inside the FastAPI / scheduler process. Instead we shell
out to a sidecar script (default: scripts/dispatch/finetune.ps1) that runs
Unsloth or Axolotl on the operator's GPU, produces a LoRA adapter, builds
an Ollama Modelfile, and tags the resulting model as
chili-coder:<timestamp>. The promotion gate decides whether that tag
becomes :current.
"""
from __future__ import annotations

import logging
import os
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def trigger_finetune(
    *,
    dataset_jsonl: str,
    base_model: str = "qwen2.5-coder:3b-instruct-q8_0",
) -> Optional[str]:
    """Run the sidecar trainer. Returns the resulting Ollama tag, or None on failure.

    Trainer script contract:
      input:  dataset path, base model
      output: prints exactly one line to stdout: 'TAG=<ollama-tag>'
    """
    script = os.environ.get(
        "CHILI_DISPATCH_FINETUNE_SCRIPT",
        str(Path("scripts") / "dispatch" / "finetune.ps1"),
    )
    if not Path(script).exists():
        logger.warning("[distillation.trainer] sidecar script %s not found; skipping", script)
        return None

    candidate_tag = f"chili-coder:{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}"
    cmd = [
        "pwsh", "-File", script,
        "-Dataset", dataset_jsonl,
        "-BaseModel", base_model,
        "-OutputTag", candidate_tag,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60 * 60 * 4)
    except Exception:
        logger.warning("[distillation.trainer] sidecar invocation failed", exc_info=True)
        return None
    if proc.returncode != 0:
        logger.warning("[distillation.trainer] non-zero exit %s\nstderr=%s", proc.returncode, proc.stderr[-2000:])
        return None
    for line in proc.stdout.splitlines():
        if line.startswith("TAG="):
            return line.split("=", 1)[1].strip()
    return None
