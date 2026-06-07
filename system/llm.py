"""
LLM client — thin wrappers for text generation and audio transcription.

Functions:
  generate_with_image(image_bytes, prompt, mime_type, model) — sends image+text prompt and returns the response
  generate_with_images(images, prompt, mime_type, model)     — sends multiple images + text prompt (one call)
  generate_text(prompt, model)                   — sends a text prompt and returns the response
  generate_json(prompt, model)                   — like generate_text but forces JSON output mode (disables thinking)
  transcribe_audio(audio_bytes, mime_type, model) — transcribes a voice message via Gemini

Internal helpers:
  _is_retryable(exc)           — true when the exception is a transient 503/429 error worth retrying
  _call_with_retry(fn, ...)    — wraps a Gemini API call with up to _RETRY_ATTEMPTS retries on transient errors
  _get_client()                — returns the singleton Gemini client (created once per process)
  _extract_text(response)      — extracts and strips text from a GenerateContentResponse
"""

import logging
import os
import time

from google import genai
from google.genai import types

from system.logging import log_event, log_failure

logger = logging.getLogger(__name__)

# Model selection — caller always specifies explicitly; no defaults rely on tier ordering.
# Verify current model IDs with: client.models.list()
# 2.0 series sunsets June 2026 — prefer 2.5.
MODEL_FLASH = "gemini-2.5-flash"        # balanced — general reasoning, summaries, vision
MODEL_PRO = "gemini-2.5-pro"            # most capable — complex reasoning, ambiguous inputs


_gemini: genai.Client | None = None

# Retry config for transient Gemini errors (503 overload, 429 rate limit).
_RETRY_ATTEMPTS = 3
_RETRY_BACKOFF = (1.0, 3.0)  # seconds to sleep before attempt 2, 3


# Returns True when the exception is a transient Gemini error worth retrying (503 overload, 429 rate limit).
# Checked by string match since the Gemini SDK wraps errors in various exception types.
def _is_retryable(exc: Exception) -> bool:
    msg = str(exc)
    return "503" in msg or "UNAVAILABLE" in msg or "429" in msg or "RESOURCE_EXHAUSTED" in msg


# Calls fn(*args, **kwargs) and retries up to _RETRY_ATTEMPTS times on transient errors.
# Sleeps _RETRY_BACKOFF seconds between attempts. Raises the last exception if all attempts fail.
def _call_with_retry(fn, *args, **kwargs):
    last_exc = None
    for attempt in range(_RETRY_ATTEMPTS):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            if _is_retryable(e) and attempt < _RETRY_ATTEMPTS - 1:
                delay = _RETRY_BACKOFF[attempt] if attempt < len(_RETRY_BACKOFF) else _RETRY_BACKOFF[-1]
                log_event(logger, logging.WARNING, "llm_transient_error_retrying",
                          attempt=attempt + 1, delay_s=delay,
                          model=kwargs.get("model"))
                time.sleep(delay)
                last_exc = e
            else:
                raise
    raise last_exc  # unreachable but satisfies type checker


def _get_client() -> genai.Client:
    # Singleton — created once per process. Avoids GC-ing the client mid-call.
    global _gemini
    if _gemini is None:
        _gemini = genai.Client(api_key=os.environ.get("GEMINI_API_KEY", "").strip() or None)
        log_event(logger, logging.INFO, "llm_client_created")
    return _gemini


# Sends a prompt to the specified LLM and returns the stripped text response.
# Inputs: image bytes, prompt string, MIME type, model name (default: MODEL_FLASH).
# Outputs: stripped response string. Raises on API error.
def generate_with_image(
    image_bytes: bytes,
    prompt: str,
    mime_type: str = "image/jpeg",
    model: str = MODEL_FLASH,
) -> str:
    """Sends an image + text prompt to Gemini and returns the response.

    Inputs: raw image bytes, prompt string, MIME type, model.
    Outputs: stripped response string. Raises on API error.
    """
    log_event(
        logger,
        logging.INFO,
        "llm_generate_with_image_started",
        model=model,
        image_bytes=len(image_bytes),
        mime_type=mime_type,
        prompt_chars=len(prompt),
    )
    try:
        response = _call_with_retry(
            _get_client().models.generate_content,
            model=model,
            contents=[
                types.Part(inline_data=types.Blob(data=image_bytes, mime_type=mime_type)),
                types.Part(text=prompt),
            ],
        )
        result = _extract_text(response)
        log_event(
            logger,
            logging.INFO,
            "llm_generate_with_image_completed",
            model=model,
            response_chars=len(result),
        )
        return result
    except Exception as e:
        log_failure(
            logger,
            logging.ERROR,
            "llm_generate_with_image_failed",
            e,
            model=model,
            image_bytes=len(image_bytes),
            mime_type=mime_type,
            prompt_chars=len(prompt),
        )
        raise


