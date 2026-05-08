"""
LLM client — thin wrapper for text generation.

Functions:
  generate_text(prompt, model) — sends a prompt to an LLM and returns the text response
"""

import logging
import os

from google import genai

logger = logging.getLogger(__name__)

# Model tiers — pick based on task complexity. Caller selects explicitly.
# Verify current model IDs with: client.models.list()
# 2.0 series sunsets June 2026 — prefer 2.5.
MODEL_LITE = "gemini-2.5-flash-lite"   # cheapest — intent classification, simple extraction
MODEL_FLASH = "gemini-2.5-flash"        # balanced — general reasoning, summaries, vision
MODEL_PRO = "gemini-2.5-pro"            # most capable — complex reasoning, ambiguous inputs


# Sends a prompt to the specified LLM and returns the stripped text response.
# Inputs: prompt string, model name (default: MODEL_FAST).
# Outputs: stripped response string. Raises on API error.
def generate_text(prompt: str, model: str = MODEL_LITE) -> str:
    client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY", "").strip() or None)
    response = client.models.generate_content(model=model, contents=prompt)
    logger.debug("llm model=%s", model)
    return response.text.strip()
