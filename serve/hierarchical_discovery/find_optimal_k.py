#!/usr/bin/env python3

"""
Optimal K Finder - Separation-First Design

Core Insight (from user feedback):
  "If 400 people say taxes, that's ONE theme, not 'too big'."

Key Realization:
  CLUSTER SIZE DOESN'T MATTER. SEPARATION DOES.

  A cluster of 400 messages about "property taxes" is GOOD.
  A cluster of 400 messages about "various concerns" is BAD (needs splitting).

  The difference? SEPARATION.
  - Good clustering: High B/W ratio (themes are distinct)
  - Bad clustering: Low B/W ratio (themes overlap)

New Design Philosophy:
  1. Separation (B/W ratio) - HIGHEST PRIORITY (50 pts)
     • If themes are distinct, cluster size is correct by definition
     • If themes overlap, need more splitting regardless of size

  2. Cohesion (Silhouette) - SECOND PRIORITY (30 pts)
     • Messages within theme should be similar
     • But less important than separation

  3. Balance (CV, distribution) - THIRD PRIORITY (20 pts)
     • Prefer even distribution (no one dominant cluster)
     • But willing to accept imbalance if separation is excellent

  4. Cluster size - IGNORED
     • Let data determine natural cluster sizes
     • 400 messages on one topic = correct
     • 10 messages on rare topic = also correct
"""

import numpy as np
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from datetime import datetime
from scipy.cluster.hierarchy import linkage, cut_tree
from sklearn.metrics import silhouette_score
import matplotlib.pyplot as plt

from shared.logger import get_logger

logger = get_logger(__name__)


