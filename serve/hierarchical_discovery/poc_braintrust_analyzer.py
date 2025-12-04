#!/usr/bin/env python3
"""
POC: Braintrust-Integrated Cluster Analyzer

This is a proof-of-concept that demonstrates how to integrate Braintrust with
the hierarchical discovery pipeline. It wraps the cluster analysis to:

1. Load prompts from Braintrust (with local fallback)
2. Log all LLM calls to Braintrust for evaluation
3. Track inputs/outputs for iterating on prompts

Usage:
    # Set your Braintrust API key
    export BRAINTRUST_API_KEY="your-api-key"
    
    # Run the POC
    python -m serve.hierarchical_discovery.poc_braintrust_analyzer
"""

import asyncio
import os
from typing import List, Dict, Any, Optional
from pydantic import BaseModel, Field

from shared.logger import get_logger
from shared.llm_gemini import GeminiClient, GeminiModelType

from .braintrust_integration import (
    init_braintrust,
    traced_llm_call,
    load_prompt_from_braintrust,
    flush_logs,
    is_enabled,
    CLUSTER_ANALYSIS_PROMPT_TEMPLATE
)

logger = get_logger(__name__)


class ClusterAnalysisResponse(BaseModel):
    """Structured response from cluster analysis LLM call - simplified for POC"""
    theme: str = Field(..., description="2-4 word concise theme/label")
    issues_summary: str = Field(..., description="1 sentence describing core issues")
    detailed_analysis: str = Field(..., description="2-3 paragraphs analyzing concerns")


