"""Clean and normalise raw financial data scraped from DBD."""

import re
import logging
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

# Thai digit map
_THAI_DIGITS = str.maketrans("๐๑๒๓๔๕๖๗๘๙", "0123456789")

# Common Thai suffixes for large numbers
_MULTIPLIERS = {
    "ล้าน": 1_000_000,
    "พัน": 1_000,
    "หมื่น": 10_000,
    "แสน": 100_000,
    "พันล้าน": 1_000_000_000,
}


def _thai_to_arabic(text: str) -> str:
    return text.translate(_THAI_DIGITS)


def clean_numeric(raw: Optional[str]) -> Optional[float]:
    """
    Convert a raw Thai/English numeric string to a Python float.

    Handles:
    - Thai numerals (๑๒๓…)
    - Parentheses as negative sign  e.g. (1,234.56) → -1234.56
    - Thousands separators
    - Thai multiplier suffixes (ล้าน, พัน, …)
    - Dash / em-dash meaning zero or N/A
    """
    if raw is None:
        return None

    text = str(raw).strip()

    if text in ("-", "–", "—", "N/A", "n/a", "ไม่มี", ""):
        return None

    text = _thai_to_arabic(text)

    # Check for multiplier suffix
    multiplier = 1
    for suffix, factor in _MULTIPLIERS.items():
        if suffix in text:
            text = text.replace(suffix, "").strip()
            multiplier = factor
            break

    # Detect negative via parentheses
    negative = False
    if text.startswith("(") and text.endswith(")"):
        negative = True
        text = text[1:-1]

    # Strip currency symbols and whitespace
    text = re.sub(r"[฿$€£\s]", "", text)

    # Remove thousands separators (comma), keep decimal dot
    text = text.replace(",", "")

    try:
        value = float(text) * multiplier
        return -value if negative else value
    except ValueError:
        logger.debug("Could not parse numeric value: %r", raw)
        return None


def clean_year(raw: Optional[str]) -> Optional[int]:
    """
    Extract a 4-digit fiscal year from a raw string.

    Accepts:
    - "2566"  (Buddhist Era)  → converted to CE (2566 - 543 = 2023)
    - "2023"  (Common Era)    → returned as-is
    - "ปี 2566 / 2023"        → picks the CE year
    """
    if not raw:
        return None

    text = _thai_to_arabic(str(raw))

    years = re.findall(r"\b(\d{4})\b", text)
    if not years:
        return None

    for y in years:
        yr = int(y)
        # Distinguish Buddhist Era (2500–2600) vs Common Era
        if 2500 <= yr <= 2600:
            return yr - 543      # Convert BE → CE
        if 1990 <= yr <= 2100:
            return yr

    return None


def clean_company_name(raw: Optional[str]) -> str:
    """Strip extra whitespace and normalise Thai company name."""
    if not raw:
        return ""
    # Collapse multiple spaces / newlines
    return re.sub(r"\s+", " ", str(raw)).strip()


def process_raw_record(raw: dict) -> Optional[dict]:
    """
    Validate and clean a raw scraped record.

    Expected keys (all strings, may be None/empty):
        company_name, year, revenue, net_profit, assets, liabilities, equity

    Returns a clean dict ready for DB insertion, or None if the record is
    unusable (missing company_name or year).
    """
    company_name = clean_company_name(raw.get("company_name"))
    year = clean_year(str(raw.get("year", "")))

    if not company_name or year is None:
        logger.debug("Skipping record with missing company_name or year: %s", raw)
        return None

    current_year = datetime.utcnow().year
    if not (1990 <= year <= current_year + 1):
        logger.debug("Skipping record with implausible year %s for %s", year, company_name)
        return None

    return {
        "company_name": company_name,
        "year": year,
        "revenue": clean_numeric(raw.get("revenue")),
        "net_profit": clean_numeric(raw.get("net_profit")),
        "assets": clean_numeric(raw.get("assets")),
        "liabilities": clean_numeric(raw.get("liabilities")),
        "equity": clean_numeric(raw.get("equity")),
    }


def process_batch(raw_records: list[dict]) -> list[dict]:
    """Clean a list of raw records, dropping invalid ones."""
    cleaned = []
    for raw in raw_records:
        result = process_raw_record(raw)
        if result:
            cleaned.append(result)
    return cleaned
