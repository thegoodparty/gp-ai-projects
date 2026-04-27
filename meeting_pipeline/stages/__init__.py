"""
meeting_pipeline.stages — Pipeline stages.

Each stage has a process.py with a process_one_city() or process_one_meeting()
function that can be called by a batch runner (current) or Lambda handler (future).

Stages:
    discover/   — Find the best URL and platform for each city
    scan/       — Check for upcoming meetings and agenda-posted status
    extract/    — Download PDFs and extract structured meeting data
    briefing/   — Generate multi-pass briefings with provenance tracking
"""
