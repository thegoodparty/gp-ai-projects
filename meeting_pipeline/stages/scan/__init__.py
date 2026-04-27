"""
stages.scan — Meeting schedule scanning.

Checks each city's known source for upcoming meetings and agenda-posted status.
Runs daily/weekly (cheap for platform cities, moderate for generic).
Output: upcoming_meetings.json per city on S3.

Modules:
    process          — process_one_city() entry point
    platforms/       — Per-platform scanners (legistar, civicplus, etc.)
"""
