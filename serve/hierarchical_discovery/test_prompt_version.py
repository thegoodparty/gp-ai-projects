#!/usr/bin/env python3

"""
Quick test script for evaluating hierarchical discovery prompt changes.
Runs pipeline on standard test files and generates comparison reports.
"""

import argparse
import subprocess
import sys
from datetime import datetime
from pathlib import Path
import json
import csv
from typing import Dict, List, Any

# Standard test files (small, medium, large)
DEFAULT_TEST_FILES = [
    "019a367f-b7c2-71a3-b140-adefc9b7ba0a",  # Very small (1.2K) - often empty
    "019a5083-c166-7170-917a-78bde83ee0d9",  # Small (1.3K) - minimal messages
    "019ab7bc-7912-7f22-bb59-5f35d0640595",  # Medium (14K) - good test size
    "019a604c-898a-7481-a868-c09fa080759b",  # Large (75K) - full scale test
]

class Colors:
    """ANSI color codes for terminal output"""
    HEADER = '\033[95m'
    BLUE = '\033[94m'
    CYAN = '\033[96m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    RED = '\033[91m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'
    UNDERLINE = '\033[4m'


def print_header(text: str):
    """Print a formatted header"""
    print(f"\n{Colors.BOLD}{Colors.CYAN}{'='*80}{Colors.ENDC}")
    print(f"{Colors.BOLD}{Colors.CYAN}{text}{Colors.ENDC}")
    print(f"{Colors.BOLD}{Colors.CYAN}{'='*80}{Colors.ENDC}\n")


def print_section(text: str):
    """Print a section header"""
    print(f"\n{Colors.BOLD}{Colors.BLUE}{text}{Colors.ENDC}")
    print(f"{Colors.BLUE}{'-'*len(text)}{Colors.ENDC}")


def run_pipeline(data_source: str, quick_test: bool = True, timeout: int = 300) -> Dict[str, Any]:
    """Run hierarchical discovery pipeline on a data source"""
    print(f"{Colors.YELLOW}Running pipeline on: {data_source}{Colors.ENDC}")
    
    cmd = [
        "uv", "run",
        "serve/hierarchical_discovery/run_pipeline.py",
        "--data-source", data_source
    ]
    
    if quick_test:
        cmd.append("--quick-test")
    
    start_time = datetime.now()
    
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=Path(__file__).parent.parent.parent
        )
        
        duration = (datetime.now() - start_time).total_seconds()
        
        return {
            "success": result.returncode == 0,
            "duration": duration,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "returncode": result.returncode
        }
    except subprocess.TimeoutExpired:
        duration = (datetime.now() - start_time).total_seconds()
        return {
            "success": False,
            "duration": duration,
            "error": f"Timeout after {timeout}s"
        }
    except Exception as e:
        duration = (datetime.now() - start_time).total_seconds()
        return {
            "success": False,
            "duration": duration,
            "error": str(e)
        }


def parse_report(report_path: Path) -> Dict[str, Any]:
    """Parse a multi-cluster report file"""
    if not report_path.exists():
        return {}
    
    content = report_path.read_text()
    data = {}
    
    # Extract key information
    for line in content.split('\n'):
        if '**Total Messages:**' in line:
            try:
                # Extract number after the last colon
                parts = line.split('**Total Messages:**')
                if len(parts) > 1:
                    data['total_messages'] = int(parts[1].strip())
            except (ValueError, IndexError):
                pass
        elif '**Cluster Ranges Tested:**' in line:
            try:
                parts = line.split('**Cluster Ranges Tested:**')
                if len(parts) > 1:
                    data['cluster_ranges'] = int(parts[1].strip())
            except (ValueError, IndexError):
                pass
        elif '**Total Cost:**' in line:
            try:
                cost_str = line.split('$')[1].strip()
                data['total_cost'] = float(cost_str)
            except (ValueError, IndexError):
                pass
        elif '|' in line and 'Cluster Count' not in line and '---' not in line:
            # Parse cluster results table
            if 'cluster_count' not in data:
                parts = [p.strip() for p in line.split('|') if p.strip()]
                if len(parts) >= 4:
                    try:
                        if parts[0].isdigit():
                            data['cluster_count'] = int(parts[0])
                            data['themes_found'] = int(parts[1])
                            data['avg_people_per_cluster'] = float(parts[2])
                            data['top_themes'] = parts[3]
                    except (ValueError, IndexError):
                        pass
    
    return data