class BraintrustClusterAnalyzer:
    """
    POC Cluster Analyzer with Braintrust integration.
    
    This demonstrates how to:
    1. Load prompts from Braintrust
    2. Trace LLM calls for evaluation
    3. Maintain local fallbacks for reliability
    4. Compare different models (flash vs pro)
    """
    
    def __init__(
        self,
        braintrust_project: str = "hierarchical-discovery",
        enable_braintrust: bool = True,
        temperature: float = 0.0,
        model: str = "flash"
    ):
        """
        Initialize the Braintrust-integrated analyzer.
        
        Args:
            braintrust_project: Name of your Braintrust project
            enable_braintrust: Whether to enable Braintrust logging
            temperature: LLM temperature setting
            model: LLM model to use ('flash' or 'pro')
        """
        # Initialize Braintrust
        self.braintrust_enabled = init_braintrust(
            project=braintrust_project,
            enabled=enable_braintrust
        )
        
        # Store model name for logging
        self.model_name = model
        
        # Map model name to GeminiModelType
        model_type = GeminiModelType.PRO if model == "pro" else GeminiModelType.FLASH
        
        # Initialize LLM client with selected model
        self.llm_client = GeminiClient(
            default_model=model_type,
            default_temperature=temperature,
            thinking_budget=0
        )
        
        logger.info(f"BraintrustClusterAnalyzer initialized (Braintrust: {'enabled' if self.braintrust_enabled else 'disabled'}, Model: {model})")
    
    def _create_analysis_prompt_braintrust(
        self,
        cluster_id: int,
        example_texts: List[str],
        cluster_size: int,
        total_clusters: int,
        person_metrics: Dict[str, Any]
    ) -> str:
        """
        Create analysis prompt with Braintrust integration.
        
        This method first tries to load the prompt from Braintrust.
        If that fails, it falls back to the local template.
        
        The prompt in Braintrust can be edited via their UI, allowing
        you to iterate on prompts without code changes.
        """
        import yaml
        
        # Extract metrics
        unique_respondents = person_metrics.get('unique_respondents', cluster_size)
        avg_mentions = person_metrics.get('avg_mentions_per_respondent', 1.0)
        coverage_pct = person_metrics.get('respondent_coverage_pct', 0.0)
        
        # Format input as YAML for cleaner Braintrust display
        input_data = {
            "cluster_info": {
                "cluster_id": cluster_id,
                "total_messages": cluster_size,
                "unique_citizens": unique_respondents,
                "avg_mentions_per_citizen": round(avg_mentions, 1),
                "respondent_coverage_pct": round(coverage_pct, 1),
                "total_clusters": total_clusters
            },
            "messages": example_texts[:10]
        }
        
        # Variables for prompt template
        variables = {
            "input_yaml": yaml.dump(input_data, default_flow_style=False, sort_keys=False)
        }
        
        # Try to load from Braintrust, fall back to local template
        prompt = load_prompt_from_braintrust(
            prompt_name="cluster-analysis-v1",
            fallback_prompt=CLUSTER_ANALYSIS_PROMPT_TEMPLATE,
            variables=variables
        )
        
        return prompt
    
    async def analyze_cluster(
        self,
        cluster_id: int,
        messages: List[str],
        total_clusters: int = 10,
        person_metrics: Optional[Dict[str, Any]] = None
    ) -> Optional[ClusterAnalysisResponse]:
        """
        Analyze a cluster with Braintrust logging.
        
        All LLM calls are traced to Braintrust for evaluation.
        
        Args:
            cluster_id: ID of the cluster being analyzed
            messages: List of message texts in this cluster
            total_clusters: Total number of clusters in the analysis
            person_metrics: Optional metrics about respondents
        
        Returns:
            ClusterAnalysisResponse or None if analysis fails
        """
        if not messages:
            logger.warning(f"No messages in cluster {cluster_id}")
            return None
        
        # Use default metrics if not provided
        if person_metrics is None:
            person_metrics = {
                'unique_respondents': len(messages),
                'avg_mentions_per_respondent': 1.0,
                'respondent_coverage_pct': 0.0
            }
        
        # Build clean structured input data for Braintrust logging
        input_data = {
            "cluster_info": {
                "cluster_id": cluster_id,
                "total_messages": len(messages),
                "unique_citizens": person_metrics.get('unique_respondents', len(messages)),
                "avg_mentions_per_citizen": round(person_metrics.get('avg_mentions_per_respondent', 1.0), 1),
                "respondent_coverage_pct": round(person_metrics.get('respondent_coverage_pct', 0.0), 1),
                "total_clusters": total_clusters
            },
            "messages": messages
        }
        
        # Create the prompt for the LLM
        prompt = self._create_analysis_prompt_braintrust(
            cluster_id=cluster_id,
            example_texts=messages,
            cluster_size=len(messages),
            total_clusters=total_clusters,
            person_metrics=person_metrics
        )
        
        # Make the LLM call with Braintrust tracing
        def make_llm_call():
            return self.llm_client.generate_structured_content(
                prompt=prompt,
                response_schema=ClusterAnalysisResponse,
                system_instruction="You are an expert civic message analyst. Analyze citizen messages and identify themes, issues, and actionable items."
            )
        
        try:
            response = traced_llm_call(
                name="cluster_analysis",
                input_data=input_data,  # Clean structured data for Braintrust UI
                llm_call_fn=make_llm_call,
                prompt=prompt,  # Stored in metadata for debugging
                metadata={"model": self.model_name},  # Log model for comparison
                tags=["cluster-analysis", "poc", f"model-{self.model_name}"]
            )
            
            logger.info(f"Cluster {cluster_id} analyzed: theme='{response.theme}'")
            return response
            
        except Exception as e:
            logger.error(f"Failed to analyze cluster {cluster_id}: {e}")
            return None
    
    def cleanup(self):
        """Cleanup resources and flush logs."""
        flush_logs()
        if hasattr(self.llm_client, 'close'):
            self.llm_client.close()


# ============================================================================
# POC Demo - Run this to test the integration
# ============================================================================

