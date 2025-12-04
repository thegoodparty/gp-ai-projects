#!/usr/bin/env python3
"""
Runner script for the Braintrust POC

This script demonstrates the Braintrust integration with the hierarchical
discovery pipeline. It can be run in multiple modes:

1. Demo mode (default): Uses sample civic messages
2. Customer mode: Loads real customer CSV data for analysis

Usage:
    # Install braintrust first
    pip install braintrust
    
    # Set your API keys
    export BRAINTRUST_API_KEY='your-api-key'
    export GEMINI_API_KEY='your-gemini-api-key'
    
    # Run demo mode
    python serve/hierarchical_discovery/run_braintrust_poc.py
    
    # Run with real customer data
    python serve/hierarchical_discovery/run_braintrust_poc.py --customer brett
    
    # Run with sampling (analyze only 5 clusters)
    python serve/hierarchical_discovery/run_braintrust_poc.py --customer brett --sample-size 5
    
    # Compare different models
    python serve/hierarchical_discovery/run_braintrust_poc.py --customer brett --model pro

What this POC demonstrates:
    1. Prompts can be stored/edited in Braintrust's UI
    2. All LLM calls are logged to Braintrust for evaluation
    3. You can iterate on prompts without code changes
    4. Local fallbacks ensure reliability
    5. Model comparison for quality evaluation

After running, check Braintrust at https://www.braintrust.dev/ to:
    - View logs of all LLM calls
    - See inputs/outputs side by side
    - Create evaluations to compare prompt versions
    - Score outputs to measure quality
"""

import argparse
import asyncio
import os
import sys

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

# Load .env file early before checking environment variables
from dotenv import load_dotenv
load_dotenv()

from shared.logger import get_logger

logger = get_logger(__name__)


def check_dependencies():
    """Check that required dependencies are installed."""
    missing = []
    
    try:
        import braintrust
    except ImportError:
        missing.append("braintrust")
    
    try:
        import google.genai
    except ImportError:
        missing.append("google-genai")
    
    if missing:
        print("\n⚠️  Missing dependencies:")
        for dep in missing:
            print(f"   pip install {dep}")
        print()
        return False
    
    return True


def check_env_vars():
    """Check that required environment variables are set."""
    warnings = []
    
    if not os.getenv("BRAINTRUST_API_KEY"):
        warnings.append(
            "BRAINTRUST_API_KEY not set - Braintrust logging will be disabled.\n"
            "   To enable: export BRAINTRUST_API_KEY='your-api-key'"
        )
    
    if not os.getenv("GEMINI_API_KEY"):
        print("\n❌ GEMINI_API_KEY not set - required for LLM calls")
        print("   export GEMINI_API_KEY='your-api-key'")
        return False
    
    if warnings:
        print("\n⚠️  Environment warnings:")
        for warn in warnings:
            print(f"   {warn}")
        print()
    
    return True


async def run_demo_mode():
    """Run the POC in demo mode with sample data."""
    from serve.hierarchical_discovery.poc_braintrust_analyzer import run_poc_demo
    return await run_poc_demo()


async def run_customer_data_mode(customer: str, sample_size: int = None, model: str = "flash"):
    """
    Run the POC with real customer CSV data.
    
    This loads customer data from CSV, runs it through the pipeline stages,
    and analyzes clusters with Braintrust logging enabled.
    
    Args:
        customer: Customer name (e.g., 'brett', 'andrea', 'justin')
        sample_size: Maximum number of clusters to analyze (None = all)
        model: LLM model to use ('flash' or 'pro')
    """
    from serve.hierarchical_discovery.poc_braintrust_analyzer import run_customer_analysis
    
    return await run_customer_analysis(
        customer=customer,
        sample_size=sample_size,
        model=model
    )


def get_available_customers():
    """Get list of available customer datasets from serve/data directory."""
    from pathlib import Path
    
    data_dir = Path(__file__).parent.parent / "data"
    
    if not data_dir.exists():
        # Fallback to hardcoded list
        return ["brett", "andrea", "justin", "cara"]
    
    # Find all .csv files - include both simple names and consolidated files
    customers = []
    for csv_file in data_dir.glob("*.csv"):
        name = csv_file.stem  # Get filename without extension
        # Include simple alphanumeric names
        if name.isalnum() and len(name) < 20:
            customers.append(name)
        # Also include consolidated files (e.g., caleb_all_rounds_consolidated)
        elif "_consolidated" in name or "_all_rounds" in name:
            customers.append(name)
    
    return sorted(customers) if customers else ["brett", "andrea", "justin"]


def main():
    available_customers = get_available_customers()
    
    parser = argparse.ArgumentParser(
        description="Run Braintrust integration POC",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""
Examples:
    # Run demo with sample data
    python run_braintrust_poc.py
    
    # Run with real customer data
    python run_braintrust_poc.py --customer cara
    
    # Run with sampling (analyze only 5 clusters)
    python run_braintrust_poc.py --customer cara --sample-size 5
    
    # Compare different models
    python run_braintrust_poc.py --customer cara --model pro
    
    # Run with Braintrust disabled (local only)
    python run_braintrust_poc.py --customer cara --no-braintrust
    
    # Set custom project name
    python run_braintrust_poc.py --customer cara --project my-project

Available customers: {', '.join(available_customers)}
        """
    )
    
    parser.add_argument(
        "--customer",
        type=str,
        choices=available_customers,
        help=f"Customer dataset to analyze. Available: {', '.join(available_customers)}"
    )
    
    parser.add_argument(
        "--sample-size",
        type=int,
        default=None,
        help="Maximum number of clusters to analyze (default: all clusters)"
    )
    
    parser.add_argument(
        "--model",
        type=str,
        choices=["flash", "pro"],
        default="flash",
        help="LLM model to use: 'flash' (fast/cheap) or 'pro' (better quality)"
    )
    
    parser.add_argument(
        "--no-braintrust",
        action="store_true",
        help="Disable Braintrust logging (run locally only)"
    )
    
    parser.add_argument(
        "--project",
        type=str,
        default="hierarchical-discovery-poc",
        help="Braintrust project name"
    )
    
    args = parser.parse_args()
    
    # Update environment if project specified
    if args.project:
        os.environ["BRAINTRUST_PROJECT"] = args.project
    
    # Disable Braintrust if requested
    if args.no_braintrust:
        os.environ.pop("BRAINTRUST_API_KEY", None)
    
    print("\n🚀 Braintrust POC Runner")
    print("-" * 40)
    
    # Check dependencies
    if not check_dependencies():
        print("Install missing dependencies and try again.")
        sys.exit(1)
    
    # Check environment
    if not check_env_vars():
        sys.exit(1)
    
    # Run appropriate mode
    try:
        if args.customer:
            print(f"📊 Mode: Customer Data ({args.customer})")
            print(f"🤖 Model: {args.model}")
            if args.sample_size:
                print(f"📉 Sample Size: {args.sample_size} clusters")
            results = asyncio.run(run_customer_data_mode(
                customer=args.customer,
                sample_size=args.sample_size,
                model=args.model
            ))
        else:
            print("📊 Mode: Demo (sample data)")
            results = asyncio.run(run_demo_mode())
        
        print(f"\n✅ POC completed successfully! Analyzed {len(results)} clusters.")
        
    except KeyboardInterrupt:
        print("\n\n⚠️  POC interrupted by user")
        sys.exit(1)
    except Exception as e:
        logger.exception("POC failed")
        print(f"\n❌ POC failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()

