"""
LLM client — thin wrappers for text generation and audio transcription.

Functions:
  generate_text(prompt, model)                   — sends a text prompt and returns the response
  transcribe_audio(audio_bytes, mime_type, model) — transcribes a voice message via Gemini
"""

import logging
import os

from google import genai
from google.genai import types

logger = logging.getLogger(__name__)

# Model tiers — pick based on task complexity. Caller selects explicitly.
# Verify current model IDs with: client.models.list()
# 2.0 series sunsets June 2026 — prefer 2.5.
MODEL_LITE = "gemini-2.5-flash-lite"   # cheapest — intent classification, simple extraction
MODEL_FLASH = "gemini-2.5-flash"        # balanced — general reasoning, summaries, vision
MODEL_PRO = "gemini-2.5-pro"            # most capable — complex reasoning, ambiguous inputs


_gemini: genai.Client | None = None


def _get_client() -> genai.Client:
    # Singleton — created once per process. Avoids GC-ing the client mid-call.
    global _gemini
    if _gemini is None:
        _gemini = genai.Client(api_key=os.environ.get("GEMINI_API_KEY", "").strip() or None)
    return _gemini


# Sends a prompt to the specified LLM and returns the stripped text response.
# Inputs: prompt string, model name (default: MODEL_LITE).
# Outputs: stripped response string. Raises on API error.
def generate_text(prompt: str, model: str = MODEL_LITE) -> str:
    response = _get_client().models.generate_content(model=model, contents=prompt)
    logger.debug("llm model=%s", model)
    return response.text.strip()


# Transcribes a voice message using Gemini's multimodal input.
# Inputs: raw audio bytes, MIME type (default: audio/ogg for Telegram voice messages), model.
# Outputs: transcribed text string. Raises on API error.
def transcribe_audio(
    audio_bytes: bytes,
    mime_type: str = "audio/ogg",
    model: str = MODEL_FLASH,
) -> str:
    response = _get_client().models.generate_content(
        model=model,
        contents=[
            types.Part(inline_data=types.Blob(data=audio_bytes, mime_type=mime_type)),
            types.Part(text=(
                "Transcribe this voice message exactly as spoken. "
                "The speaker is Singaporean and may speak in Singaporean English (Singlish), "
                "or mix in Hokkien, Mandarin, Cantonese, Malay, or Thai words. "
                "Preserve the words as spoken — do not translate or normalise to standard English. "
                "Return only the transcription, nothing else."
            )),
        ],
    )
    logger.debug("transcribe model=%s bytes=%d", model, len(audio_bytes))
    return response.text.strip()
