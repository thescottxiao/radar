"""Encode/decode structured button IDs for WhatsApp interactive messages.

Format: {action_type}:{action_id}:{response}
Example: event_confirm:a1b2c3d4-...:yes
"""


def encode_button_id(action_type: str, action_id: str, response: str) -> str:
    """Encode a structured button ID."""
    return f"{action_type}:{action_id}:{response}"


def decode_button_id(button_id: str) -> dict | None:
    """Decode a button ID into its components.

    Returns {"action_type": ..., "action_id": ..., "response": ...} or None if invalid.
    """
    parts = button_id.split(":", 2)
    if len(parts) != 3:
        return None
    return {
        "action_type": parts[0],
        "action_id": parts[1],
        "response": parts[2],
    }
