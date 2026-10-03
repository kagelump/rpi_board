"""Shared Yahoo/ISO date normalization for weather source alignment.

Yahoo labels its today/tomorrow tabs in human month/day form (``"10月2日"``)
with no year. The board target is a calculated ISO date, so Yahoo rows are
resolved to the nearest matching year before they are selected or compared to
Open-Meteo's dated series.
"""
import re
from datetime import datetime


_ISO_DATE_PATTERN = re.compile(r"(\d{4})\s*-\s*(\d{1,2})\s*-\s*(\d{1,2})")
_MONTH_DAY_PATTERNS = (
    re.compile(r"(\d{1,2})\s*月\s*(\d{1,2})\s*日"),
    re.compile(r"(\d{1,2})\s*/\s*(\d{1,2})"),
)


def parse_yahoo_month_day(label):
    """Return ``(month, day)`` from a human date label, else ``None``."""
    if not isinstance(label, str):
        return None
    iso = _ISO_DATE_PATTERN.search(label)
    if iso:
        month, day = int(iso.group(2)), int(iso.group(3))
        if 1 <= month <= 12 and 1 <= day <= 31:
            try:
                datetime(2000, month, day)
            except ValueError:
                pass
            else:
                return month, day
    for pattern in _MONTH_DAY_PATTERNS:
        for match in pattern.finditer(label):
            month, day = int(match.group(1)), int(match.group(2))
            if not (1 <= month <= 12 and 1 <= day <= 31):
                continue
            try:
                datetime(2000, month, day)
            except ValueError:
                continue
            return month, day
    return None


def normalize_yahoo_date(item, reference_date):
    """Resolve a Yahoo item to an ISO date, or ``None`` when it is unknown.

    An explicit ISO ``date_iso``/``date`` always wins. Otherwise the human
    month/day label is anchored to the year nearest ``reference_date`` (the run
    date), which keeps month and year boundaries correct.
    """
    if not isinstance(item, dict):
        return None
    for key in ("date_iso", "date"):
        value = item.get(key)
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value[:10]).date().isoformat()
            except ValueError:
                pass
    parsed = parse_yahoo_month_day(item.get("date_label"))
    if parsed is None:
        return None
    month, day = parsed
    candidates = []
    for year in (reference_date.year - 1, reference_date.year, reference_date.year + 1):
        try:
            candidates.append(datetime(year, month, day).date())
        except ValueError:
            continue
    if not candidates:
        return None
    return min(candidates, key=lambda candidate: abs((candidate - reference_date).days)).isoformat()