def get_latest_report(data_source: str, output_dir: Path) -> Path:
    """Find the most recent report for a data source"""
    reports_dir = output_dir / "reports"
    pattern = f"multi_cluster_report_{data_source}_*.md"
    
    matching_reports = sorted(reports_dir.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
    
    if matching_reports:
        return matching_reports[0]
    return None


def analyze_csv_export(export_path: Path) -> Dict[str, Any]:
    """Analyze the CSV export to extract prompt quality metrics"""
    if not export_path.exists():
        return {}
    
    data = {
        'total_rows': 0,
        'unique_clusters': set(),
        'unique_themes': set(),
        'theme_samples': [],
        'analysis_samples': [],
    }
    
    with open(export_path, 'r') as f:
        reader = csv.DictReader(f)
        for i, row in enumerate(reader):
            data['total_rows'] += 1
            
            if 'cluster_merged' in row:
                data['unique_clusters'].add(row['cluster_merged'])
            
            if 'theme_merged' in row:
                theme = row['theme_merged']
                data['unique_themes'].add(theme)
                
                # Collect first 3 unique theme samples
                if len(data['theme_samples']) < 3 and theme not in [t['theme'] for t in data['theme_samples']]:
                    data['theme_samples'].append({
                        'theme': theme,
                        'issues_summary': row.get('issues_summary_merged', '')[:150],
                        'analysis_preview': row.get('detailed_analysis_merged', '')[:200]
                    })
    
    data['unique_clusters'] = len(data['unique_clusters'])
    data['unique_themes'] = len(data['unique_themes'])
    
    return data


def get_latest_export(data_source: str, output_dir: Path) -> Path:
    """Find the most recent export for a data source"""
    exports_dir = output_dir / "exports"
    pattern = f"multi_cluster_results_{data_source}_*.csv"
    
    matching_exports = sorted(exports_dir.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
    
    if matching_exports:
        return matching_exports[0]
    return None


def print_results_summary(data_source: str, run_result: Dict, report_data: Dict, export_data: Dict):
    """Print a formatted summary of results"""
    print_section(f"Results for {data_source}")
    
    if not run_result.get('success'):
        print(f"{Colors.RED}✗ Pipeline failed{Colors.ENDC}")
        if 'error' in run_result:
            print(f"  Error: {run_result['error']}")
        return
    
    print(f"{Colors.GREEN}✓ Pipeline completed in {run_result['duration']:.1f}s{Colors.ENDC}")
    
    if report_data:
        print(f"\n{Colors.BOLD}Report Summary:{Colors.ENDC}")
        print(f"  Messages: {report_data.get('total_messages', 'N/A')}")
        print(f"  Clusters: {report_data.get('cluster_count', 'N/A')}")
        print(f"  Themes: {report_data.get('themes_found', 'N/A')}")
        print(f"  Avg per cluster: {report_data.get('avg_people_per_cluster', 'N/A')}")
        print(f"  Cost: ${report_data.get('total_cost', 0):.4f}")
        
        if 'top_themes' in report_data:
            print(f"\n{Colors.BOLD}Top Themes:{Colors.ENDC}")
            print(f"  {report_data['top_themes']}")
    
    if export_data and export_data.get('theme_samples'):
        print(f"\n{Colors.BOLD}Sample Theme Analysis (from new prompt):{Colors.ENDC}")
        for i, sample in enumerate(export_data['theme_samples'][:2], 1):
            print(f"\n  {Colors.CYAN}{i}. {sample['theme']}{Colors.ENDC}")
            print(f"     Summary: {sample['issues_summary']}")
            if sample['analysis_preview']:
                print(f"     Analysis: {sample['analysis_preview']}...")


def main():
    parser = argparse.ArgumentParser(
        description="Test hierarchical discovery prompt changes",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Run on default test files
  python test_prompt_version.py
  
  # Run on specific files
  python test_prompt_version.py --files 019ab7bc-7912-7f22-bb59-5f35d0640595 019a604c-898a-7481-a868-c09fa080759b
  
  # Run without quick-test mode (full analysis)
  python test_prompt_version.py --no-quick-test
  
  # Add a version label for tracking
  python test_prompt_version.py --version "v2-action-oriented"
        """
    )
    
    parser.add_argument(
        '--files',
        nargs='+',
        default=DEFAULT_TEST_FILES,
        help='Data source files to test (UUIDs or names)'
    )
    
    parser.add_argument(
        '--no-quick-test',
        action='store_true',
        help='Run full analysis instead of quick test'
    )
    
    parser.add_argument(
        '--timeout',
        type=int,
        default=300,
        help='Timeout per file in seconds (default: 300)'
    )
    
    parser.add_argument(
        '--version',
        type=str,
        default=None,
        help='Version label for this test run'
    )
    
    parser.add_argument(
        '--save-summary',
        action='store_true',
        help='Save results summary to JSON file'
    )
    
    args = parser.parse_args()
    
    # Setup paths
    project_root = Path(__file__).parent.parent.parent
    output_dir = Path(__file__).parent / "output"
    
    # Print header
    print_header("Hierarchical Discovery Prompt Testing")
    
    if args.version:
        print(f"{Colors.BOLD}Version:{Colors.ENDC} {args.version}")
    
    print(f"{Colors.BOLD}Test Files:{Colors.ENDC} {len(args.files)}")
    print(f"{Colors.BOLD}Quick Test:{Colors.ENDC} {not args.no_quick_test}")
    print(f"{Colors.BOLD}Timeout:{Colors.ENDC} {args.timeout}s per file")
    
    # Run tests
    all_results = []
    start_time = datetime.now()
    
    for i, data_source in enumerate(args.files, 1):
        print_header(f"Test {i}/{len(args.files)}: {data_source}")
        
        # Run pipeline
        run_result = run_pipeline(
            data_source,
            quick_test=not args.no_quick_test,
            timeout=args.timeout
        )
        
        # Get latest reports
        report_path = get_latest_report(data_source, output_dir)
        export_path = get_latest_export(data_source, output_dir)
        
        # Parse results
        report_data = parse_report(report_path) if report_path else {}
        export_data = analyze_csv_export(export_path) if export_path else {}
        
        # Store results
        result_summary = {
            'data_source': data_source,
            'timestamp': datetime.now().isoformat(),
            'version': args.version,
            'run_result': {k: v for k, v in run_result.items() if k != 'stdout' and k != 'stderr'},
            'report_data': report_data,
            'export_summary': {k: v for k, v in export_data.items() if k not in ['theme_samples', 'analysis_samples']}
        }
        all_results.append(result_summary)
        
        # Print summary
        print_results_summary(data_source, run_result, report_data, export_data)
    
    # Overall summary
    total_duration = (datetime.now() - start_time).total_seconds()
    successful_runs = sum(1 for r in all_results if r['run_result'].get('success'))
    total_cost = sum(r['report_data'].get('total_cost', 0) for r in all_results)
    
    print_header("Overall Summary")
    print(f"{Colors.BOLD}Total Duration:{Colors.ENDC} {total_duration:.1f}s")
    print(f"{Colors.BOLD}Successful Runs:{Colors.ENDC} {successful_runs}/{len(args.files)}")
    print(f"{Colors.BOLD}Total Cost:{Colors.ENDC} ${total_cost:.4f}")
    
    # Save summary if requested
    if args.save_summary:
        summary_file = output_dir / f"test_summary_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        summary_file.parent.mkdir(parents=True, exist_ok=True)
        
        with open(summary_file, 'w') as f:
            json.dump({
                'version': args.version,
                'timestamp': datetime.now().isoformat(),
                'total_duration': total_duration,
                'total_cost': total_cost,
                'results': all_results
            }, f, indent=2)
        
        print(f"\n{Colors.GREEN}✓ Summary saved to: {summary_file}{Colors.ENDC}")
    
    print(f"\n{Colors.BOLD}Output directory:{Colors.ENDC} {output_dir}")
    print(f"{Colors.BOLD}Latest reports:{Colors.ENDC} {output_dir}/reports/")
    print(f"{Colors.BOLD}Latest exports:{Colors.ENDC} {output_dir}/exports/")
    
    # Exit with appropriate code
    sys.exit(0 if successful_runs == len(args.files) else 1)


if __name__ == "__main__":
    main()


