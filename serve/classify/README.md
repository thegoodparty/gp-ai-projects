# Civic Message Classification Pipeline

High-throughput classification system for civic engagement messages using hybrid LLM + rule-based approach.

## Quick Start

```bash
# Classify messages from a specific campaign
cd serve/classify
uv run run_pipeline.py --data-source josh

# Quick test with sample data
uv run run_pipeline.py --data-source berkley --quick-test

# Process all campaigns
uv run run_pipeline.py --data-source all
```

## Architecture

**Pipeline Flow:**
```
CSV Loading → Data Cleaning → Classification → Aggregation → Export
     ↓              ↓               ↓              ↓            ↓
  Parallel     Smart Filter    Multi-Pass     Insights    CSV/JSON/MD
```

**Multi-Pass Classification:**
1. **Pass 1**: Rule-based uncategorization (federal politics, wrong numbers)
2. **Pass 2**: LLM issue identification with hierarchical taxonomy
3. **Pass 3**: Apply learned classification rules (property taxes, trucks)
4. **Pass 4**: Context refinement (root cause detection)
5. **Pass 5**: Overall sentiment and quality assessment

## Key Features

- **High Throughput**: 10,000+ messages/minute using ThreadPoolExecutor pattern
- **Hybrid Approach**: LLM accuracy + rule-based consistency
- **Smart Cleaning**: Extracts feedback from STOP messages
- **Hierarchical Taxonomy**: 9 primary categories, 30+ subcategories
- **Issue-Specific Stance**: Track sentiment per issue (e.g., negative about taxes, positive about parks)
- **Root Cause Detection**: Identifies underlying issues (zoning enables warehouse complaints)

## Performance

| Configuration | Messages/Min |
|---------------|--------------|
| Default (100) | ~833 |
| High (1200) | ~10,000+ |

Adjust via `BatchProcessingConfig(target_concurrency=1200)`

## Data Sources

Available campaigns in `serve/data/`:
- `josh` - Josh campaign messages
- `cara` - Cara campaign messages
- `berkley` - Berkley campaign messages
- `heather` - Heather campaign messages
- `japjeet` - Japjeet campaign messages
- `joanna` - Joanna campaign messages
- `jonathan` - Jonathan campaign messages
- `all` - Process all campaigns

## Output Files

Results saved to `serve/classify/output/`:

```
{source}_classified_messages.csv          # CSV with all classifications
{source}_classification_results.json      # Full pipeline results
{source}_insights_report.md              # Human-readable insights
{source}_processing_report.md            # Performance metrics
{source}_cleaning_report.md              # Data quality stats
```

## Configuration

Create `config.yaml` (optional):

```yaml
data:
  directory: "../data"
  source: "josh"
  inbound_only: true

processing:
  batch_size: 200
  max_parallel: 50
  target_concurrency: 100  # LLM connections
  temperature: 0.0

output:
  directory: "./output"
  formats: ["csv", "json", "markdown"]
```

## Classification Taxonomy

**Primary Categories:**
- `infrastructure_and_transportation` - Roads, transit, traffic
- `public_safety` - Police, fire, emergency services
- `education` - Schools, programs, safety
- `housing_and_development` - Zoning, housing, taxes
- `health_and_human_services` - Health, mental health, seniors
- `economic_development` - Jobs, business, industry
- `quality_of_life` - Parks, utilities, environment
- `government_operations` - Budget, transparency, engagement
- `other` - Uncategorized issues

## Classification Rules

**Learned Patterns:**
- **Property taxes** → `housing_and_development/taxes_and_assessments`
- **Truck traffic** → `infrastructure/roads_and_bridges` + `housing/zoning` (root cause)
- **E-bikes** → `infrastructure/transit` unless enforcement mentioned
- **Water bills** → `quality_of_life/utilities` (not general taxes)

**Uncategorized Filters:**
- Federal politics (Trump, Biden, Congress)
- Wrong numbers / out of district
- Personal attacks without substance
- Simple acknowledgments (thanks, ok, hi)

## Code Structure

```
serve/classify/
├── run_pipeline.py           # Main orchestrator
├── models.py                 # Pydantic data models
├── smart_classifier.py       # Multi-pass LLM classifier
├── classification_rules.py   # Rule-based logic
├── batch_processor.py        # High-throughput processing
├── data_loader.py           # CSV loading with parallel I/O
├── data_cleaner.py          # Smart filtering and cleaning
├── smart_aggregator.py      # Insights generation
└── validator.py             # Classification validation
```

## Production Patterns

**Threading Pattern:**
```python
# Non-blocking LLM calls via ThreadPoolExecutor
self.thread_pool = ThreadPoolExecutor(max_workers=target_concurrency)

response = await loop.run_in_executor(
    self.thread_pool,
    lambda: self.llm_client.generate_structured_content(...)
)
```

**Individual Task Concurrency:**
```python
# Create task per message (not batch-level)
tasks = [self.classify_message(msg) for msg in messages]
results = await asyncio.gather(*tasks, return_exceptions=True)
```

## Model Configuration

**Flash Model Settings:**
```python
GeminiClient(
    default_model=GeminiModelType.FLASH,
    thinking_budget=0,  # Optimized for efficiency
    temperature=0.0     # Consistent classifications
)
```

## Examples

**Basic Usage:**
```python
from serve.classify.run_pipeline import ClassificationPipeline

pipeline = ClassificationPipeline()
results = await pipeline.run_full_pipeline("josh")
pipeline.print_summary(results)
```

**Custom Configuration:**
```python
config = {
    "processing": {
        "batch_size": 100,
        "target_concurrency": 500
    }
}
pipeline = ClassificationPipeline(config)
```

**Access Classified Data:**
```python
results = await pipeline.run_full_pipeline("josh", return_data=True)
classified_messages = results["classified_messages"]

# Filter by stance
negative_tax_msgs = [
    msg for msg in classified_messages
    if msg.smart_classification and any(
        issue.primary_category == "housing_and_development"
        and issue.stance == IssueStance.NEGATIVE
        for issue in msg.smart_classification.issues
    )
]
```

## Troubleshooting

**"Data directory not found"**
- Ensure CSV files are in `serve/data/`
- Check path configuration in config.yaml

**"Slow classification speed"**
- Increase `target_concurrency` (default: 100)
- Disable validation: `enable_validation=False`
- Use ultra_fast_mode (enabled by default)

**"Empty classifications"**
- Check data cleaning removed too many messages
- Review `cleaning_report.md` for stats
- Adjust `min_length` in data cleaner config

## Related Pipelines

- `serve/analyze_texts/` - Full multi-stage analysis with atomization
- `serve/v1_pipeline/` - Legacy message processing pipeline

## License

Internal use only - GoodParty.org
