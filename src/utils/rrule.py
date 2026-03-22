"""RRULE utilities for building, parsing, and formatting RFC 5545 recurrence rules.

Supports the subset of RRULE needed for family calendar events:
WEEKLY, MONTHLY, DAILY frequencies with day-of-week, day-of-month, interval, and until.
"""

import re
from datetime import date, datetime

# Two-letter day codes used by RRULE (RFC 5545)
_DAY_NAMES = {
    "MO": "Monday",
    "TU": "Tuesday",
    "WE": "Wednesday",
    "TH": "Thursday",
    "FR": "Friday",
    "SA": "Saturday",
    "SU": "Sunday",
}

_DAY_FULL_TO_CODE = {
    "monday": "MO",
    "tuesday": "TU",
    "wednesday": "WE",
    "thursday": "TH",
    "friday": "FR",
    "saturday": "SA",
    "sunday": "SU",
    # Abbreviations
    "mon": "MO",
    "tue": "TU",
    "tues": "TU",
    "wed": "WE",
    "thu": "TH",
    "thurs": "TH",
    "fri": "FR",
    "sat": "SA",
    "sun": "SU",
}


def build_rrule(
    freq: str,
    byday: list[str] | None = None,
    bymonthday: int | None = None,
    until: date | None = None,
    interval: int = 1,
) -> str:
    """Build an RRULE string from components.

    Args:
        freq: WEEKLY, MONTHLY, or DAILY
        byday: Day codes like ["MO", "WE", "FR"]
        bymonthday: Day of month (1-31) for MONTHLY freq
        until: End date (None = indefinite)
        interval: Recurrence interval (2 = biweekly for WEEKLY)

    Returns:
        RRULE string without the "RRULE:" prefix, e.g. "FREQ=WEEKLY;BYDAY=MO,WE"
    """
    parts = [f"FREQ={freq.upper()}"]

    if interval > 1:
        parts.append(f"INTERVAL={interval}")

    if byday:
        parts.append(f"BYDAY={','.join(d.upper() for d in byday)}")

    if bymonthday is not None:
        parts.append(f"BYMONTHDAY={bymonthday}")

    if until:
        # RRULE UNTIL uses UTC format: YYYYMMDDTHHMMSSZ
        if isinstance(until, datetime):
            parts.append(f"UNTIL={until.strftime('%Y%m%dT%H%M%SZ')}")
        else:
            parts.append(f"UNTIL={until.strftime('%Y%m%d')}T235959Z")

    return ";".join(parts)


def rrule_to_gcal(rrule: str) -> list[str]:
    """Convert an RRULE string to GCal API format.

    GCal expects a list of strings with "RRULE:" prefix.
    """
    if rrule.startswith("RRULE:"):
        return [rrule]
    return [f"RRULE:{rrule}"]


def rrule_to_human(rrule: str) -> str:
    """Convert an RRULE string to human-readable text.

    Examples:
        "FREQ=WEEKLY;BYDAY=MO" -> "every Monday"
        "FREQ=WEEKLY;BYDAY=TU,TH" -> "every Tuesday and Thursday"
        "FREQ=WEEKLY;INTERVAL=2;BYDAY=MO" -> "every 2 weeks on Monday"
        "FREQ=MONTHLY;BYMONTHDAY=15" -> "monthly on the 15th"
        "FREQ=DAILY" -> "every day"
    """
    parts = _parse_rrule(rrule)
    freq = parts.get("FREQ", "WEEKLY")
    interval = int(parts.get("INTERVAL", "1"))
    byday = parts.get("BYDAY", "").split(",") if parts.get("BYDAY") else []
    bymonthday = parts.get("BYMONTHDAY")
    until = parts.get("UNTIL")

    day_names = [_DAY_NAMES.get(d.upper(), d) for d in byday if d]

    if freq == "DAILY":
        result = "every day" if interval == 1 else f"every {interval} days"
    elif freq == "WEEKLY":
        if interval == 1:
            if day_names:
                result = "every " + _join_words(day_names)
            else:
                result = "every week"
        elif interval == 2:
            if day_names:
                result = "every 2 weeks on " + _join_words(day_names)
            else:
                result = "every 2 weeks"
        else:
            if day_names:
                result = f"every {interval} weeks on " + _join_words(day_names)
            else:
                result = f"every {interval} weeks"
    elif freq == "MONTHLY":
        if bymonthday:
            result = f"monthly on the {_ordinal(int(bymonthday))}"
        elif day_names:
            result = "monthly on " + _join_words(day_names)
        else:
            result = "every month" if interval == 1 else f"every {interval} months"
    else:
        result = rrule  # Fallback to raw RRULE

    if until:
        try:
            until_dt = datetime.strptime(until[:8], "%Y%m%d")
            result += f", until {until_dt.strftime('%B %d, %Y')}"
        except ValueError:
            pass

    return result


