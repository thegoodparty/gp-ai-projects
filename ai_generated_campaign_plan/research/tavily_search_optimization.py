#!/usr/bin/env python3
"""
AI-Judged Tavily Search Optimization Experiment

This script tests different search queries and uses DeepSeek R1 to evaluate 
the quality of results for generating community events content like:

Community Events & Civic Presence
- City Commission & Parks Meetings (June 12 & 16): opportunities to speak or attend.
- June 24 "Save a Life" / Veterans Event: civic visibility and voter interaction.
- Library & Bookmobile Events (June 11–17): reach families and educators.
- Chicopee Chamber: June 9, 11, 27 networking and visibility events.
"""

import asyncio
import json
import os
from datetime import datetime
from typing import Any

from dotenv import load_dotenv
from pydantic import BaseModel, Field
from tavily import TavilyClient
from together import Together

load_dotenv()

class SearchEvaluation(BaseModel):
    """AI evaluation of search results"""
    relevance_score: int = Field(description="Relevance score from 1-10 for community events", ge=1, le=10)
    actionability_score: int = Field(description="How actionable are these results for campaign planning (1-10)", ge=1, le=10)
    date_specificity_score: int = Field(description="How specific are the dates mentioned (1-10)", ge=1, le=10)
    civic_engagement_score: int = Field(description="Suitability for civic engagement opportunities (1-10)", ge=1, le=10)
    overall_score: int = Field(description="Overall usefulness score (1-10)", ge=1, le=10)
    reasoning: str = Field(description="Detailed reasoning for the scores")
    key_findings: list[str] = Field(description="Key findings from the search results", max_items=5)
    missing_elements: list[str] = Field(description="What's missing for ideal campaign planning", max_items=10)