# Sends multiple images plus one text prompt to the LLM and returns the stripped text response.
# Inputs: list of image byte blobs, prompt string, MIME type (applied to all), model.
# Outputs: stripped response string. Raises on API error.
# Used by the expense domain when several photos describe one transaction (receipt + payment
# screenshot), so the model can cross-reference them in a single call.
def generate_with_images(
    images: list[bytes],
    prompt: str,
    mime_type: str = "image/jpeg",
    model: str = MODEL_FLASH,
) -> str:
    log_event(
        logger,
        logging.INFO,
        "llm_generate_with_images_started",
        model=model,
        image_count=len(images),
        total_bytes=sum(len(b) for b in images),
        prompt_chars=len(prompt),
    )
    try:
        parts = [types.Part(inline_data=types.Blob(data=b, mime_type=mime_type)) for b in images]
        parts.append(types.Part(text=prompt))
        response = _call_with_retry(
            _get_client().models.generate_content,
            model=model,
            contents=parts,
        )
        result = _extract_text(response)
        log_event(
            logger,
            logging.INFO,
            "llm_generate_with_images_completed",
            model=model,
            image_count=len(images),
            response_chars=len(result),
        )
        return result
    except Exception as e:
        log_failure(
            logger,
            logging.ERROR,
            "llm_generate_with_images_failed",
            e,
            model=model,
            image_count=len(images),
        )
        raise


# Sends a text prompt to the specified LLM and returns the stripped text response.
# Inputs: prompt string, model name (default: MODEL_FLASH).
# Outputs: stripped response string. Raises on API error.
def generate_text(prompt: str, model: str = MODEL_FLASH) -> str:
    log_event(
        logger,
        logging.INFO,
        "llm_generate_text_started",
        model=model,
        prompt_chars=len(prompt),
    )
    try:
        response = _call_with_retry(
            _get_client().models.generate_content, model=model, contents=prompt
        )
        result = _extract_text(response)
        log_event(
            logger,
            logging.INFO,
            "llm_generate_text_completed",
            model=model,
            response_chars=len(result),
        )
        return result
    except Exception as e:
        log_failure(
            logger,
            logging.ERROR,
            "llm_generate_text_failed",
            e,
            model=model,
            prompt_chars=len(prompt),
        )
        raise


# Like generate_text but forces JSON output mode and disables thinking tokens.
# Use this for prompts that return structured JSON — prevents Gemini 2.5 Flash from
# consuming all tokens in thinking and returning empty text.
def generate_json(prompt: str, model: str = MODEL_FLASH) -> str:
    log_event(
        logger,
        logging.INFO,
        "llm_generate_json_started",
        model=model,
        prompt_chars=len(prompt),
    )
    try:
        response = _call_with_retry(
            _get_client().models.generate_content,
            model=model,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                thinking_config=types.ThinkingConfig(thinking_budget=0),
            ),
        )
        result = _extract_text(response)
        log_event(
            logger,
            logging.INFO,
            "llm_generate_json_completed",
            model=model,
            response_chars=len(result),
        )
        return result
    except Exception as e:
        log_failure(
            logger,
            logging.ERROR,
            "llm_generate_json_failed",
            e,
            model=model,
            prompt_chars=len(prompt),
        )
        raise


# Transcribes a voice message using Gemini's multimodal input.
# Inputs: raw audio bytes, MIME type (default: audio/ogg for Telegram voice messages), model.
# Outputs: transcribed text string. Raises on API error.
def transcribe_audio(
    audio_bytes: bytes,
    mime_type: str = "audio/ogg",
    model: str = MODEL_FLASH,
) -> str:
    log_event(
        logger,
        logging.INFO,
        "llm_transcribe_audio_started",
        model=model,
        audio_bytes=len(audio_bytes),
        mime_type=mime_type,
    )
    try:
        response = _call_with_retry(
            _get_client().models.generate_content,
            model=model,
            contents=[
                types.Part(inline_data=types.Blob(data=audio_bytes, mime_type=mime_type)),
                types.Part(text=(
                    "Transcribe this voice message exactly as spoken. "
                    "The speaker is Singaporean and may speak in Singaporean English (Singlish), "
                    "or mix in Hokkien, Mandarin, Cantonese, Malay, or Thai words. "
                    "Preserve the words as spoken — do not translate or normalise to standard English. "
                    "Context: the speaker logs personal health data by voice. Common messages include "
                    "sleep/wake phrases (e.g. 'night night', 'good night', 'going to sleep', "
                    "'woke up', 'good morning', 'wakey wakey', 'rise and shine') and weight numbers "
                    "(e.g. '57.2', '63 kg'). Prefer sleep/wake phrase interpretations for greetings "
                    "and bedtime expressions over digit sequences. They also log attention/activity "
                    "phrases like 'I go cook dinner now', 'coffee break', and 'order food'. "
                    "They may also use family/baby-talk "
                    "terms like 'mum mum' for eat, 'pong pong' for shower/bathe, and "
                    "'orh orh' or 'orh orh kun' for sleep; transcribe those literally. "
                    "Return only the transcription, nothing else."
                )),
            ],
        )
        result = _extract_text(response)
        log_event(
            logger,
            logging.INFO,
            "llm_transcribe_audio_completed",
            model=model,
            response_chars=len(result),
        )
        return result
    except Exception as e:
        log_failure(
            logger,
            logging.ERROR,
            "llm_transcribe_audio_failed",
            e,
            model=model,
            audio_bytes=len(audio_bytes),
            mime_type=mime_type,
        )
        raise


# Extracts the text from a GenerateContentResponse.
# response.text is Optional[str] — it is None when the model returns no text parts
# (e.g. safety-filtered response, function-call-only response, empty candidates).
# Raises RuntimeError with a clear message rather than letting .strip() raise AttributeError.
def _extract_text(response) -> str:
    if response.text is None:
        raise RuntimeError("LLM returned no text (safety filter or empty response)")
    return response.text.strip()
