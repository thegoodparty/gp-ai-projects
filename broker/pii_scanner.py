import re
from dataclasses import dataclass, field


@dataclass
class PIIMatch:
    pattern_name: str
    matched_text: str
    field_path: str | None = None


PII_PATTERNS: dict[str, re.Pattern] = {
    "ssn": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    "phone": re.compile(r"\b\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b"),
    "email": re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b"),
    "dob": re.compile(r"\b(0[1-9]|1[0-2])/(0[1-9]|[12]\d|3[01])/\d{4}\b"),
}


def _redact(text: str) -> str:
    if len(text) <= 2:
        return text[0] + "*" if len(text) == 2 else text
    return text[0] + "***" + text[-1]


def scan_for_pii(text: str, *, field_path: str | None = None) -> list[PIIMatch]:
    matches = []
    for name, pattern in PII_PATTERNS.items():
        for m in pattern.finditer(text):
            matches.append(
                PIIMatch(
                    pattern_name=name,
                    matched_text=_redact(m.group()),
                    field_path=field_path,
                )
            )
    return matches


def scan_artifact(artifact: dict, *, _path: str = "") -> list[PIIMatch]:
    matches = []
    for key, value in artifact.items():
        current_path = f"{_path}.{key}" if _path else key
        if isinstance(value, str):
            matches.extend(scan_for_pii(value, field_path=current_path))
        elif isinstance(value, dict):
            matches.extend(scan_artifact(value, _path=current_path))
        elif isinstance(value, list):
            for i, item in enumerate(value):
                item_path = f"{current_path}[{i}]"
                if isinstance(item, str):
                    matches.extend(scan_for_pii(item, field_path=item_path))
                elif isinstance(item, dict):
                    matches.extend(scan_artifact(item, _path=item_path))
    return matches
