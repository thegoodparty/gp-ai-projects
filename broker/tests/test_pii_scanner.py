from broker.pii_scanner import scan_for_pii, scan_artifact, PIIMatch


def test_detects_ssn():
    matches = scan_for_pii("SSN: 123-45-6789")
    assert len(matches) >= 1
    ssn_match = next(m for m in matches if m.pattern_name == "ssn")
    assert ssn_match is not None
    assert "123-45-6789" not in ssn_match.matched_text
    assert ssn_match.matched_text.startswith("1")
    assert ssn_match.matched_text.endswith("9")


def test_detects_phone():
    matches = scan_for_pii("Call (555) 123-4567")
    assert len(matches) >= 1
    phone_match = next(m for m in matches if m.pattern_name == "phone")
    assert phone_match is not None
    assert "(555) 123-4567" not in phone_match.matched_text


def test_detects_email():
    matches = scan_for_pii("Contact voter@example.com for info")
    assert len(matches) >= 1
    email_match = next(m for m in matches if m.pattern_name == "email")
    assert email_match is not None
    assert "voter@example.com" not in email_match.matched_text
    assert email_match.matched_text.startswith("v")
    assert email_match.matched_text.endswith("m")


def test_detects_dob():
    matches = scan_for_pii("DOB: 01/15/1990")
    assert len(matches) >= 1
    dob_match = next(m for m in matches if m.pattern_name == "dob")
    assert dob_match is not None
    assert "01/15/1990" not in dob_match.matched_text


def test_clean_text_returns_empty():
    matches = scan_for_pii("The city council met on Tuesday to discuss infrastructure.")
    assert matches == []


def test_scan_artifact_walks_nested():
    artifact = {
        "summary": "Contact voter@example.com",
        "details": {
            "phone": "Call (555) 123-4567",
            "notes": ["SSN: 123-45-6789", "Clean text here"],
        },
    }
    matches = scan_artifact(artifact)
    pattern_names = {m.pattern_name for m in matches}
    assert "email" in pattern_names
    assert "phone" in pattern_names
    assert "ssn" in pattern_names
    assert len(matches) >= 3


def test_matched_text_is_redacted():
    matches = scan_for_pii("SSN: 123-45-6789 and voter@example.com")
    for m in matches:
        if m.pattern_name == "ssn":
            assert m.matched_text != "123-45-6789"
            assert len(m.matched_text) < len("123-45-6789")
        if m.pattern_name == "email":
            assert m.matched_text != "voter@example.com"
