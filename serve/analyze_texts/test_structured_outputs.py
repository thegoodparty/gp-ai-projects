import sys
from pathlib import Path

sys.path.append(str(Path(__file__).parent.parent.parent))

from serve.analyze_texts.orchestrator import AnalyzeTextsOrchestrator

if __name__ == "__main__":
    orchestrator = AnalyzeTextsOrchestrator()

    result = orchestrator.run(campaign="heather-ghaps")

    print("\n✅ Pipeline completed successfully!")
    print(f"Campaign: {result['campaign']}")
    print(f"Messages: {len(result['classified_messages'])}")
    print(f"Categories: {len(result['category_summaries'])}")
