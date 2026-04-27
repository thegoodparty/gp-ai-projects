"""
stages.extract — PDF text extraction and meeting normalization.

Downloads agenda PDFs, extracts text (PyMuPDF + Firecrawl OCR fallback),
runs Gemini structured extraction, and outputs normalized meeting JSON.

Entry point: process_one_meeting()
"""
