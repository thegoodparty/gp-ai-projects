# Hierarchical Discovery Pipeline

A machine learning pipeline for discovering thematic clusters in civic engagement messages using hierarchical clustering, optimal k-selection, and LLM-powered analysis.

## Overview

This pipeline transforms raw civic messages into actionable insights through semantic clustering and AI-powered theme extraction. It serves two purposes:

1. **Standalone Research Tool**: Analyze campaign datasets, experiment with clustering parameters, generate comprehensive reports
2. **Production Integration**: Consumed by v1_pipeline via `ClusteringAdapter` for real-time message clustering

**Key Features:**
- Separation-first optimal K selection (prioritizes theme distinctness over cluster size)
- Hierarchical clustering with complete linkage and cosine similarity
- Parallel LLM-powered theme analysis (50 concurrent workers)
- Two-stage cluster merging (embedding similarity + LLM validation)
- 100% phone attribution accuracy via metadata preservation
- Comprehensive visualizations and exports

## Quick Start

```bash
# Install dependencies
uv sync && source .venv/bin/activate

# Run on a campaign
cd serve/hierarchical_discovery
uv run run_pipeline.py --data-source berkeley

# With keyword anonymization
uv run run_pipeline.py --data-source berkeley --anonymize-keywords berkeley,Berkeley

# Debug mode
ENVIRONMENT=development uv run run_pipeline.py --data-source josh
```

**Input**: CSV files in `data/` with columns `Contact Phone Number`, `Message Text`, demographics
**Output**: `output/` directory with CSVs, JSONs, reports, visualizations, dendrograms

## How It Works

### The 10-Stage Pipeline

```
Raw CSV → Load → Filter → AI Process → Embed → Reduce → Cluster → Analyze → Merge → Visualize → Export
```

#### 1. Data Loader (`stages/data_loader.py`)
Loads CSV files and creates `RawMessage` objects with metadata preservation.

**Key features:**
- Reads CSV with configurable column mappings
- Preserves all demographic fields in metadata
- Handles multiple CSV files (r1, r2, consolidated)

#### 2. Content Filter (`stages/content_filter.py`)
Rule-based filtering to remove non-substantive messages.

**Filters:**
- STOP/unsubscribe messages
- Emoji-only reactions
- Profanity (optional)
- Messages below minimum length

#### 3. AI Message Processor (`stages/ai_message_processor.py`)
LLM-powered message cleaning and splitting.

**Operations:**
- Grammar correction and standardization
- Compound message splitting ("Fix roads. Also schools." → 2 messages)
- Keyword anonymization (campaign-specific terms)

**Batch processing:** 50 messages per LLM call

#### 4. Embedding Generator (`stages/embedding_generator.py`)
Generates 3072-dimensional semantic embeddings via Gemini API.

**Output:** `EmbeddedMessage` objects with high-dimensional vectors

#### 5. Dimensionality Reducer (`stages/dimensionality_reducer.py`)
Two-stage reduction for efficient clustering.

**Stage 1:** PCA (3072d → 50d, preserves ~95% variance)
**Stage 2:** UMAP (50d → 15d, preserves local structure)

**Configuration:**
```yaml
dimensionality_reduction:
  pca_dimensions: 50
  umap_dimensions: 15
  umap_n_neighbors: 15
  umap_min_dist: 0.0        # Tighter clusters for hierarchical
  umap_metric: "cosine"     # Semantic similarity
```

#### 6. Hierarchical Cluster Engine (`stages/hierarchical_cluster_engine.py`)
Performs hierarchical clustering with optimal K selection.

**Algorithm:** Complete linkage with cosine distance
- Deterministic (no random initialization)
- Produces balanced, interpretable clusters
- Generates dendrogram for visualization

**Optimal K Selection** (`find_optimal_k.py`):
Tests k values from min_k to max_k, scoring each by:

**Separation (50% weight)** - Between/within cluster variance ratio
- B/W ≥ 50: 50 pts (exceptional)
- B/W ≥ 20: 45 pts (outstanding)
- B/W ≥ 10: 40 pts (excellent)
- B/W < 1.5: penalty

**Cohesion (30% weight)** - Silhouette score
- ≥ 0.8: 30 pts (excellent)
- ≥ 0.7: 25 pts (very good)
- ≥ 0.6: 20 pts (good)

