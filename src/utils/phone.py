"""Phone number normalization utilities."""


def normalize_phone(phone: str) -> str:
    """Normalize a phone number to +{digits} format.

    Strips all non-digit characters and ensures a leading '+'.
    Examples:
        "16173866506"   → "+16173866506"
        "+16173866506"  → "+16173866506"
        "(617) 386-6506" → "+16173866506"
    """
    digits = "".join(c for c in phone if c.isdigit())
    return f"+{digits}"
