from __future__ import annotations

from typing import Any


_CHAT_CONTROL_TOKENS = (
    "<|start_header_id|>",
    "<|end_header_id|>",
    "<|eot_id|>",
    "<|reserved_special_token_70|>",
    "<|reserved_special_token_69|>",
    "<|reserved_special_token_71|>",
)
_ASSISTANT_HEADERS = (
    "<|start_header_id|>assistant<|end_header_id|>",
    "<|start_header_id|>assistant<|end_header_id|>\n\n",
)


def _without_chat_tokens(value: str) -> str:
    cleaned = value
    for token in _CHAT_CONTROL_TOKENS:
        cleaned = cleaned.replace(token, "")
    return cleaned.strip()


def generated_completion_text(
    output: Any,
    *,
    formatted_prompt: str = "",
    user_prompt: str = "",
) -> str:
    """Return only a model answer while leaving the exact raw output untouched."""
    value = output[0] if isinstance(output, list) and output else output
    if isinstance(value, dict):
        value = value.get("generated_text", value.get("text", ""))
    if isinstance(value, list):
        for item in reversed(value):
            if not isinstance(item, dict) or item.get("role") != "assistant":
                continue
            content = item.get("content", "")
            if isinstance(content, list):
                return " ".join(
                    str(part.get("text", ""))
                    for part in content
                    if isinstance(part, dict)
                ).strip()
            return str(content).strip()

    text = str(value or "").strip()
    if not text:
        return ""

    # Use the final assistant boundary because malformed legacy decodes can
    # repeat earlier chat turns.
    boundary = max(text.rfind(header) for header in _ASSISTANT_HEADERS)
    if boundary >= 0:
        header = next(
            header for header in _ASSISTANT_HEADERS
            if text.rfind(header) == boundary
        )
        return _without_chat_tokens(text[boundary + len(header):])

    if formatted_prompt and text.startswith(formatted_prompt):
        return _without_chat_tokens(text[len(formatted_prompt):])

    cleaned = _without_chat_tokens(text)
    cleaned_formatted = _without_chat_tokens(formatted_prompt)
    if cleaned_formatted and cleaned.startswith(cleaned_formatted):
        return cleaned[len(cleaned_formatted):].strip()

    # Older evidence retains the user prompt but may lack the complete template.
    cleaned_user_prompt = _without_chat_tokens(user_prompt)
    if cleaned_user_prompt:
        prompt_end = cleaned.rfind(cleaned_user_prompt)
        if prompt_end >= 0:
            return cleaned[prompt_end + len(cleaned_user_prompt):].strip()

    return cleaned