class OptimalKFinder:
    """
    Optimal k finder that prioritizes separation above all else

    Design principles:
    1. High separation (B/W ratio) = themes are distinct = good clustering
    2. Cluster size doesn't matter (let data determine natural sizes)
    3. NO ELBOWS, NO ARBITRARY CONSTRAINTS
    """

    def __init__(
        self,
        embeddings: np.ndarray,
        min_k: int = 5,
        max_k: int = 50,
        max_bw_ratio: float = 1000.0,
        zero_epsilon: float = 1.0e-10
    ):
        if embeddings is None or len(embeddings) == 0:
            raise ValueError("Embeddings cannot be None or empty")

        if not isinstance(embeddings, np.ndarray):
            raise TypeError(f"Embeddings must be numpy array, got {type(embeddings)}")

        if embeddings.ndim != 2:
            raise ValueError(f"Embeddings must be 2D array, got {embeddings.ndim}D")

        if min_k < 2:
            raise ValueError(f"min_k must be >= 2, got {min_k}")

        if min_k >= max_k:
            raise ValueError(f"min_k ({min_k}) must be < max_k ({max_k})")

        if len(embeddings) < min_k:
            raise ValueError(f"Dataset size ({len(embeddings)}) must be >= min_k ({min_k})")

        self.embeddings = embeddings
        self.max_bw_ratio = max_bw_ratio
        self.zero_epsilon = zero_epsilon
        self.n_samples = len(self.embeddings)

        self.min_k = min_k
        self.max_k = min(max_k, self.n_samples - 1)

        self.linkage_matrix = None
        self.results = []

        self.gap_results = None
        self.stability_scores = None

        logger.info(f"OptimalKFinder initialized:")
        logger.info(f"  Dataset: {self.n_samples:,} messages")
        logger.info(f"  k range: [{self.min_k}, {self.max_k}]")

    def compute_linkage(self, method: str = 'complete', metric: str = 'cosine'):
        """Compute linkage matrix for hierarchical clustering"""
        logger.info(f"Computing linkage matrix: method={method}, metric={metric}")

        if method == 'ward':
            self.linkage_matrix = linkage(self.embeddings, method='ward')
        else:
            self.linkage_matrix = linkage(self.embeddings, method=method, metric=metric)

        logger.info(f"Linkage matrix computed: shape={self.linkage_matrix.shape}")
        return self.linkage_matrix

    def test_k_values(self):
        """Test all k values in range"""
        logger.info(f"Testing k values from {self.min_k} to {self.max_k}")

        for k in range(self.min_k, self.max_k + 1):
            if k >= self.n_samples:
                logger.warning(f"k={k} >= n_samples={self.n_samples}, stopping")
                break

            labels = cut_tree(self.linkage_matrix, n_clusters=k).flatten()
            unique_labels = np.unique(labels)
            n_clusters_actual = len(unique_labels)

            if n_clusters_actual < k:
                logger.warning(f"k={k} produced only {n_clusters_actual} clusters (some empty)")

            # Compute metrics
            silhouette = silhouette_score(self.embeddings, labels, metric='cosine')
            wcscd = self._calculate_wcscd(labels, unique_labels)
            between_within_ratio = self._calculate_between_within_ratio(labels, unique_labels)

            cluster_sizes = [np.sum(labels == label) for label in unique_labels]
            min_size = min(cluster_sizes)
            max_size = max(cluster_sizes)
            mean_size = np.mean(cluster_sizes)
            std_size = np.std(cluster_sizes)
            cv = std_size / mean_size if mean_size > 0 else 0

            result = {
                'k': k,
                'n_clusters_actual': n_clusters_actual,
                'silhouette': silhouette,
                'wcscd': wcscd,
                'between_within_ratio': between_within_ratio,
                'cluster_sizes': cluster_sizes,
                'min_cluster_size': min_size,
                'max_cluster_size': max_size,
                'mean_cluster_size': mean_size,
                'std_cluster_size': std_size,
                'cv': cv
            }

            self.results.append(result)

            if k % 10 == 0:
                logger.info(f"k={k}: B/W={between_within_ratio:.2f}, sil={silhouette:.3f}, "
                          f"mean_size={mean_size:.1f}, CV={cv:.2f}")

        logger.info(f"Tested {len(self.results)} k values")
        return self.results

    def _calculate_wcscd(self, labels: np.ndarray, unique_labels: np.ndarray) -> float:
        """Calculate Within-Cluster Sum of Cosine Distances"""
        from sklearn.metrics.pairwise import cosine_distances

        total_wcscd = 0.0
        for label in unique_labels:
            cluster_points = self.embeddings[labels == label]
            if len(cluster_points) > 1:
                distances = cosine_distances(cluster_points)
                total_wcscd += np.sum(np.triu(distances, k=1))

        return total_wcscd

    def _calculate_between_within_ratio(self, labels: np.ndarray, unique_labels: np.ndarray) -> float:
        """Calculate Between-Cluster to Within-Cluster distance ratio"""
        from sklearn.metrics.pairwise import cosine_distances

        if len(unique_labels) < 2:
            return 0.0

        centroids = []
        for label in unique_labels:
            cluster_points = self.embeddings[labels == label]
            centroid = np.mean(cluster_points, axis=0)
            centroids.append(centroid)

        centroids = np.array(centroids)
        between_distances = cosine_distances(centroids)
        between_cluster_distance = np.sum(np.triu(between_distances, k=1))

        within_cluster_distance = 0.0
        for label in unique_labels:
            cluster_points = self.embeddings[labels == label]
            if len(cluster_points) > 1:
                centroid = np.mean(cluster_points, axis=0)
                distances = cosine_distances(cluster_points, centroid.reshape(1, -1))
                within_cluster_distance += np.sum(distances)

        if within_cluster_distance == 0 or within_cluster_distance < self.zero_epsilon:
            return self.max_bw_ratio

        return between_cluster_distance / within_cluster_distance

    def apply_constraints(self, max_cv: float = 2.0, min_silhouette: float = 0.3, min_bw_ratio: float = 0.5) -> List[Dict]:
        """
        Apply minimal hard constraints

        Parameters:
            max_cv: Maximum coefficient of variation (default 2.0)
            min_silhouette: Minimum silhouette score (default 0.3)
            min_bw_ratio: Minimum B/W separation ratio (default 0.5)

        Only reject obviously broken clusterings:
        1. Silhouette >= min_silhouette (minimum coherence)
        2. B/W ratio >= min_bw_ratio (some separation required)
        3. CV <= max_cv (not extremely unbalanced)
        """
        valid_results = []

        for result in self.results:
            # Minimal constraints
            if result['silhouette'] < min_silhouette:
                continue  # Very poor quality
            if result['between_within_ratio'] < min_bw_ratio:
                continue  # Essentially no separation
            if result['cv'] > max_cv:
                continue  # Extremely unbalanced

            valid_results.append(result)

        logger.info(f"Applied minimal constraints:")
        logger.info(f"  Silhouette >= {min_silhouette} (coherence)")
        logger.info(f"  B/W >= {min_bw_ratio} (separation)")
        logger.info(f"  CV <= {max_cv} (balance)")
        logger.info(f"  Valid k values: {len(valid_results)}/{len(self.results)}")

        return valid_results

    def recommend_optimal_k(self, valid_results: Optional[List[Dict]] = None) -> Tuple[int, str]:
        """
        Recommend optimal k using separation-first scoring

        Scoring (out of 100):
        1. SEPARATION (50 pts) - Are themes DISTINCT?
        2. COHESION (30 pts) - Are messages SIMILAR within themes?
        3. BALANCE (20 pts) - Is distribution REASONABLE?

        NO CLUSTER SIZE CONSTRAINTS.
        If 400 people say "taxes," that's ONE theme (correct).
        If 10 people say "bike lanes," that's ONE theme (also correct).
        """
        if valid_results is None:
            valid_results = self.apply_constraints()

        if not valid_results:
            logger.error("No valid k values after applying constraints")
            return None, "No valid k values found"

        logger.info("\n" + "="*80)
        logger.info("SEPARATION-FIRST OPTIMAL K SELECTION")
        logger.info("="*80)
        logger.info(f"Dataset: {self.n_samples:,} messages")
        logger.info(f"Valid candidates: {len(valid_results)}")
        logger.info("")

        # Scoring
        candidates = {}

        for result in valid_results:
            k = result['k']
            score = 0
            reasons = []

            # ================================================================
            # 1. SEPARATION - B/W Ratio (0-50 points) - HIGHEST PRIORITY
            # ================================================================
            bw_ratio = result['between_within_ratio']

            if bw_ratio >= 50.0:
                separation_score = 50
                reasons.append(f"🔥 EXCEPTIONAL separation (B/W={bw_ratio:.1f})")
            elif bw_ratio >= 20.0:
                separation_score = 45
                reasons.append(f"🔥 OUTSTANDING separation (B/W={bw_ratio:.1f})")
            elif bw_ratio >= 10.0:
                separation_score = 40
                reasons.append(f"🔥 EXCELLENT separation (B/W={bw_ratio:.1f})")
            elif bw_ratio >= 5.0:
                separation_score = 35
                reasons.append(f"Excellent separation (B/W={bw_ratio:.1f})")
            elif bw_ratio >= 3.0:
                separation_score = 30
                reasons.append(f"Very good separation (B/W={bw_ratio:.1f})")
            elif bw_ratio >= 2.0:
                separation_score = 25
                reasons.append(f"Good separation (B/W={bw_ratio:.1f})")
            elif bw_ratio >= 1.5:
                separation_score = 15
                reasons.append(f"Moderate separation (B/W={bw_ratio:.1f})")
            elif bw_ratio >= 1.0:
                separation_score = 5
                reasons.append(f"Weak separation (B/W={bw_ratio:.1f})")
            else:
                separation_score = 0
                reasons.append(f"⚠️ Very weak separation (B/W={bw_ratio:.2f})")

            score += separation_score

            # ================================================================
            # 2. COHESION - Silhouette (0-30 points) - SECOND PRIORITY
            # ================================================================
            silhouette = result['silhouette']

            if silhouette >= 0.8:
                cohesion_score = 30
                reasons.append(f"Exceptional cohesion (sil={silhouette:.3f})")
            elif silhouette >= 0.7:
                cohesion_score = 25
                reasons.append(f"Excellent cohesion (sil={silhouette:.3f})")
            elif silhouette >= 0.6:
                cohesion_score = 20
                reasons.append(f"Very good cohesion (sil={silhouette:.3f})")
            elif silhouette >= 0.5:
                cohesion_score = 15
                reasons.append(f"Good cohesion (sil={silhouette:.3f})")
            elif silhouette >= 0.4:
                cohesion_score = 10
                reasons.append(f"Moderate cohesion (sil={silhouette:.3f})")
            else:
                cohesion_score = 5
                reasons.append(f"Adequate cohesion (sil={silhouette:.3f})")

            score += cohesion_score

            # ================================================================
            # 3. BALANCE - Distribution (0-20 points) - THIRD PRIORITY
            # ================================================================
            cv = result['cv']
            max_cluster_pct = (result['max_cluster_size'] / self.n_samples) * 100

            balance_score = 0

            # CV component (0-10 points)
            if cv < 0.3:
                balance_score += 10
                reasons.append(f"Excellent balance (CV={cv:.2f})")
            elif cv < 0.5:
                balance_score += 7
                reasons.append(f"Very good balance (CV={cv:.2f})")
            elif cv < 0.7:
                balance_score += 5
                reasons.append(f"Good balance (CV={cv:.2f})")
            elif cv < 1.0:
                balance_score += 3
                reasons.append(f"Moderate balance (CV={cv:.2f})")

            # Max cluster component (0-10 points)
            # Only penalize if ONE cluster dominates (>50% of data)
            if max_cluster_pct < 20:
                balance_score += 10  # No single dominant cluster
            elif max_cluster_pct < 30:
                balance_score += 7
            elif max_cluster_pct < 40:
                balance_score += 5
            elif max_cluster_pct < 50:
                balance_score += 3
            else:
                balance_score += 0  # One cluster has >50% of data (but may be correct!)
                reasons.append(f"Note: One large cluster ({max_cluster_pct:.0f}% of data)")

            score += balance_score

            candidates[k] = {
                'score': score,
                'reasons': reasons,
                'result': result,
                'breakdown': {
                    'separation': separation_score,
                    'cohesion': cohesion_score,
                    'balance': balance_score
                }
            }

        # Select best k
        best_k = max(candidates.keys(), key=lambda k: candidates[k]['score'])
        best_info = candidates[best_k]

        # Build detailed reasoning
        reasoning_lines = [
            "="*80,
            f"🎯 OPTIMAL K* RECOMMENDATION: k={best_k}",
            "="*80,
            "",
            f"Dataset: {self.n_samples:,} messages",
            f"Final Score: {best_info['score']:.0f} / 100 points",
            "",
            "Score Breakdown:",
            f"  • Separation (B/W ratio):  {best_info['breakdown']['separation']:.0f} / 50 points",
            f"  • Cohesion (silhouette):   {best_info['breakdown']['cohesion']:.0f} / 30 points",
            f"  • Balance (distribution):  {best_info['breakdown']['balance']:.0f} / 20 points",
            "",
            "Why This k*:",
        ]

        for reason in best_info['reasons']:
            reasoning_lines.append(f"  ✓ {reason}")

        reasoning_lines.extend([
            "",
            "📊 Quality Metrics:",
            f"  • B/W Separation Ratio:  {best_info['result']['between_within_ratio']:.2f}",
            f"  • Silhouette Score:      {best_info['result']['silhouette']:.4f}",
            f"  • Mean Cluster Size:     {best_info['result']['mean_cluster_size']:.1f} messages",
            f"  • Size Range:            {best_info['result']['min_cluster_size']}-{best_info['result']['max_cluster_size']} messages",
            f"  • CV (balance):          {best_info['result']['cv']:.3f}",
            f"  • Largest cluster:       {(best_info['result']['max_cluster_size']/self.n_samples*100):.1f}% of data",
            "",
            "🏅 Top 5 Alternatives:",
        ])

        sorted_candidates = sorted(candidates.items(), key=lambda x: x[1]['score'], reverse=True)
        for k, info in sorted_candidates[1:6]:
            reasoning_lines.append(
                f"  • k={k:2d} (score={info['score']:5.1f}): "
                f"B/W={info['result']['between_within_ratio']:6.1f}, "
                f"sil={info['result']['silhouette']:.3f}, "
                f"mean_size={info['result']['mean_cluster_size']:5.1f}"
            )

        reasoning_lines.extend([
            "",
            "="*80
        ])

        reasoning = "\n".join(reasoning_lines)
        logger.info(f"\n{reasoning}")

        return best_k, reasoning

    def plot_results(self, output_dir: Path, valid_results: Optional[List[Dict]] = None, dataset_name: str = "",
                     gap_results: Optional[Dict] = None, stability_scores: Optional[Dict] = None):
        """Generate analysis plots focused on separation"""
        output_dir.mkdir(parents=True, exist_ok=True)

        if valid_results is None:
            valid_results = self.apply_constraints()

        all_k = [r['k'] for r in self.results]
        valid_k = [r['k'] for r in valid_results]

        fig, axes = plt.subplots(2, 3, figsize=(24, 12))

        # B/W Ratio plot (MOST IMPORTANT)
        bw_vals = [r['between_within_ratio'] for r in self.results]
        axes[0, 0].plot(all_k, bw_vals, 'r-', linewidth=3, label='B/W Ratio')
        axes[0, 0].scatter(valid_k, [r['between_within_ratio'] for r in valid_results],
                          c='green', s=100, zorder=5, label='Valid k')
        axes[0, 0].axhline(20, color='purple', linestyle='--', alpha=0.5, label='Exceptional (>20)')
        axes[0, 0].axhline(10, color='blue', linestyle='--', alpha=0.5, label='Excellent (>10)')
        axes[0, 0].axhline(2, color='orange', linestyle='--', alpha=0.5, label='Good (>2)')
        axes[0, 0].set_xlabel('Number of Clusters (k)', fontsize=12)
        axes[0, 0].set_ylabel('B/W Separation Ratio', fontsize=12)
        axes[0, 0].set_title('🔥 SEPARATION (B/W Ratio) vs k - HIGHEST PRIORITY', fontsize=14, fontweight='bold')
        axes[0, 0].legend()
        axes[0, 0].grid(True, alpha=0.3)

        # Silhouette plot
        silhouette_vals = [r['silhouette'] for r in self.results]
        axes[0, 1].plot(all_k, silhouette_vals, 'b-', linewidth=2, label='Silhouette')
        axes[0, 1].scatter(valid_k, [r['silhouette'] for r in valid_results],
                          c='green', s=50, zorder=5, label='Valid k')
        axes[0, 1].axhline(0.7, color='green', linestyle='--', alpha=0.5, label='Excellent (>0.7)')
        axes[0, 1].axhline(0.5, color='orange', linestyle='--', alpha=0.5, label='Good (>0.5)')
        axes[0, 1].set_xlabel('Number of Clusters (k)')
        axes[0, 1].set_ylabel('Silhouette Score')
        axes[0, 1].set_title('COHESION (Silhouette) vs k')
        axes[0, 1].legend()
        axes[0, 1].grid(True, alpha=0.3)

        # CV plot
        cv_vals = [r['cv'] for r in self.results]
        axes[0, 2].plot(all_k, cv_vals, 'purple', linewidth=2, label='CV')
        axes[0, 2].axhline(0.5, color='green', linestyle='--', alpha=0.5, label='Good balance (<0.5)')
        axes[0, 2].axhline(1.0, color='orange', linestyle='--', alpha=0.5, label='Moderate (<1.0)')
        axes[0, 2].scatter(valid_k, [r['cv'] for r in valid_results],
                          c='green', s=50, zorder=5, label='Valid k')
        axes[0, 2].set_xlabel('Number of Clusters (k)')
        axes[0, 2].set_ylabel('Coefficient of Variation')
        axes[0, 2].set_title('BALANCE (CV) vs k')
        axes[0, 2].legend()
        axes[0, 2].grid(True, alpha=0.3)

        # Mean cluster size
        mean_size_vals = [r['mean_cluster_size'] for r in self.results]
        axes[1, 0].plot(all_k, mean_size_vals, 'orange', linewidth=2, label='Mean size')
        axes[1, 0].scatter(valid_k, [r['mean_cluster_size'] for r in valid_results],
                          c='green', s=50, zorder=5, label='Valid k')
        axes[1, 0].set_xlabel('Number of Clusters (k)')
        axes[1, 0].set_ylabel('Mean Cluster Size (messages)')
        axes[1, 0].set_title('Mean Cluster Size vs k (No constraints)')
        axes[1, 0].legend()
        axes[1, 0].grid(True, alpha=0.3)

        # Composite score
        # Recalculate scores for visualization
        composite_scores = []
        for result in self.results:
            k = result['k']
            bw = result['between_within_ratio']
            sil = result['silhouette']
            cv = result['cv']

            # Simplified score calculation
            sep_score = min(50, (bw / 20) * 45) if bw < 20 else 45
            coh_score = min(30, (sil / 0.7) * 25) if sil < 0.7 else 25
            bal_score = 10 if cv < 0.5 else (5 if cv < 1.0 else 0)

            total = sep_score + coh_score + bal_score
            composite_scores.append(total)

        axes[1, 1].plot(all_k, composite_scores, 'green', linewidth=2, label='Composite score')
        axes[1, 1].scatter(valid_k, [composite_scores[all_k.index(k)] for k in valid_k],
                          c='green', s=50, zorder=5, label='Valid k')
        axes[1, 1].set_xlabel('Number of Clusters (k)')
        axes[1, 1].set_ylabel('Composite Score (out of 100)')
        axes[1, 1].set_title('Separation-First Composite Score vs k')
        axes[1, 1].legend()
        axes[1, 1].grid(True, alpha=0.3)

        # Philosophy text
        philosophy_text = (
            "SEPARATION-FIRST PHILOSOPHY\n\n"
            "1. SEPARATION (50%) - B/W Ratio\n"
            "   • >20: Outstanding\n"
            "   • >10: Excellent\n"
            "   • >2: Good\n\n"
            "2. COHESION (30%) - Silhouette\n"
            "   • >0.7: Excellent\n"
            "   • >0.5: Good\n\n"
            "3. BALANCE (20%) - CV & Distribution\n"
            "   • Prefer even distribution\n"
            "   • But accept imbalance if\n"
            "     separation is high\n\n"
            "NO ELBOWS.\n"
            "NO SIZE LIMITS.\n"
            "Just maximize separation."
        )
        axes[1, 2].text(0.5, 0.5, philosophy_text,
                       ha='center', va='center', transform=axes[1, 2].transAxes,
                       fontsize=11, family='monospace',
                       bbox=dict(boxstyle='round', facecolor='wheat', alpha=0.5))
        axes[1, 2].axis('off')
        axes[1, 2].set_title('Design Philosophy')

        plt.tight_layout()

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        dataset_prefix = f"{dataset_name}_" if dataset_name else ""
        plot_file = output_dir / f"separation_first_analysis_{dataset_prefix}{timestamp}.png"
        plt.savefig(plot_file, dpi=300, bbox_inches='tight')
        plt.close()

        logger.info(f"Separation-first analysis plots saved: {plot_file}")
        return plot_file

    def save_report(self, output_dir: Path, optimal_k: int, reasoning: str, dataset_name: str = ""):
        """Save separation-first optimal k recommendation report"""
        output_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        dataset_prefix = f"{dataset_name}_" if dataset_name else ""

        import pandas as pd
        results_df = pd.DataFrame(self.results)
        csv_file = output_dir / f"k_comparison_table_{dataset_prefix}{timestamp}.csv"
        results_df.to_csv(csv_file, index=False)
        logger.info(f"Results table saved: {csv_file}")

        report_file = output_dir / f"separation_first_optimal_k_{dataset_prefix}{timestamp}.md"
        with open(report_file, 'w') as f:
            f.write(f"# Separation-First Optimal K Selection Report\n\n")
            f.write(f"**Dataset Size**: {self.n_samples:,} messages\n")
            f.write(f"**K Range Tested**: {self.min_k} to {self.max_k}\n")
            f.write(f"**Method**: Separation-First (No Size Limits)\n\n")
            f.write(f"---\n\n")
            f.write(f"## Recommendation\n\n")
            f.write(f"```\n{reasoning}\n```\n\n")

        logger.info(f"Separation-first report saved: {report_file}")

        return csv_file, report_file