**Balance (20% weight)** - Cluster size distribution
- Low CV + no dominant cluster: 20 pts
- Accepts imbalance if separation is high

**Philosophy:** Cluster size doesn't matter. Separation does.
- If 400 people say "taxes" → ONE cluster (high B/W ratio) ✅
- If 400 people have mixed concerns → MULTIPLE clusters (low B/W ratio) ❌

See `OPTIMAL_K_APPROACH.md` for detailed explanation.

#### 7. Multi-Cluster Analyzer (`stages/multi_cluster_analyzer.py`)
Parallel LLM-powered theme extraction with integrated cluster merging.

**For each cluster, generates:**
- Theme (concise label)
- Category (high-level classification)
- Sentiment (emotional tone)
- Key topics (extracted concepts)
- Detailed analysis (comprehensive summary)
- Action items (3-5 specific recommendations)
- Representative quotes (verbatim examples with phone attribution)

**Parallelization:**
- 50 concurrent LLM calls (default)
- ThreadPoolExecutor + asyncio.gather
- Typical speed: 20-30 seconds for 24 clusters

**Quote Attribution:**
- Fuzzy string matching to link quotes → phone numbers
- Preserves original message alongside cleaned version
- 100% attribution accuracy

#### 8. Integrated Cluster Merger (`stages/cluster_merger.py`)
Two-stage merging to consolidate similar themes (called automatically within stage 7).

**Stage 1: Embedding Pre-filter**
- Cosine similarity between 3072d cluster centroids
- Threshold: 0.92 (configurable)
- Fast operation, reduces LLM calls by ~90%

**Stage 2: LLM Validation (All-at-Once)**
- Sends all candidate clusters to LLM in single call
- LLM groups clusters into merge groups
- Uses structured Pydantic output for type safety
- Prevents false merges from embedding similarity alone

**Merge Strategy:**
- Merges smaller clusters into larger ones
- Updates themes with LLM-generated merged names
- Preserves original cluster assignments for accountability

#### 9. Dendrogram + Visualization (`stages/dendrogram_generator.py`, `stages/visualization_generator.py`)
Creates hierarchical tree visualizations and statistical charts.

**Outputs:**
- Dendrogram showing cluster merge structure
- Cluster size distribution histogram
- Sentiment breakdown by cluster
- Category distribution pie chart

#### 10. Export Results (`utils/multi_cluster_exporter.py`)
Exports results in multiple formats.

**Outputs:**
1. Multi-cluster CSV (complete dataset with themes)
2. Cluster analysis JSON (structured for v1_pipeline)
3. Markdown reports (optimal K rationale, statistics)

## Configuration

### Core Settings (`config.yaml`)

**Hierarchical Clustering:**
```yaml
hierarchical:
  linkage: "complete"              # ward, complete, average, single
  affinity: "cosine"               # euclidean, manhattan, cosine

  multi_cluster_analysis: true     # Enable multi-cluster mode
  cluster_ranges: "optimal_k"      # "optimal_k", "auto", or list [10, 20, 30]

  optimal_k_config:
    enabled: true
    min_k: 5                       # Minimum clusters to test
    max_k: 50                      # Maximum clusters to test
    min_cluster_size: 5            # Minimum messages per cluster
    min_substantial_clusters: 3    # Min substantial clusters required
    max_cv: 1.0                    # Max coefficient of variation
    max_bw_ratio: 1000.0           # Upper bound for B/W ratio
    zero_epsilon: 1.0e-10          # Epsilon for near-zero checks
```

**Dimensionality Reduction:**
```yaml
dimensionality_reduction:
  pca_dimensions: 50               # 3072d → 50d
  umap_dimensions: 15              # 50d → 15d
  umap_n_neighbors: 15
  umap_min_dist: 0.0
  umap_metric: "cosine"
```

**Cluster Analysis:**
```yaml
analysis:
  parallel_analysis: true
  max_workers: 50                  # Concurrent LLM calls
  max_example_messages: 50
  save_example_messages: 5

  llm_config:
    model: "flash"                 # Gemini Flash
    temperature: 0.0               # Deterministic
    thinking_budget: 0             # No extended thinking
```

**Cluster Merger:**
```yaml
cluster_merger:
  enabled: true
  embedding_similarity_threshold: 0.92  # Pre-filter
  similarity_threshold: 0.8             # LLM merge threshold
  max_workers: 10
```