async def run_poc_demo():
    """
    Run a simple POC demo with sample data.
    
    This demonstrates:
    1. Initializing the Braintrust-integrated analyzer
    2. Analyzing sample clusters
    3. Having logs appear in Braintrust
    """
    
    # Sample civic messages for testing
    sample_clusters = {
        1: [
            "The potholes on Main Street are destroying my car",
            "Road conditions are terrible, especially after the last winter",
            "We need better road maintenance in the downtown area",
            "My street hasn't been repaved in 20 years",
            "The city needs to prioritize infrastructure repairs"
        ],
        2: [
            "Property taxes are way too high for seniors on fixed income",
            "Can't afford these tax increases anymore",
            "Why do we pay so much when services are declining?",
            "Tax burden is pushing families out of the city",
            "Need property tax relief for longtime residents"
        ],
        3: [
            "School overcrowding is affecting my children's education",
            "We need more teachers, not bigger class sizes",
            "The school building is falling apart",
            "Our kids deserve better educational facilities",
            "Why is the school district cutting programs?"
        ]
    }
    
    print("\n" + "="*60)
    print("🧪 Braintrust Integration POC Demo")
    print("="*60)
    
    # Check for API key
    if not os.getenv("BRAINTRUST_API_KEY"):
        print("\n⚠️  BRAINTRUST_API_KEY not set!")
        print("   Set it to enable Braintrust logging:")
        print("   export BRAINTRUST_API_KEY='your-api-key'")
        print("\n   Running in local-only mode (no Braintrust logging)...")
    
    # Initialize analyzer
    analyzer = BraintrustClusterAnalyzer(
        braintrust_project="hierarchical-discovery-poc",
        enable_braintrust=True  # Will gracefully disable if no API key
    )
    
    print(f"\n📊 Braintrust Status: {'✅ Enabled' if is_enabled() else '❌ Disabled (local mode)'}")
    print(f"📝 Analyzing {len(sample_clusters)} sample clusters...\n")
    
    try:
        results = []
        for cluster_id, messages in sample_clusters.items():
            print(f"  Analyzing cluster {cluster_id} ({len(messages)} messages)...")
            
            result = await analyzer.analyze_cluster(
                cluster_id=cluster_id,
                messages=messages,
                total_clusters=len(sample_clusters)
            )
            
            if result:
                results.append(result)
                print(f"    ✅ Theme: '{result.theme}'")
            else:
                print(f"    ❌ Analysis failed")
        
        print("\n" + "-"*60)
        print("📋 Analysis Summary:")
        print("-"*60)
        
        for i, result in enumerate(results, 1):
            print(f"\n{i}. {result.theme}")
            print(f"   Summary: {result.issues_summary}")
            print(f"   Analysis: {result.detailed_analysis[:200]}...")
        
        print("\n" + "="*60)
        if is_enabled():
            print("✅ Logs sent to Braintrust!")
            print("   View them at: https://www.braintrust.dev/")
        else:
            print("ℹ️  Local mode - no logs sent to Braintrust")
        print("="*60 + "\n")
        
    finally:
        analyzer.cleanup()
    
    return results


# ============================================================================
# Customer Data Analysis - Run with REAL clustering pipeline
# ============================================================================