def infer_rrule_from_text(text: str) -> tuple[str, str] | None:
    """Infer an RRULE from natural language text.

    Returns (rrule_string, human_pattern) or None if no pattern detected.
    This is a simple regex-based fallback; the LLM extraction is preferred.
    """
    text_lower = text.lower().strip()

    # "every day" / "daily"
    if re.search(r"\b(every\s+day|daily)\b", text_lower):
        return "FREQ=DAILY", "every day"

    # "biweekly" / "every other week" / "every 2 weeks"
    biweekly = re.search(r"\b(biweekly|every\s+other\s+week|every\s+2\s+weeks?)\b", text_lower)
    if biweekly:
        # Check for day names
        days = _extract_day_codes(text_lower)
        if days:
            rrule = build_rrule("WEEKLY", byday=days, interval=2)
            human = "every 2 weeks on " + _join_words([_DAY_NAMES[d] for d in days])
        else:
            rrule = build_rrule("WEEKLY", interval=2)
            human = "every 2 weeks"
        return rrule, human

    # "every [day names]" — e.g., "every Monday", "every Tue and Thu"
    every_match = re.search(
        r"\bevery\s+("
        r"(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
        r"mon|tue|tues|wed|thu|thurs|fri|sat|sun)"
        r"(?:\s*(?:,|and|&)\s*"
        r"(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
        r"mon|tue|tues|wed|thu|thurs|fri|sat|sun))*)",
        text_lower,
    )
    if every_match:
        days = _extract_day_codes(every_match.group(1))
        if days:
            rrule = build_rrule("WEEKLY", byday=days)
            human = "every " + _join_words([_DAY_NAMES[d] for d in days])
            return rrule, human

    # "weekly" without specific days
    if re.search(r"\bweekly\b", text_lower):
        return "FREQ=WEEKLY", "every week"

    # "monthly" / "every month"
    if re.search(r"\b(monthly|every\s+month)\b", text_lower):
        return "FREQ=MONTHLY", "every month"

    return None


def _parse_rrule(rrule: str) -> dict[str, str]:
    """Parse an RRULE string into a dict of parts."""
    rrule = rrule.removeprefix("RRULE:")
    parts = {}
    for part in rrule.split(";"):
        if "=" in part:
            key, value = part.split("=", 1)
            parts[key.upper()] = value
    return parts


def _extract_day_codes(text: str) -> list[str]:
    """Extract RRULE day codes from text containing day names."""
    codes = []
    seen = set()
    for word in re.split(r"[\s,&]+", text.lower()):
        word = word.strip(".,;")
        code = _DAY_FULL_TO_CODE.get(word)
        if code and code not in seen:
            codes.append(code)
            seen.add(code)
    return codes


def _join_words(words: list[str]) -> str:
    """Join words with commas and 'and': ['a','b','c'] -> 'a, b, and c'."""
    if len(words) <= 1:
        return words[0] if words else ""
    if len(words) == 2:
        return f"{words[0]} and {words[1]}"
    return ", ".join(words[:-1]) + f", and {words[-1]}"


def _ordinal(n: int) -> str:
    """Convert number to ordinal: 1 -> '1st', 2 -> '2nd', etc."""
    if 11 <= n % 100 <= 13:
        return f"{n}th"
    suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"
