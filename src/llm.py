import logging
from typing import TypeVar

import anthropic
from pydantic import BaseModel

from src.config import settings

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

HAIKU_MODEL = "claude-haiku-4-5-20251001"
SONNET_MODEL = "claude-sonnet-4-20250514"


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
            data = _normalize_extraction(block.input)
            return schema.model_validate(data)

    raise ValueError("No tool_use block in response")


def _normalize_extraction(data: dict) -> dict:
    """Fix common LLM field name variations before Pydantic validation."""
    # Normalize events — ensure datetime fields are present
    for event in data.get("events", []):
        # LLM may use "date", "start", "start_time", "start_date" instead of "datetime_start"
        if "datetime_start" not in event or event["datetime_start"] is None:
            for alt in ("date", "start", "start_time", "start_date", "date_start"):
                if alt in event and event[alt] is not None:
                    event["datetime_start"] = event.pop(alt)
                    break
        # Same for datetime_end
        if "datetime_end" not in event or event["datetime_end"] is None:
            for alt in ("end", "end_time", "end_date", "date_end"):
                if alt in event and event[alt] is not None:
                    event["datetime_end"] = event.pop(alt)
                    break
        # LLM may use "name" instead of "title"
        if "title" not in event and "name" in event:
            event["title"] = event.pop("name")
    # Normalize action items
    for item in data.get("action_items", []):
        if "task" in item and "description" not in item:
            item["description"] = item.pop("task")
        if "type" in item and "action_type" not in item:
            item["action_type"] = item.pop("type")
    # Normalize learnings
    _VALID_LEARNING_CATEGORIES = {
        "child_school", "child_activity", "child_friend", "contact",
        "gear", "preference", "schedule_pattern", "budget",
    }
    # Map common LLM category outputs to valid DB categories
    _CATEGORY_MAP = {
        "school": "child_school",
        "activity": "child_activity",
        "friend": "child_friend",
        "allergy": "preference",
        "routine": "schedule_pattern",
        "schedule": "schedule_pattern",
        "other": "contact",
    }
    for learning in data.get("learnings", []):
        if "category" not in learning:
            learning["category"] = learning.pop("type", "contact")
        # Normalize category to valid DB value
        cat = learning.get("category", "").lower().strip()
        if cat not in _VALID_LEARNING_CATEGORIES:
            learning["category"] = _CATEGORY_MAP.get(cat, "contact")
        if "fact" not in learning:
            # LLM may use description, content, detail, or value instead of fact
            for alt in ("description", "content", "detail", "value", "text"):
                if alt in learning:
                    learning["fact"] = learning.pop(alt)
                    break
        # Normalize entity_type to valid DB values
        _VALID_ENTITY_TYPES = {"child", "caregiver", "external_contact"}
        et = learning.get("entity_type")
        if et and et not in _VALID_ENTITY_TYPES:
            if et in ("family", "parent", "guardian"):
                learning["entity_type"] = "caregiver"
            elif et in ("coach", "teacher", "doctor", "contact"):
                learning["entity_type"] = "external_contact"
            else:
                learning["entity_type"] = None
    return data


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