async def run_customer_analysis(
    customer: str,
    sample_size: Optional[int] = None,
    model: str = "flash"
) -> List[ClusterAnalysisResponse]:
    """
    Run cluster analysis on real customer data with REAL hierarchical clustering
    and Braintrust logging for prompt iteration.
    
    This function:
    1. Loads customer CSV data
    2. Runs the FULL hierarchical discovery pipeline (embeddings, clustering, etc.)
    3. Re-analyzes clusters with Braintrust tracing (for prompt iteration)
    
    Args:
        customer: Customer name (e.g., 'brett', 'cara', 'josh')
        sample_size: Maximum number of clusters to analyze (None = all)
        model: LLM model to use ('flash' or 'pro')
    
    Returns:
        List of ClusterAnalysisResponse objects
    """
    from collections import defaultdict
    from pathlib import Path
    import tempfile
    import yaml
    
    from .orchestrator import HierarchicalDiscoveryOrchestrator
    
    print("\n" + "="*60)
    print(f"🧪 Braintrust POC - Real Clustering Pipeline")
    print("="*60)
    print(f"\n📊 Customer: {customer}")
    print(f"🤖 Model: {model}")
    if sample_size:
        print(f"📉 Sample Size: {sample_size} clusters max")
    
    # Check for API key
    if not os.getenv("BRAINTRUST_API_KEY"):
        print("\n⚠️  BRAINTRUST_API_KEY not set!")
        print("   Set it to enable Braintrust logging:")
        print("   export BRAINTRUST_API_KEY='your-api-key'")
        print("\n   Running in local-only mode (no Braintrust logging)...")
    
    # Find the customer data file
    data_dir = Path(__file__).parent.parent / "data"
    csv_file = data_dir / f"{customer}.csv"
    
    if not csv_file.exists():
        available = [f.stem for f in data_dir.glob("*.csv") if f.stem.isalnum() or "_consolidated" in f.stem]
        raise ValueError(f"Customer data file not found: {csv_file}\nAvailable: {', '.join(sorted(available))}")
    
    # === STEP 1: Run the REAL hierarchical clustering pipeline ===
    print(f"\n🚀 Running full hierarchical discovery pipeline...")
    print(f"   This includes: filtering → AI processing → embeddings → clustering")
    
    # Load default config
    default_config_path = Path(__file__).parent / "config.yaml"
    with open(default_config_path) as f:
        config_data = yaml.safe_load(f)
    
    # Override for this customer
    config_data['data_source'] = customer
    if 'data_files' not in config_data:
        config_data['data_files'] = {}
    config_data['data_files'][customer] = str(csv_file)
    
    # Disable visualizations for speed
    config_data['dendrogram'] = {'enabled': False}
    config_data['output']['save_intermediates'] = False
    
    # Write temp config
    with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
        yaml.dump(config_data, f)
        temp_config_path = f.name
    
    try:
        # Run the real pipeline
        orchestrator = HierarchicalDiscoveryOrchestrator(
            config_path=temp_config_path,
            data_source_override=customer
        )
        
        # Run multi-cluster pipeline and get results as data (not files)
        pipeline_result = await orchestrator.run_multi_cluster_pipeline(
            disable_optimization=True,
            return_data=True
        )
        
        print(f"\n✅ Pipeline completed!")
        print(f"   Total messages: {pipeline_result.get('total_messages', 0)}")
        print(f"   Cluster ranges: {pipeline_result.get('cluster_ranges', [])}")
        
    finally:
        # Cleanup temp config
        import os as os_module
        os_module.unlink(temp_config_path)
    
    # === STEP 2: Extract clusters from pipeline results ===
    print(f"\n📊 Extracting clusters from pipeline results...")
    
    cluster_results = pipeline_result.get('cluster_results', {})
    if not cluster_results:
        print("   ⚠️ No cluster results found")
        return []
    
    # Use the first (or optimal) cluster configuration
    cluster_counts = list(cluster_results.keys())
    print(f"   Available cluster counts: {cluster_counts}")
    
    # Pick a middle-ground cluster count or the first one
    selected_k = cluster_counts[len(cluster_counts) // 2] if cluster_counts else None
    if not selected_k:
        print("   ⚠️ No cluster configurations available")
        return []
    
    print(f"   Using k={selected_k} for analysis")
    
    selected_result = cluster_results[selected_k]
    clustered_messages = selected_result.get('clustered_messages', [])
    existing_analyses = selected_result.get('analyzed_clusters', [])
    
    print(f"   Found {len(clustered_messages)} clustered messages")
    print(f"   Found {len(existing_analyses)} existing cluster analyses")
    
    # Group messages by cluster_id
    # Use msg.text which contains the atomic text (preprocessed, split civic concerns)
    clusters = defaultdict(list)
    for msg in clustered_messages:
        cluster_id = msg.cluster_assignment.cluster_id
        text = msg.text  # This is the atomic text, not original_text
        clusters[cluster_id].append(text)
    
    print(f"   Grouped into {len(clusters)} clusters")
    
    # Apply sample size limit
    cluster_ids = list(clusters.keys())
    if sample_size and sample_size < len(cluster_ids):
        print(f"   Limiting to {sample_size} clusters (sample mode)")
        cluster_ids = cluster_ids[:sample_size]
    
    # === STEP 3: Re-analyze clusters with Braintrust tracing ===
    print(f"\n📝 Re-analyzing {len(cluster_ids)} clusters with Braintrust logging...")
    
    # Initialize analyzer with selected model
    analyzer = BraintrustClusterAnalyzer(
        braintrust_project=os.getenv("BRAINTRUST_PROJECT", "hierarchical-discovery-poc"),
        enable_braintrust=True,
        model=model
    )
    
    print(f"   Braintrust Status: {'✅ Enabled' if is_enabled() else '❌ Disabled (local mode)'}")
    print()
    
    try:
        results = []
        for cluster_id in cluster_ids:
            messages = clusters[cluster_id]
            if not messages:
                continue
                
            print(f"  Analyzing cluster {cluster_id} ({len(messages)} messages)...")
            
            result = await analyzer.analyze_cluster(
                cluster_id=cluster_id,
                messages=messages[:30],  # Limit messages per cluster for cost
                total_clusters=len(clusters)
            )
            
            if result:
                results.append(result)
                print(f"    ✅ Theme: '{result.theme}'")
            else:
                print(f"    ❌ Analysis failed")
        
        # Print summary
        print("\n" + "-"*60)
        print("📋 Analysis Summary:")
        print("-"*60)
        
        for i, result in enumerate(results, 1):
            print(f"\n{i}. {result.theme}")
            print(f"   Summary: {result.issues_summary}")
            print(f"   Analysis: {result.detailed_analysis[:200]}...")
        
        print("\n" + "="*60)
        if is_enabled():
            print("✅ Logs sent to Braintrust!")
            print("   View them at: https://www.braintrust.dev/")
            print(f"   Filter by tag: model-{model}")
        else:
            print("ℹ️  Local mode - no logs sent to Braintrust")
        print("="*60 + "\n")
        
    finally:
        analyzer.cleanup()
    
    return results


if __name__ == "__main__":
    asyncio.run(run_poc_demo())

