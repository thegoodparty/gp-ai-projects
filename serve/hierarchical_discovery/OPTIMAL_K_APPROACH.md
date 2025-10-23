# Optimal K Selection: Separation-First Approach

## Overview

Our optimal k selection prioritizes **separation over cluster size**. This approach finds the natural number of distinct themes in civic messages by maximizing the between-cluster to within-cluster variance ratio (B/W ratio).

## Core Philosophy

**Cluster size doesn't matter. Separation does.**

- If 400 people say "taxes" → ONE cluster (high B/W ratio) ✅
- If 400 people have mixed concerns → MULTIPLE clusters (low B/W ratio) ❌
- The B/W ratio tells us which case we're in

## Scoring System (100 points)

### 1. SEPARATION (50 points) - Highest Priority
Are themes DISTINCT from each other?

- B/W ≥ 50: 50 pts (exceptional - themes very distinct)
- B/W ≥ 20: 45 pts (outstanding)
- B/W ≥ 10: 40 pts (excellent)
- B/W ≥ 5:  35 pts (very good)
- B/W ≥ 3:  30 pts (good)
- B/W ≥ 2:  25 pts (moderate)
- B/W < 1.5: 0-15 pts (weak - themes overlap)

### 2. COHESION (30 points) - Second Priority
Are messages SIMILAR within themes?

- Silhouette ≥ 0.8: 30 pts (excellent)
- Silhouette ≥ 0.7: 25 pts (very good)
- Silhouette ≥ 0.6: 20 pts (good)
- Silhouette ≥ 0.5: 15 pts (acceptable)

### 3. BALANCE (20 points) - Third Priority
Is the distribution REASONABLE?

- Low CV + no dominant cluster: 20 pts
- Moderate CV + balanced: 14 pts
- High CV but acceptable: 6 pts

**Note**: We accept imbalance if separation is high.

## What We DON'T Use

❌ **NO ELBOWS** - Elbows find "diminishing returns," not "best clustering"
❌ **NO CLUSTER SIZE CONSTRAINTS** - Let data determine natural sizes
❌ **NO CONSENSUS RANGES** - No arbitrary penalties for being outside a range

## Implementation

**File**: `find_optimal_k.py`
**Class**: `OptimalKFinder`

### Usage

```python
from serve.hierarchical_discovery.find_optimal_k import OptimalKFinder

finder = OptimalKFinder(embeddings, min_k=5, max_k=50)
finder.compute_linkage(method='complete', metric='cosine')
finder.evaluate_all_k_values()

optimal_k, reasoning = finder.recommend_optimal_k()
```

### Configuration (config.yaml)

```yaml
hierarchical:
  cluster_ranges: "optimal_k"

  optimal_k_config:
    enabled: true
    min_k: 5
    max_k: 50
    min_cluster_size: 5
    min_substantial_clusters: 3
    max_cv: 1.0
```

## Example Results

### Berkeley Dataset (322 messages)
- **Selected**: k=29
- **Score**: 90/100
- **B/W Ratio**: 140.07 (exceptional separation)
- **Silhouette**: 0.714 (excellent cohesion)
- **Mean Cluster Size**: 11.1 messages (naturally varied)
- **Interpretation**: Highly distinct themes with excellent coherence

### Why This Works

Traditional approaches would try to force uniform cluster sizes or find elbow points. Our approach:

1. **Measures actual separation** between themes
2. **Allows natural size variation** (some issues affect many people, others few)
3. **Optimizes for actionability** (distinct themes = clear action items)
4. **Scales automatically** with dataset size

## Key Metrics

- **B/W Ratio**: `variance_between_clusters / variance_within_clusters`
  - Higher = themes more distinct
  - Target: >10 for excellent separation

- **Silhouette Score**: How similar messages are within vs between clusters
  - Range: [-1, 1]
  - Target: >0.6 for good cohesion

- **Coefficient of Variation (CV)**: `std_dev / mean` of cluster sizes
  - Lower = more balanced
  - We accept high CV if separation is excellent

## When to Adjust

**Increase max_k** if:
- Dataset is very large (>2000 messages)
- B/W ratio at max_k is still improving

**Decrease max_k** if:
- Dataset is small (<200 messages)
- Computation time is too long

**Typical ranges**:
- Small datasets (<500 msgs): k=5-30
- Medium datasets (500-2000 msgs): k=10-40
- Large datasets (>2000 msgs): k=15-50

## Historical Context

This approach evolved from analyzing why k=9 was selected for 1,935 Cara messages (creating 215 msg/cluster averages). The old approach:
- Used elbow detection (found diminishing returns at k=9)
- Penalized better options for being outside consensus range
- Ignored separation quality

The separation-first approach correctly identifies k≈25-35 for Cara, with:
- Exceptional B/W ratios (>80)
- Natural cluster sizes (some large, some small)
- Actionable, distinct themes

See `docs_archive/` for detailed historical analysis and evolution of thinking.