## Architecture

### Data Models

Messages transform through six types:

```python
RawMessage          # Original CSV data
   ↓
FilteredMessage     # After content filtering
   ↓
AtomicMessage       # After AI cleaning/splitting
   ↓
EmbeddedMessage     # With semantic embeddings (3072d, 50d, 15d)
   ↓
ClusteredMessage    # With cluster assignments
   ↓
ClusterAnalysis     # With AI-generated themes
```

### Metadata Preservation

Critical for phone attribution:

```python
RawMessage.metadata = {
    'Contact Phone Number': '12485619334',
    'Campaign ID': 'berkeley',
    # ... other CSV fields
}

# Metadata flows through all transformations
ClusteredMessage.metadata  # Same metadata preserved
```

Ensures quotes can be attributed to phone numbers with 100% accuracy.

### Refactored Orchestrator

The main orchestrator (`orchestrator.py`) was recently refactored from 1,813 lines to 428 lines (76% reduction) by extracting utilities into `utils/`:

**Utility Modules:**
- `helpers.py` - CSV serialization, coordinate extraction
- `config_manager.py` - YAML configuration loading
- `output_manager.py` - Directory structure setup
- `cost_tracker.py` - API cost aggregation
- `single_message_analyzer.py` - LLM analysis for single messages
- `cluster_range_selector.py` - Intelligent cluster range selection
- `multi_cluster_output_builder.py` - Consolidated output structures
- `multi_cluster_exporter.py` - CSV export with pandas
- `visualization_orchestrator.py` - Dendrogram/visualization coordination
- `report_generator.py` - Markdown report generation

All utilities are async-compatible and accept `pipeline_state` and `config` as parameters.

## Output Files

### Multi-Cluster Analysis CSV

Columns per k value:
- `cluster_{k}`: Cluster ID
- `theme_{k}`: AI-generated theme
- `category_{k}`: High-level category
- `sentiment_{k}`: Emotional tone
- `key_topics_{k}`: Comma-separated topics
- `detailed_analysis_{k}`: Full analysis
- `verbatim_quotes_{k}`: Representative quotes
- `quotes_{k}`: JSON with phone attribution

Plus: `phone_number`, `message`, `atomic_message`, demographics

### Cluster Analysis JSON

Structure per cluster:
```json
{
  "cluster_id": 23,
  "size": 12,
  "theme_analysis": {
    "theme": "Road and Sidewalk Repair",
    "summary": "Citizens reporting infrastructure issues",
    "key_topics": ["road conditions", "street repair"],
    "sentiment": "concerned",
    "category": "Infrastructure",
    "civic_relevance": "Maintaining public infrastructure",
    "confidence_score": 0.9,
    "quotes": [
      {
        "quote": "Side streets are in horrible shape.",
        "phone_number": "12485619334",
        "original_message": "Side streets are in horrible shape. ",
        "atomic_message": "Side streets are in horrible shape."
      }
    ]
  }
}
```

### Optimal K Report

Markdown with:
- Selected k value and quality metrics
- Comparison table for all tested k values
- Cluster size distribution statistics
- Rationale for selection

## Performance

### Typical Processing Times

For 175 messages with k=24:

| Stage | Time | Notes |
|-------|------|-------|
| Data Loading | 0.5s | CSV parsing |
| Content Filter | 0.3s | Rule-based |
| AI Processing | 15-20s | LLM batches |
| Embedding | 10-15s | Gemini API |
| Dimensionality Reduction | 2-3s | PCA + UMAP |
| Optimal K Selection | 5-10s | Tests k=5-50 |
| Cluster Analysis | 20-30s | 50 parallel LLM |
| Cluster Merger | 5-10s | LLM similarity |
| Visualization | 2-3s | Matplotlib |
| **Total** | **~2 minutes** | End-to-end |

### Cost Estimates

Using Gemini Flash ($0.075/1M input, $0.30/1M output):

| Stage | Input | Output | Cost |
|-------|-------|--------|------|
| Message Cleaning | 50K | 30K | $0.013 |
| Embeddings | 25K | - | $0.002 |
| Theme Analysis | 200K | 50K | $0.030 |
| Merger | 30K | 5K | $0.004 |
| **Total (175 msgs)** | **305K** | **85K** | **~$0.049** |

### Scalability

