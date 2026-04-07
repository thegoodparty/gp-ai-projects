"""
collection_agent — Automated collection orchestration for briefing POC.

Portable design: all file I/O goes through StorageBackend, all components
accept/return plain dicts so they can run as Lambda handlers or locally.
"""
