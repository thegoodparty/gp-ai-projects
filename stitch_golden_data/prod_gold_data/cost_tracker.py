import os
import json
import pandas as pd
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, asdict
from shared.logger import get_logger

@dataclass
class CostRecord:
    """Individual cost record for tracking"""
    timestamp: str
    operation_type: str  # 'vector_generation', 'matching', 'embedding', 'llm'
    state: Optional[str]
    records_processed: int
    embedding_cost: float
    llm_cost: float
    total_cost: float
    metadata: Dict

@dataclass
class CostSummary:
    """Cost summary for reporting"""
    total_cost: float
    embedding_cost: float
    llm_cost: float
    total_records: int
    cost_per_record: float
    operations: List[str]
    states_processed: List[str]
    time_period: str

class CostTracker:
    """Comprehensive cost tracking and reporting for BR-L2 matching operations"""
    
    def __init__(self):
        self.logger = get_logger(__name__)
        
        # Setup cost tracking directory
        current_file_dir = os.path.dirname(os.path.abspath(__file__))
        stitch_golden_data_dir = os.path.dirname(current_file_dir) if os.path.basename(current_file_dir) == "stitch_golden_data" else current_file_dir
        
        self.cost_tracking_dir = os.path.join(stitch_golden_data_dir, "stitch_golden_data", "cost_tracking")
        os.makedirs(self.cost_tracking_dir, exist_ok=True)
        
        self.daily_costs_file = os.path.join(self.cost_tracking_dir, "daily_costs.json")
        self.detailed_log_file = os.path.join(self.cost_tracking_dir, "detailed_cost_log.jsonl")

    def record_cost(self, operation_type: str, embedding_cost: float, llm_cost: float, 
                   records_processed: int, state: Optional[str] = None, **metadata):
        """Record a cost entry"""
        total_cost = embedding_cost + llm_cost
        
        record = CostRecord(
            timestamp=datetime.now().isoformat(),
            operation_type=operation_type,
            state=state,
            records_processed=records_processed,
            embedding_cost=embedding_cost,
            llm_cost=llm_cost,
            total_cost=total_cost,
            metadata=metadata
        )
        
        # Append to detailed log
        with open(self.detailed_log_file, 'a') as f:
            f.write(json.dumps(asdict(record)) + '\n')
        
        # Update daily summary
        self._update_daily_summary(record)
        
        self.logger.info(f"💰 Cost recorded: {operation_type} - ${total_cost:.6f} ({records_processed} records)")

    def _update_daily_summary(self, record: CostRecord):
        """Update daily cost summary"""
        today = datetime.now().strftime('%Y-%m-%d')
        
        # Load existing daily costs
        daily_costs = {}
        if os.path.exists(self.daily_costs_file):
            with open(self.daily_costs_file, 'r') as f:
                daily_costs = json.load(f)
        
        # Initialize today's entry if needed
        if today not in daily_costs:
            daily_costs[today] = {
                'total_cost': 0.0,
                'embedding_cost': 0.0,
                'llm_cost': 0.0,
                'total_records': 0,
                'operations': [],
                'states': []
            }
        
        # Update today's summary
        today_data = daily_costs[today]
        today_data['total_cost'] += record.total_cost
        today_data['embedding_cost'] += record.embedding_cost
        today_data['llm_cost'] += record.llm_cost
        today_data['total_records'] += record.records_processed
        
        if record.operation_type not in today_data['operations']:
            today_data['operations'].append(record.operation_type)
        
        if record.state and record.state not in today_data['states']:
            today_data['states'].append(record.state)
        
        # Save updated daily costs
        with open(self.daily_costs_file, 'w') as f:
            json.dump(daily_costs, f, indent=2)

    def get_daily_summary(self, date: Optional[str] = None) -> Optional[CostSummary]:
        """Get cost summary for a specific date"""
        if not date:
            date = datetime.now().strftime('%Y-%m-%d')
        
        if not os.path.exists(self.daily_costs_file):
            return None
        
        with open(self.daily_costs_file, 'r') as f:
            daily_costs = json.load(f)
        
        if date not in daily_costs:
            return None
        
        data = daily_costs[date]
        return CostSummary(
            total_cost=data['total_cost'],
            embedding_cost=data['embedding_cost'],
            llm_cost=data['llm_cost'],
            total_records=data['total_records'],
            cost_per_record=data['total_cost'] / max(1, data['total_records']),
            operations=data['operations'],
            states_processed=data['states'],
            time_period=f"Day: {date}"
        )

    def get_period_summary(self, days: int = 7) -> Optional[CostSummary]:
        """Get cost summary for the last N days"""
        if not os.path.exists(self.daily_costs_file):
            return None
        
        with open(self.daily_costs_file, 'r') as f:
            daily_costs = json.load(f)
        
        # Calculate date range
        end_date = datetime.now()
        start_date = end_date - timedelta(days=days-1)
        
        total_cost = 0.0
        embedding_cost = 0.0
        llm_cost = 0.0
        total_records = 0
        all_operations = set()
        all_states = set()
        
        # Sum costs for the period
        for i in range(days):
            date_str = (start_date + timedelta(days=i)).strftime('%Y-%m-%d')
            if date_str in daily_costs:
                day_data = daily_costs[date_str]
                total_cost += day_data['total_cost']
                embedding_cost += day_data['embedding_cost']
                llm_cost += day_data['llm_cost']
                total_records += day_data['total_records']
                all_operations.update(day_data['operations'])
                all_states.update(day_data['states'])
        
        if total_cost == 0:
            return None
        
        return CostSummary(
            total_cost=total_cost,
            embedding_cost=embedding_cost,
            llm_cost=llm_cost,
            total_records=total_records,
            cost_per_record=total_cost / max(1, total_records),
            operations=list(all_operations),
            states_processed=list(all_states),
            time_period=f"Last {days} days ({start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')})"
        )

    def get_state_breakdown(self, days: int = 30) -> Dict[str, CostSummary]:
        """Get cost breakdown by state for the last N days"""
        if not os.path.exists(self.detailed_log_file):
            return {}
        
        # Calculate cutoff date
        cutoff_date = datetime.now() - timedelta(days=days)
        
        state_costs = {}
        
        # Read detailed log
        with open(self.detailed_log_file, 'r') as f:
            for line in f:
                record_data = json.loads(line.strip())
                record_time = datetime.fromisoformat(record_data['timestamp'])
                
                if record_time < cutoff_date:
                    continue
                
                state = record_data.get('state', 'Unknown')
                if state not in state_costs:
                    state_costs[state] = {
                        'total_cost': 0.0,
                        'embedding_cost': 0.0,
                        'llm_cost': 0.0,
                        'total_records': 0,
                        'operations': set()
                    }
                
                state_data = state_costs[state]
                state_data['total_cost'] += record_data['total_cost']
                state_data['embedding_cost'] += record_data['embedding_cost']
                state_data['llm_cost'] += record_data['llm_cost']
                state_data['total_records'] += record_data['records_processed']
                state_data['operations'].add(record_data['operation_type'])
        
        # Convert to CostSummary objects
        result = {}
        for state, data in state_costs.items():
            result[state] = CostSummary(
                total_cost=data['total_cost'],
                embedding_cost=data['embedding_cost'],
                llm_cost=data['llm_cost'],
                total_records=data['total_records'],
                cost_per_record=data['total_cost'] / max(1, data['total_records']),
                operations=list(data['operations']),
                states_processed=[state],
                time_period=f"State: {state} (Last {days} days)"
            )
        
        return result

    def generate_cost_report(self, output_file: Optional[str] = None) -> str:
        """Generate comprehensive cost report"""
        if not output_file:
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            output_file = f"cost_report_{timestamp}.txt"
        
        output_path = os.path.join(self.cost_tracking_dir, output_file)
        
        with open(output_path, 'w') as f:
            f.write("BR-L2 DISTRICT MATCHING COST REPORT\n")
            f.write("=" * 80 + "\n")
            f.write(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
            
            # Today's summary
            today_summary = self.get_daily_summary()
            if today_summary:
                f.write("TODAY'S COSTS\n")
                f.write("-" * 40 + "\n")
                f.write(f"Total Cost: ${today_summary.total_cost:.6f}\n")
                f.write(f"Embedding Cost: ${today_summary.embedding_cost:.6f}\n")
                f.write(f"LLM Cost: ${today_summary.llm_cost:.6f}\n")
                f.write(f"Records Processed: {today_summary.total_records:,}\n")
                f.write(f"Cost per Record: ${today_summary.cost_per_record:.6f}\n")
                f.write(f"Operations: {', '.join(today_summary.operations)}\n")
                f.write(f"States: {', '.join(today_summary.states_processed)}\n\n")
            
            # Week summary
            week_summary = self.get_period_summary(7)
            if week_summary:
                f.write("LAST 7 DAYS\n")
                f.write("-" * 40 + "\n")
                f.write(f"Total Cost: ${week_summary.total_cost:.6f}\n")
                f.write(f"Embedding Cost: ${week_summary.embedding_cost:.6f}\n")
                f.write(f"LLM Cost: ${week_summary.llm_cost:.6f}\n")
                f.write(f"Records Processed: {week_summary.total_records:,}\n")
                f.write(f"Cost per Record: ${week_summary.cost_per_record:.6f}\n")
                f.write(f"Operations: {', '.join(week_summary.operations)}\n")
                f.write(f"States: {', '.join(week_summary.states_processed)}\n\n")
            
            # Month summary
            month_summary = self.get_period_summary(30)
            if month_summary:
                f.write("LAST 30 DAYS\n")
                f.write("-" * 40 + "\n")
                f.write(f"Total Cost: ${month_summary.total_cost:.6f}\n")
                f.write(f"Embedding Cost: ${month_summary.embedding_cost:.6f}\n")
                f.write(f"LLM Cost: ${month_summary.llm_cost:.6f}\n")
                f.write(f"Records Processed: {month_summary.total_records:,}\n")
                f.write(f"Cost per Record: ${month_summary.cost_per_record:.6f}\n")
                f.write(f"Operations: {', '.join(month_summary.operations)}\n")
                f.write(f"States: {', '.join(month_summary.states_processed)}\n\n")
            
            # State breakdown
            state_breakdown = self.get_state_breakdown(30)
            if state_breakdown:
                f.write("COST BY STATE (LAST 30 DAYS)\n")
                f.write("-" * 40 + "\n")
                for state, summary in sorted(state_breakdown.items(), key=lambda x: x[1].total_cost, reverse=True):
                    f.write(f"{state}: ${summary.total_cost:.6f} ({summary.total_records:,} records, ${summary.cost_per_record:.6f}/record)\n")
                f.write("\n")
            
            # Cost projections
            self._add_cost_projections(f, month_summary)
        
        self.logger.info(f"📊 Cost report generated: {output_path}")
        return output_path

    def _add_cost_projections(self, f, month_summary: Optional[CostSummary]):
        """Add cost projections to report"""
        if not month_summary or month_summary.total_records == 0:
            return
        
        f.write("COST PROJECTIONS\n")
        f.write("-" * 40 + "\n")
        
        # Estimate costs for full BR database
        estimated_br_records = {
            'small_states': 1000,      # WY, VT, ND, etc.
            'medium_states': 5000,     # NV, NH, RI, etc.
            'large_states': 15000,     # CA, TX, FL, NY, etc.
            'total_estimate': 250000   # Conservative total estimate
        }
        
        cost_per_record = month_summary.cost_per_record
        
        f.write(f"Based on current cost per record: ${cost_per_record:.6f}\n\n")
        
        for category, records in estimated_br_records.items():
            estimated_cost = records * cost_per_record
            f.write(f"{category.replace('_', ' ').title()}: {records:,} records = ${estimated_cost:.2f}\n")
        
        f.write("\n")

    def export_to_csv(self, days: int = 30, output_file: Optional[str] = None) -> str:
        """Export detailed cost data to CSV"""
        if not output_file:
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            output_file = f"detailed_costs_{timestamp}.csv"
        
        output_path = os.path.join(self.cost_tracking_dir, output_file)
        
        if not os.path.exists(self.detailed_log_file):
            self.logger.warning("No detailed cost log found")
            return ""
        
        # Calculate cutoff date
        cutoff_date = datetime.now() - timedelta(days=days)
        
        records = []
        with open(self.detailed_log_file, 'r') as f:
            for line in f:
                record_data = json.loads(line.strip())
                record_time = datetime.fromisoformat(record_data['timestamp'])
                
                if record_time >= cutoff_date:
                    # Flatten metadata
                    flattened = {**record_data}
                    metadata = flattened.pop('metadata', {})
                    for key, value in metadata.items():
                        flattened[f'metadata_{key}'] = value
                    records.append(flattened)
        
        if records:
            df = pd.DataFrame(records)
            df.to_csv(output_path, index=False)
            self.logger.info(f"📊 Cost data exported: {output_path} ({len(records)} records)")
        else:
            self.logger.warning("No cost records found for the specified period")
        
        return output_path

    def print_quick_summary(self):
        """Print a quick cost summary to console"""
        today_summary = self.get_daily_summary()
        week_summary = self.get_period_summary(7)
        
        print(f"\n{'='*60}")
        print(f"QUICK COST SUMMARY")
        print(f"{'='*60}")
        
        if today_summary:
            print(f"Today: ${today_summary.total_cost:.6f} ({today_summary.total_records:,} records)")
        else:
            print("Today: No activity")
        
        if week_summary:
            print(f"Last 7 days: ${week_summary.total_cost:.6f} ({week_summary.total_records:,} records)")
            print(f"Average per record: ${week_summary.cost_per_record:.6f}")
        else:
            print("Last 7 days: No activity")
        
        print(f"{'='*60}")

def main():
    """CLI for cost tracking utilities"""
    import argparse
    
    parser = argparse.ArgumentParser(description='Cost Tracking Utilities')
    parser.add_argument('action', choices=['summary', 'report', 'export', 'quick'], 
                       help='Action to perform')
    parser.add_argument('--days', type=int, default=7, help='Number of days for analysis')
    parser.add_argument('--output', help='Output filename')
    
    args = parser.parse_args()
    
    tracker = CostTracker()
    
    if args.action == 'summary':
        summary = tracker.get_period_summary(args.days)
        if summary:
            print(f"\nCost Summary ({summary.time_period}):")
            print(f"Total Cost: ${summary.total_cost:.6f}")
            print(f"Records: {summary.total_records:,}")
            print(f"Cost per Record: ${summary.cost_per_record:.6f}")
        else:
            print("No cost data found for the specified period")
    
    elif args.action == 'report':
        report_path = tracker.generate_cost_report(args.output)
        print(f"Report generated: {report_path}")
    
    elif args.action == 'export':
        csv_path = tracker.export_to_csv(args.days, args.output)
        if csv_path:
            print(f"Data exported: {csv_path}")
    
    elif args.action == 'quick':
        tracker.print_quick_summary()

if __name__ == "__main__":
    main()