| Messages | Time | Cost | Optimal k |
|----------|------|------|-----------|
| 100 | ~1.5 min | $0.03 | 12-18 |
| 200 | ~2.5 min | $0.06 | 20-30 |
| 500 | ~5 min | $0.15 | 35-45 |
| 1000 | ~10 min | $0.30 | 45-60 |

## Integration with V1 Pipeline

The v1_pipeline consumes this code via `ClusteringAdapter`:

```python
# v1_pipeline/adapters/clustering_adapter.py

class ClusteringAdapter:
    async def process_messages(self,
                               messages: List[ConsolidatedMessage],
                               campaign_name: str) -> Dict:
        # Convert to RawMessage objects
        raw_messages = self._convert_to_raw_messages(messages, campaign_name)

        # Initialize orchestrator
        orchestrator = HierarchicalDiscoveryOrchestrator(
            config_path=self.config_path,
            data_source_override=campaign_name
        )

        # Run in-memory (no CSV intermediaries)
        result = await orchestrator.run_multi_cluster_pipeline(
            return_data=True,
            in_memory_messages=raw_messages
        )

        return self._parse_clustering_results_from_objects(result)
```

**Benefits:**
- In-memory processing
- Automatic phone attribution
- Same algorithms as standalone
- Configurable via config.yaml

**V1 Pipeline Flow:**
```
Consolidation → Classification → Clustering (this) → DynamoDB Upload
```

## Troubleshooting

### "Optimal k selection failed - no valid k found"

**Cause:** All tested k values fail constraints

**Solution:** Relax constraints
```yaml
optimal_k_config:
  min_cluster_size: 3      # Lower from 5
  max_cv: 1.5              # Increase from 1.0
```

### "UMAP fails with 'n_neighbors too large'"

**Cause:** Dataset smaller than n_neighbors

**Solution:** Reduce n_neighbors
```yaml
dimensionality_reduction:
  umap_n_neighbors: 5      # Lower from 15
```

### "Out of memory during clustering"

**Cause:** High-dimensional embeddings

**Solution:** Reduce dimensions
```yaml
dimensionality_reduction:
  pca_dimensions: 30       # Lower from 50
  umap_dimensions: 8       # Lower from 15
```

### "LLM rate limit errors"

**Cause:** Too many parallel calls

**Solution:** Reduce workers
```yaml
analysis:
  max_workers: 20          # Lower from 50
cluster_merger:
  max_workers: 5           # Lower from 10
```

## File Structure

```
serve/hierarchical_discovery/
├── config.yaml                      # Configuration
├── orchestrator.py                  # Main coordinator (428 lines)
├── models.py                        # Data models
├── find_optimal_k.py                # Optimal K algorithm
├── run_pipeline.py                  # CLI entry point
├── OPTIMAL_K_APPROACH.md            # Separation-first philosophy
├── utils/                           # Utility modules
│   ├── __init__.py
│   ├── helpers.py
│   ├── config_manager.py
│   ├── output_manager.py
│   ├── cost_tracker.py
│   ├── single_message_analyzer.py
│   ├── cluster_range_selector.py
│   ├── multi_cluster_output_builder.py
│   ├── multi_cluster_exporter.py
│   ├── visualization_orchestrator.py
│   └── report_generator.py
├── stages/                          # Pipeline stages
│   ├── data_loader.py
│   ├── content_filter.py
│   ├── ai_message_processor.py
│   ├── embedding_generator.py
│   ├── dimensionality_reducer.py
│   ├── hierarchical_cluster_engine.py
│   ├── multi_cluster_analyzer.py
│   ├── cluster_merger.py
│   ├── cluster_merger_analysis.py
│   ├── dendrogram_generator.py
│   └── visualization_generator.py
├── _archive/                        # Archived code (unused)
│   ├── README.md
│   ├── cluster_analyzer.py
│   └── accountability_exporter.py
└── output/                          # Generated results
    ├── exports/
    ├── reports/
    ├── visualizations/
    ├── dendrograms/
    └── checkpoints/
```

## Key Concepts

### Separation-First Optimal K

**Philosophy:** Cluster size doesn't matter. Separation does.

Traditional approaches:
- Find elbow points (diminishing returns)
- Enforce cluster size constraints (30-100 messages)
- Penalize outliers

