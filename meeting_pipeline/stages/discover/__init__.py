"""
stages.discover — Source discovery for the meeting data pipeline.

Finds the best URL and platform for each city's governing body agenda page.
Runs once per city (expensive). Output: source.json per city on S3.

Modules:
    process   — process_one_city() entry point (delegates to source_discover for now)
    search    — Search backends (Serper, DDG, Exa, Tavily, Firecrawl, PDF search)
    crawl     — Domain validation, Firecrawl map, multi-hop page crawling
    scoring   — Candidate scoring, ranking, domain trust classification
"""