class SearchOptimizer:
    def __init__(self):
        self.client = TavilyClient(api_key=os.getenv("TAVILY_API_KEY"))
        self.together_client = Together()
        self.results = []

    def generate_search_variations(self, city: str = "chicopee", state: str = "massachusetts",
                                 year: str = "2025") -> list[dict[str, Any]]:
        """Generate focused search query variations to test across multiple months"""

        test_months = ["july", "august", "september", "october", "november"]

        search_configs = []

        for month in test_months:
            query = f"{city} {state} community events {month} {year} with dates"

            search_configs.extend([
                {"query": query, "search_depth": "basic", "max_results": 3, "month": month},
                # {"query": query, "search_depth": "basic", "max_results": 5, "month": month},
            ])

            search_configs.append({
                "query": query,
                "search_depth": "advanced",
                "max_results": 1,
                "month": month
            })

        return search_configs

    async def evaluate_search_with_ai(self, search_config: dict[str, Any], search_response: dict[str, Any]) -> SearchEvaluation:
        """Use DeepSeek R1 to evaluate search results with reasoning"""

        results_text = ""
        if "results" in search_response:
            for i, result in enumerate(search_response["results"], 1):
                results_text += f"\n--- Result {i} ---\n"
                results_text += f"Title: {result.get('title', 'N/A')}\n"
                results_text += f"Content: {result.get('content', 'N/A')[:500]}...\n"
                results_text += f"URL: {result.get('url', 'N/A')}\n"

        evaluation_prompt = f"""
You are evaluating search results for a political campaign's community engagement planning. 

SEARCH QUERY: {search_config['query']}
SEARCH DEPTH: {search_config['search_depth']}
MAX RESULTS: {search_config['max_results']}

TARGET GOAL: Find community events and civic engagement opportunities like:
- City Commission & Parks Meetings with specific dates
- Veterans Events with civic visibility potential  
- Library & Bookmobile Events for family/educator outreach
- Chamber of Commerce networking events

SEARCH RESULTS:
{results_text}

Evaluate these results on:
1. RELEVANCE: How well do results match community events/civic engagement?
2. ACTIONABILITY: Can a campaign actually use this information?
3. DATE SPECIFICITY: Are there specific dates/times mentioned?
4. CIVIC ENGAGEMENT: How suitable are these for campaign visibility?
5. OVERALL USEFULNESS: General quality for campaign planning

Provide scores 1-10 for each category plus detailed reasoning.
"""

        try:
            result = self.together_client.chat.completions.create(
                messages=[
                    {
                        "role": "system",
                        "content": "You are an expert campaign strategist evaluating search results for community engagement opportunities. Provide detailed, analytical evaluations with specific reasoning. Only respond in JSON format.",
                    },
                    {
                        "role": "user",
                        "content": evaluation_prompt,
                    },
                ],
                model="deepseek-ai/DeepSeek-R1",
                response_format={
                    "type": "json_schema",
                    "schema": SearchEvaluation.model_json_schema(),
                },
                max_tokens=1000,
            )

            output = json.loads(result.choices[0].message.content)
            return SearchEvaluation(**output)

        except Exception as e:
            print(f"❌ Error in AI evaluation: {e!s}")
            return SearchEvaluation(
                relevance_score=1,
                actionability_score=1,
                date_specificity_score=1,
                civic_engagement_score=1,
                overall_score=1,
                reasoning=f"Error in AI evaluation: {e!s}",
                key_findings=["Evaluation failed"],
                missing_elements=["AI evaluation unavailable"]
            )

    async def run_search_experiment(self, search_configs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Run all search variations and collect AI-evaluated results"""
        print(f"🚀 Starting AI-judged search optimization with {len(search_configs)} configurations...")

        results = []
        for i, config in enumerate(search_configs, 1):
            print(f"\n🔍 Running search {i}/{len(search_configs)}")
            print(f"Month: {config.get('month', 'N/A').title()}")
            print(f"Query: {config['query'][:60]}...")
            print(f"Depth: {config['search_depth']}, Max Results: {config.get('max_results', 1)}")

            try:
                response = self.client.search(**config)

                print(f"✅ Search completed: {len(response.get('results', []))} results")
                print("🤖 Evaluating with DeepSeek R1...")

                evaluation = await self.evaluate_search_with_ai(config, response)

                result = {
                    "config": config,
                    "response": response,
                    "ai_evaluation": evaluation.model_dump(),
                    "result_count": len(response.get("results", [])),
                    "timestamp": datetime.now().isoformat(),
                    "success": True
                }

                print(f"🎯 AI Scores - Overall: {evaluation.overall_score}/10, Relevance: {evaluation.relevance_score}/10, Actionability: {evaluation.actionability_score}/10")
                print(f"📝 AI Reasoning: {evaluation.reasoning[:100]}...")

            except Exception as e:
                result = {
                    "config": config,
                    "error": str(e),
                    "success": False,
                    "timestamp": datetime.now().isoformat()
                }
                print(f"❌ Error: {e!s}")

            results.append(result)

            await asyncio.sleep(1.0)

        return results

    def analyze_results(self, results: list[dict[str, Any]]) -> dict[str, Any]:
        """Analyze AI-evaluated results"""
        print("\n📊 Analyzing AI evaluations...")

        successful_results = [r for r in results if r.get("success", False)]

        if not successful_results:
            return {"error": "No successful results to analyze"}

        successful_results.sort(key=lambda x: x["ai_evaluation"]["overall_score"], reverse=True)

        overall_scores = [r["ai_evaluation"]["overall_score"] for r in successful_results]
        relevance_scores = [r["ai_evaluation"]["relevance_score"] for r in successful_results]
        actionability_scores = [r["ai_evaluation"]["actionability_score"] for r in successful_results]

        analysis = {
            "total_searches": len(results),
            "successful_searches": len(successful_results),
            "score_statistics": {
                "overall_avg": sum(overall_scores) / len(overall_scores),
                "relevance_avg": sum(relevance_scores) / len(relevance_scores),
                "actionability_avg": sum(actionability_scores) / len(actionability_scores),
                "max_overall": max(overall_scores),
                "min_overall": min(overall_scores)
            },
            "top_5_configs": [r["config"] for r in successful_results[:5]],
            "top_5_evaluations": [r["ai_evaluation"] for r in successful_results[:5]],
            "best_search_patterns": self._analyze_search_patterns(successful_results),
            "common_findings": self._extract_common_findings(successful_results),
            "improvement_suggestions": self._extract_improvement_suggestions(successful_results)
        }

        return analysis

    def _analyze_search_patterns(self, results: list[dict[str, Any]]) -> dict[str, Any]:
        """Analyze which search patterns perform best"""
        pattern_performance = {}
        month_performance = {}

        for result in results:
            query = result["config"]["query"]
            depth = result["config"]["search_depth"]
            max_results = result["config"].get("max_results", 1)
            month = result["config"].get("month", "unknown")

            key = f"{depth}_{max_results}"
            if key not in pattern_performance:
                pattern_performance[key] = []

            pattern_performance[key].append(result["ai_evaluation"]["overall_score"])

            if month not in month_performance:
                month_performance[month] = []
            month_performance[month].append(result["ai_evaluation"]["overall_score"])

        pattern_averages = {}
        for pattern, scores in pattern_performance.items():
            pattern_averages[pattern] = {
                "avg_score": sum(scores) / len(scores),
                "count": len(scores),
                "max_score": max(scores)
            }

        month_averages = {}
        for month, scores in month_performance.items():
            month_averages[month] = {
                "avg_score": sum(scores) / len(scores),
                "count": len(scores),
                "max_score": max(scores)
            }

        return {
            "patterns": dict(sorted(pattern_averages.items(), key=lambda x: x[1]["avg_score"], reverse=True)),
            "months": dict(sorted(month_averages.items(), key=lambda x: x[1]["avg_score"], reverse=True))
        }

    def _extract_common_findings(self, results: list[dict[str, Any]]) -> list[str]:
        """Extract common findings across evaluations"""
        all_findings = []
        for result in results:
            all_findings.extend(result["ai_evaluation"]["key_findings"])

        finding_counts = {}
        for finding in all_findings:
            key = finding.lower()[:50]  # First 50 chars as key
            finding_counts[key] = finding_counts.get(key, 0) + 1

        common_findings = sorted(finding_counts.items(), key=lambda x: x[1], reverse=True)[:5]
        return [finding[0] for finding in common_findings]

    def _extract_improvement_suggestions(self, results: list[dict[str, Any]]) -> list[str]:
        """Extract improvement suggestions from AI evaluations"""
        all_missing = []
        for result in results:
            all_missing.extend(result["ai_evaluation"]["missing_elements"])

        missing_counts = {}
        for missing in all_missing:
            key = missing.lower()[:50]
            missing_counts[key] = missing_counts.get(key, 0) + 1

        common_missing = sorted(missing_counts.items(), key=lambda x: x[1], reverse=True)[:5]
        return [missing[0] for missing in common_missing]

    def save_results(self, results: list[dict[str, Any]], analysis: dict[str, Any], filename: str = None):
        """Save results to a JSON file in the results directory"""
        import os

        # Create results directory if it doesn't exist
        results_dir = "./results"
        os.makedirs(results_dir, exist_ok=True)

        if filename is None:
            filename = f"tavily_ai_evaluation_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"

        # Construct full path
        full_path = os.path.join(results_dir, filename)

        output = {
            "experiment_metadata": {
                "timestamp": datetime.now().isoformat(),
                "evaluation_method": "DeepSeek R1 AI Evaluation",
                "total_searches": len(results),
                "successful_searches": len([r for r in results if r.get("success", False)])
            },
            "analysis": analysis,
            "detailed_results": results
        }

        with open(full_path, 'w') as f:
            json.dump(output, f, indent=2, default=str)

        print(f"\n💾 Results saved to: {full_path}")
        return full_path

    def print_recommendations(self, analysis: dict[str, Any]):
        """Print AI-based optimization recommendations"""
        print("\n" + "="*60)
        print("🎯 AI-EVALUATED SEARCH OPTIMIZATION RECOMMENDATIONS")
        print("="*60)

        if "error" in analysis:
            print(f"❌ {analysis['error']}")
            return

        stats = analysis['score_statistics']
        print("\n📈 Score Statistics:")
        print(f"   - Average Overall Score: {stats['overall_avg']:.1f}/10")
        print(f"   - Average Relevance: {stats['relevance_avg']:.1f}/10")
        print(f"   - Average Actionability: {stats['actionability_avg']:.1f}/10")
        print(f"   - Best Score Achieved: {stats['max_overall']}/10")

        print("\n🏆 Top 3 AI-Rated Configurations:")
        for i, (config, evaluation) in enumerate(zip(analysis['top_5_configs'][:3], analysis['top_5_evaluations'][:3], strict=False), 1):
            print(f"\n   {i}. Overall Score: {evaluation['overall_score']}/10")
            print(f"      Query: {config['query']}")
            print(f"      Month: {config.get('month', 'N/A')}")
            print(f"      Depth: {config['search_depth']}, Max Results: {config.get('max_results', 1)}")
            print(f"      AI Reasoning: {evaluation['reasoning'][:150]}...")
            print(f"      Key Findings: {', '.join(evaluation['key_findings'][:2])}")

        print("\n📊 Best Search Patterns:")
        for pattern, data in list(analysis['best_search_patterns']['patterns'].items())[:3]:
            print(f"   - {pattern}: Avg {data['avg_score']:.1f}/10 (max: {data['max_score']}/10)")

        print("\n📅 Performance by Month:")
        for month, data in analysis['best_search_patterns']['months'].items():
            print(f"   - {month.title()}: Avg {data['avg_score']:.1f}/10 (max: {data['max_score']}/10, {data['count']} tests)")

        print("\n🔍 Common Findings Across Searches:")
        for finding in analysis['common_findings'][:3]:
            print(f"   - {finding}")

        print("\n💡 Improvement Suggestions:")
        for suggestion in analysis['improvement_suggestions'][:3]:
            print(f"   - {suggestion}")


async def main():
    """Run the AI-evaluated search optimization experiment"""
    optimizer = SearchOptimizer()

    search_configs = optimizer.generate_search_variations()

    print(f"🎯 Generated {len(search_configs)} search configurations for AI evaluation")

    results = await optimizer.run_search_experiment(search_configs)

    analysis = optimizer.analyze_results(results)

    filename = optimizer.save_results(results, analysis)

    optimizer.print_recommendations(analysis)

    print(f"\n🎉 AI-evaluated experiment completed! Check {filename} for detailed results.")


if __name__ == "__main__":
    asyncio.run(main())
