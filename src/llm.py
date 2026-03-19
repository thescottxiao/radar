import logging
from typing import TypeVar

import anthropic
from pydantic import BaseModel

from src.config import settings

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

HAIKU_MODEL = "claude-haiku-4-5-20251001"
SONNET_MODEL = "claude-sonnet-4-5-20241022"


def _get_client() -> anthropic.AsyncAnthropic:
    return anthropic.AsyncAnthropic(
        api_key=settings.anthropic_api_key.get_secret_value(),
    )


async def classify(prompt: str, system: str, model: str = HAIKU_MODEL) -> str:
    """Fast classification using Haiku. Returns raw text response."""
    client = _get_client()
    response = await client.messages.create(
        model=model,
        max_tokens=256,
        system=system,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text


async def extract(
    prompt: str, system: str, schema: type[T], model: str = SONNET_MODEL
) -> T:
    """Structured extraction using Sonnet with tool_use for JSON output."""
    client = _get_client()
    tool_schema = schema.model_json_schema()
    # Remove $defs from the schema root and inline if needed
    tool_schema.pop("$defs", None)

    response = await client.messages.create(
        model=model,
        max_tokens=4096,
        system=system,
        messages=[{"role": "user", "content": prompt}],
        tools=[
            {
                "name": "extract_data",
                "description": f"Extract structured data matching the {schema.__name__} schema.",
                "input_schema": tool_schema,
            }
        ],
        tool_choice={"type": "tool", "name": "extract_data"},
    )

    for block in response.content:
        if block.type == "tool_use":
            return schema.model_validate(block.input)

    raise ValueError("No tool_use block in response")


async def generate(prompt: str, system: str, model: str = SONNET_MODEL) -> str:
    """Free-form generation using Sonnet. Returns text response."""
    client = _get_client()
    response = await client.messages.create(
        model=model,
        max_tokens=2048,
        system=system,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text
