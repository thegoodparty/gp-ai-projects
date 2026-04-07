"""
pdf_extractor.py — Reusable PDF text and table extraction collector.

Extracts readable text and tables from PDF files using PyMuPDF (fast text)
and pdfplumber (table detection). Completely city-agnostic — works with any
directory of PDFs.

Usage:
    from collectors.pdf_extractor import PdfConfig, extract_all_pdfs

    config = PdfConfig(
        pdf_dir=Path("data/legistar/attachments"),
        output_dir=Path("data/extracted"),
    )
    result = extract_all_pdfs(config)
"""

import json
import time
from pathlib import Path
from dataclasses import dataclass, field

import fitz
import pdfplumber


# ============================================================================
# CONFIG AND RESULT DATACLASSES
# ============================================================================

@dataclass
class PdfConfig:
    """Configuration for PDF extraction."""
    pdf_dir: Path
    output_dir: Path
    max_file_size: int = 100 * 1024 * 1024  # 100 MB
    min_chars_for_text_page: int = 50


@dataclass
class PdfResult:
    """Summary of PDF extraction run."""
    total_found: int = 0
    processed: int = 0
    skipped_existing: int = 0
    skipped_other: int = 0
    no_text_count: int = 0
    total_chars: int = 0
    total_tables: int = 0
    elapsed_seconds: float = 0.0
    output_dir: Path = field(default_factory=lambda: Path("."))


# ============================================================================
# EXTRACTION FUNCTIONS
# ============================================================================

def extract_text_pymupdf(pdf_path: Path, min_chars: int = 50) -> dict:
    """Extract text from a PDF using PyMuPDF (fast, general-purpose)."""
    with fitz.open(str(pdf_path)) as doc:
        pages = []
        for i, page in enumerate(doc):
            text = page.get_text()
            pages.append({
                "page_number": i + 1,
                "text": text,
                "char_count": len(text),
            })

        full_text = "\n\n".join(p["text"] for p in pages)
        text_pages = sum(1 for p in pages if p["char_count"] >= min_chars)

        return {
            "pages": pages,
            "full_text": full_text,
            "total_pages": len(pages),
            "total_chars": len(full_text),
            "text_pages": text_pages,
            "has_text": text_pages > 0,
        }


def extract_tables_pdfplumber(pdf_path: Path) -> list[dict]:
    """Extract tables from a PDF using pdfplumber."""
    tables = []
    try:
        with pdfplumber.open(str(pdf_path)) as pdf:
            for i, page in enumerate(pdf.pages):
                page_tables = page.extract_tables()
                for j, table in enumerate(page_tables):
                    if not table or len(table) < 2:
                        continue
                    tables.append({
                        "page": i + 1,
                        "table_index": j,
                        "headers": table[0],
                        "rows": table[1:],
                        "row_count": len(table),
                    })
    except Exception as e:
        print(f"  WARNING: Table extraction failed for {pdf_path.name}: {e}")
    return tables


def process_single_pdf(pdf_path: Path, config: PdfConfig) -> dict | None:
    """Process a single PDF: extract text + tables, return combined result."""
    file_size = pdf_path.stat().st_size

    if file_size > config.max_file_size:
        print(f"  SKIP: {pdf_path.name} is too large ({file_size / 1024 / 1024:.0f} MB)")
        return None

    if file_size < 100:
        print(f"  SKIP: {pdf_path.name} is too small ({file_size} bytes)")
        return None

    try:
        text_result = extract_text_pymupdf(pdf_path, config.min_chars_for_text_page)

        tables = []
        if text_result["has_text"]:
            tables = extract_tables_pdfplumber(pdf_path)

        return {
            "metadata": {
                "source_file": pdf_path.name,
                "file_size_bytes": file_size,
                "file_size_mb": round(file_size / 1024 / 1024, 2),
                "total_pages": text_result["total_pages"],
                "text_pages": text_result["text_pages"],
                "total_chars": text_result["total_chars"],
                "has_text": text_result["has_text"],
                "table_count": len(tables),
            },
            "full_text": text_result["full_text"],
            "pages": text_result["pages"],
            "tables": tables,
        }
    except Exception as e:
        print(f"  ERROR: Failed to process {pdf_path.name}: {e}")
        return None


# ============================================================================
# MAIN EXTRACTION FUNCTION
# ============================================================================

def extract_all_pdfs(config: PdfConfig) -> PdfResult:
    """
    Process all PDFs in config.pdf_dir, save extracted text as JSON.

    Skips files already processed (resumable). Returns a summary result.
    """
    config.output_dir.mkdir(parents=True, exist_ok=True)

    pdf_files = sorted(config.pdf_dir.glob("*.pdf"))
    total_count = len(pdf_files)

    print(f"Found {total_count} PDFs to process in {config.pdf_dir}")
    print(f"Saving extracted text to {config.output_dir}")
    print()

    processed = 0
    skipped_existing = 0
    skipped_other = 0
    total_chars = 0
    total_tables = 0
    no_text_count = 0
    start_time = time.time()

    for i, pdf_path in enumerate(pdf_files):
        out_path = config.output_dir / pdf_path.with_suffix(".json").name

        if out_path.exists():
            skipped_existing += 1
            continue

        if i % 50 == 0:
            elapsed = time.time() - start_time
            if processed > 0:
                per_file = elapsed / processed
                remaining = per_file * (total_count - i - skipped_existing)
                print(f"  [{i + 1}/{total_count}] "
                      f"Processed: {processed}, Skipped: {skipped_existing}, "
                      f"~{remaining:.0f}s remaining...")
            else:
                print(f"  [{i + 1}/{total_count}] Starting...")

        result = process_single_pdf(pdf_path, config)

        if result is None:
            skipped_other += 1
            continue

        if not result["metadata"]["has_text"]:
            no_text_count += 1

        with open(out_path, "w") as f:
            json.dump(result, f, indent=2, ensure_ascii=False)

        processed += 1
        total_chars += result["metadata"]["total_chars"]
        total_tables += result["metadata"]["table_count"]

    elapsed = time.time() - start_time

    print()
    print("=" * 60)
    print("PDF Extraction complete!")
    print(f"  Total PDFs found:      {total_count}")
    print(f"  Successfully processed:{processed}")
    print(f"  Skipped (already done):{skipped_existing}")
    print(f"  Skipped (error/size):  {skipped_other}")
    print(f"  No extractable text:   {no_text_count}")
    print(f"  Total characters:      {total_chars:,}")
    print(f"  Total tables found:    {total_tables}")
    print(f"  Time elapsed:          {elapsed:.0f}s ({elapsed / 60:.1f} min)")
    if processed > 0:
        print(f"  Avg per PDF:           {elapsed / processed:.1f}s")
        print(f"  Avg chars per PDF:     {total_chars // processed:,}")
    print(f"  Saved to:              {config.output_dir.resolve()}")
    print("=" * 60)

    return PdfResult(
        total_found=total_count,
        processed=processed,
        skipped_existing=skipped_existing,
        skipped_other=skipped_other,
        no_text_count=no_text_count,
        total_chars=total_chars,
        total_tables=total_tables,
        elapsed_seconds=elapsed,
        output_dir=config.output_dir,
    )
