"""
03_extract_pdfs.py — Extract text from downloaded PDF attachments.

Thin wrapper around the shared PDF extraction collector. All extraction logic
lives in briefing_poc/collectors/pdf_extractor.py — this script just bridges
the city config to the collector's config dataclass.

Usage:
    uv run python briefing_poc/charlotte/scripts/03_extract_pdfs.py
"""

from city_config import cfg  # noqa: F401 — ensures sys.path is set up
from collectors.pdf_extractor import PdfConfig, extract_all_pdfs


# ============================================================================
# CONFIGURATION
# ============================================================================

PDF_DIR = cfg.data_dir / "legistar" / "attachments"
OUTPUT_DIR = cfg.data_dir / "extracted"


# ============================================================================
# MAIN
# ============================================================================

def main():
    config = PdfConfig(
        pdf_dir=PDF_DIR,
        output_dir=OUTPUT_DIR,
    )
    extract_all_pdfs(config)


if __name__ == "__main__":
    main()
