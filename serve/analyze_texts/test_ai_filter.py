import sys
from pathlib import Path

sys.path.append(str(Path(__file__).parent.parent.parent))

from serve.analyze_texts.stages.stage_2_filter import MessageFilter
from serve.analyze_texts.models import MessageRecord

test_messages = [
    ("Wrong number this is Johnny with Green Valley plumbing", False),
    ("Do not text k at this number not her phone anymore", False),
    ("Affordable housing is a major concern in our community", True),
    ("Crime in the community", True),
    ("nan", False),
    ("not my number", False),
    ("We need better schools and education funding", True),
]

message_filter = MessageFilter()

print("Testing AI-based non-substantive message filtering:\n")

for text, expected_substantive in test_messages:
    is_non_substantive = message_filter.is_non_substantive(text)
    is_substantive = not is_non_substantive

    status = "✓" if is_substantive == expected_substantive else "✗"
    result = "SUBSTANTIVE" if is_substantive else "NON-SUBSTANTIVE"

    print(f"{status} '{text}'")
    print(f"   Result: {result} (Expected: {'SUBSTANTIVE' if expected_substantive else 'NON-SUBSTANTIVE'})\n")