**Our approach:**
- Maximize B/W ratio (theme distinctness)
- Natural cluster sizes (some large, some small)
- No arbitrary size limits

**Example:**
- 400 messages about "property taxes" = ONE cluster (B/W=45) ✅
- 400 messages about mixed topics = split into MULTIPLE (B/W=1.3) ❌

The B/W ratio tells us which case we're in.

**Why this works:**
- High B/W = themes are distinct → cluster size is correct by definition
- Low B/W = themes overlap → need more splitting
- No guessing, just measuring separation

See `OPTIMAL_K_APPROACH.md` for detailed explanation.

### Complete Linkage vs Ward

**Complete Linkage (default):**
- Minimizes maximum distance between clusters
- Produces more compact, uniform clusters
- Better for text/semantic data
- Less sensitive to outliers

**Ward Linkage:**
- Minimizes within-cluster variance
- Produces hierarchical structure
- Better for numerical/geometric data
- Can create unbalanced clusters

We use complete linkage with cosine distance for semantic messages.

### Two-Stage Cluster Merging

**Why two stages?**

Embedding similarity alone causes false positives:
- "Road repair" and "School funding" may have similar embeddings
- But they're semantically distinct themes

**Stage 1:** Fast embedding pre-filter (eliminates 90% of comparisons)
**Stage 2:** LLM validation (prevents false merges)

Result: Only truly similar themes are merged.

## Braintrust Integration (LLM Observability)

The pipeline integrates with [Braintrust](https://www.braintrust.dev/) for LLM observability and prompt evaluation.

### Automatic Enablement

Braintrust logging automatically enables when `BRAINTRUST_API_KEY` is set in your environment:

```bash
# Add to your .env file
BRAINTRUST_API_KEY=your-braintrust-api-key
BRAINTRUST_PROJECT=hierarchical-discovery  # Optional, defaults to "hierarchical-discovery"
```

No code changes or configuration flags needed. If the API key is not set, the pipeline runs normally without logging.

### What Gets Logged

Cluster analysis LLM calls are logged with structured input/output:

**Input (shows in Braintrust UI as JSON):**
- `cluster_info`: cluster_id, total_messages, unique_citizens, coverage metrics
- `messages`: Sample of 10 messages from the cluster

**Output:**
- Complete `ClusterAnalysisResponse`: theme, category, sentiment, key_topics, action_items, etc.

**Metadata:**
- Full prompt text (for debugging)
- cluster_id, total_clusters, cluster_size
- Duration in milliseconds

### Viewing Logs

After running the pipeline:

1. Go to [https://www.braintrust.dev/](https://www.braintrust.dev/)
2. Navigate to your project (default: "hierarchical-discovery")
3. View logs with structured input/output side-by-side
4. Filter by tags: `cluster-analysis`, `k-{cluster_count}`

### Prompt Management (Optional)

Prompts can be managed in Braintrust's UI for iteration without code changes:

1. Create a prompt in Braintrust with slug `cluster-analysis-v1`
2. The pipeline will load it automatically (falls back to local if not found)
3. Edit prompts in the UI and re-run to test changes

## Dependencies

**Core Libraries:**
- `scikit-learn` - Clustering, PCA, metrics
- `umap-learn` - UMAP dimensionality reduction
- `scipy` - Hierarchical clustering, dendrograms
- `numpy` - Numerical operations
- `pandas` - Data manipulation
- `matplotlib` - Visualizations
- `google-generativeai` - Gemini LLM and embeddings

**Shared Modules:**
- `shared.llm_gemini` - Gemini client with cost tracking
- `shared.logger` - Environment-aware logging

## References

**Clustering:**
- Ward, J. H. (1963). "Hierarchical Grouping to Optimize an Objective Function"
- Müllner, D. (2011). "Modern hierarchical, agglomerative clustering algorithms"

**Dimensionality Reduction:**
- McInnes, L., et al. (2018). "UMAP: Uniform Manifold Approximation and Projection"
- Jolliffe, I. T. (2002). "Principal Component Analysis"

**Metrics:**
- Rousseeuw, P. J. (1987). "Silhouettes: A graphical aid to interpretation"
- Caliński, T., & Harabasz, J. (1974). "A dendrite method for cluster analysis"

---

**Version:** 1.0 (Production-Ready)
**Last Updated:** October 2025
**Maintainer:** GoodParty.org AI Team
