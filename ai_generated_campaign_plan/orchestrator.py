import asyncio
from dataclasses import dataclass
from datetime import date

from ai_generated_campaign_plan.schema.models import CampaignInfo
from ai_generated_campaign_plan.sections.five_know_your_community import (
    KnowYourCommunityGenerator,
)
from ai_generated_campaign_plan.sections.four_recommended_total_budget import (
    generate_recommended_total_budget,
)
from ai_generated_campaign_plan.sections.one_overview import generate_campaign_overview
from ai_generated_campaign_plan.sections.six_voter_contact_plan import (
    VoterContactPlanGenerator,
)
from ai_generated_campaign_plan.sections.three_campaign_timeline import (
    CampaignTimelineGenerator,
)
from ai_generated_campaign_plan.sections.two_strategic_landscape_electoral_goals import (
    StrategicLandscapeElectoralGoalsGenerator,
)
from ai_generated_campaign_plan.utils.utils import CampaignUtils
from shared.llm import LLMClient
from shared.logger import get_logger


@dataclass
class CostBreakdown:
    """Detailed cost breakdown for campaign plan generation."""

    # LLM costs by provider
    gemini_cost: float = 0.0
    together_cost: float = 0.0
    total_llm_cost: float = 0.0

    # Token usage
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    total_tokens: int = 0

    # Tavily search costs
    tavily_searches: int = 0
    tavily_cost: float = 0.0

    # Total cost
    total_cost: float = 0.0

    def __post_init__(self):
        """Calculate total costs after initialization."""
        self.total_llm_cost = self.gemini_cost + self.together_cost
        self.total_cost = self.total_llm_cost + self.tavily_cost


class CostTracker:
    """Track costs for campaign plan generation including LLM and Tavily."""

    # Pricing per 1M tokens (as of 2024)
    PRICING = {
        'gemini-2.5-flash': {'input': 0.3, 'output': 2.50},  # $0.075 input, $0.30 output per 1M tokens
        'deepseek-ai/DeepSeek-V3': {'input': 1.25, 'output': 1.25},  # TogetherAI pricing
        'tavily_search': 0.008  # $0.001 per search
    }

    def __init__(self):
        self.logger = get_logger(__name__)
        self.llm_costs_by_provider = {}
        self.tavily_searches = 0
        self.section_costs = {}

    def calculate_llm_cost(self, provider_name: str, model_name: str, prompt_tokens: int, completion_tokens: int) -> float:
        """Calculate cost for LLM usage."""
        model_key = model_name if model_name in self.PRICING else 'gemini-2.5-flash'

        if provider_name not in self.llm_costs_by_provider:
            self.llm_costs_by_provider[provider_name] = {'prompt_tokens': 0, 'completion_tokens': 0, 'cost': 0.0}

        pricing = self.PRICING[model_key]
        input_cost = (prompt_tokens / 1_000_000) * pricing['input']
        output_cost = (completion_tokens / 1_000_000) * pricing['output']
        total_cost = input_cost + output_cost

        self.llm_costs_by_provider[provider_name]['prompt_tokens'] += prompt_tokens
        self.llm_costs_by_provider[provider_name]['completion_tokens'] += completion_tokens
        self.llm_costs_by_provider[provider_name]['cost'] += total_cost

        self.logger.debug(f"LLM cost calculated: {provider_name} - ${total_cost:.6f} (input: ${input_cost:.6f}, output: ${output_cost:.6f})")
        return total_cost

    def track_tavily_search(self, num_searches: int = 1) -> float:
        """Track Tavily search cost."""
        cost = num_searches * self.PRICING['tavily_search']
        self.tavily_searches += num_searches
        self.logger.debug(f"Tavily searches tracked: {num_searches}, cost: ${cost:.6f}")
        return cost

    def get_cost_breakdown(self) -> CostBreakdown:
        """Get detailed cost breakdown."""
        breakdown = CostBreakdown()

        # LLM costs
        for provider, data in self.llm_costs_by_provider.items():
            breakdown.total_prompt_tokens += data['prompt_tokens']
            breakdown.total_completion_tokens += data['completion_tokens']

            if 'gemini' in provider.lower():
                breakdown.gemini_cost += data['cost']
            elif 'together' in provider.lower():
                breakdown.together_cost += data['cost']

        breakdown.total_tokens = breakdown.total_prompt_tokens + breakdown.total_completion_tokens

        # Tavily costs
        breakdown.tavily_searches = self.tavily_searches
        breakdown.tavily_cost = self.tavily_searches * self.PRICING['tavily_search']

        # Recalculate totals
        breakdown.__post_init__()

        return breakdown


class CampaignPlanOrchestrator:
    """
    Orchestrates the generation of a complete campaign plan by coordinating
    data processing and section generation across all modules.
    """

    def __init__(self, llm_client: LLMClient | None = None):
        """
        Initialize the orchestrator with necessary utilities and generators.
        
        Args:
            llm_client: Optional LLM client to share across components
        """
        self.logger = get_logger(__name__)
        self.llm_client = llm_client or LLMClient()
        self.cost_tracker = CostTracker()

        # Initialize utilities and generators with shared LLM client
        self.campaign_utils = CampaignUtils(self.llm_client)
        self.strategic_generator = StrategicLandscapeElectoralGoalsGenerator()
        self.timeline_generator = CampaignTimelineGenerator()
        self.community_generator = KnowYourCommunityGenerator()
        self.voter_contact_plan_generator = VoterContactPlanGenerator()

        # Set shared LLM client for generators that support it
        if hasattr(self.strategic_generator, 'llm_client'):
            self.strategic_generator.llm_client = self.llm_client
        if hasattr(self.timeline_generator, 'llm_client'):
            self.timeline_generator.llm_client = self.llm_client
        if hasattr(self.community_generator, 'llm_client'):
            self.community_generator.llm_client = self.llm_client
        if hasattr(self.voter_contact_plan_generator, 'llm_client'):
            self.voter_contact_plan_generator.llm_client = self.llm_client

        self.logger.info("CampaignPlanOrchestrator initialized with cost tracking")

    def _track_llm_usage(self, section_name: str):
        """Track LLM usage for a specific section."""
        stats = self.llm_client.get_usage_since_last_reset()

        # Get the actual provider and model used
        provider_name = stats.get('last_provider_used', 'unknown')
        model_name = stats.get('last_model_used', 'unknown')

        prompt_tokens = stats.get('total_prompt_tokens', 0)
        completion_tokens = stats.get('total_completion_tokens', 0)

        if prompt_tokens > 0 or completion_tokens > 0:
            cost = self.cost_tracker.calculate_llm_cost(
                provider_name, model_name, prompt_tokens, completion_tokens
            )
            self.logger.info(f"Section {section_name} cost: ${cost:.6f} ({provider_name}, {prompt_tokens + completion_tokens:,} tokens)")
        else:
            self.logger.debug(f"Section {section_name}: No LLM usage detected")

        # Reset for next section
        self.llm_client.reset_token_usage_stats()

    def _track_tavily_usage(self, section_name: str, num_searches: int):
        """Track Tavily search usage for a specific section."""
        cost = self.cost_tracker.track_tavily_search(num_searches)
        self.logger.debug(f"Section {section_name} Tavily cost: ${cost:.6f} ({num_searches} searches)")

    def get_generation_cost_report(self) -> str:
        """Generate a detailed cost report for the campaign plan generation."""
        breakdown = self.cost_tracker.get_cost_breakdown()

        report = f"""
CAMPAIGN PLAN GENERATION COST REPORT
═══════════════════════════════════════

LLM COSTS:
  Gemini (Primary):     ${breakdown.gemini_cost:.6f}
  TogetherAI (Fallback): ${breakdown.together_cost:.6f}
  Total LLM Cost:       ${breakdown.total_llm_cost:.6f}

TOKEN USAGE:
  Prompt Tokens:        {breakdown.total_prompt_tokens:,}
  Completion Tokens:    {breakdown.total_completion_tokens:,}
  Total Tokens:         {breakdown.total_tokens:,}

SEARCH COSTS:
  Tavily Searches:      {breakdown.tavily_searches} searches
  Search Cost:          ${breakdown.tavily_cost:.6f}

TOTAL GENERATION COST:  ${breakdown.total_cost:.6f}

Note: Costs are estimates based on current pricing as of 2024.
Actual costs may vary based on provider pricing changes.
"""
        return report

    async def generate_complete_campaign_plan(self, campaign_info: CampaignInfo) -> str:
        """
        Generate a complete campaign plan from raw campaign information.
        
        Args:
            campaign_info: Raw campaign information input
            
        Returns:
            str: Complete formatted campaign plan document
            
        Raises:
            Exception: If plan generation fails at any stage
        """
        self.logger.info(f"Starting complete campaign plan generation for {campaign_info.candidate_name}")
        self.logger.debug(f"Campaign info: {campaign_info}")

        try:
            # Step 1: Clean and enhance campaign data
            self.logger.info("Step 1: Cleaning and enhancing campaign data")
            cleaned_campaign_info = self.campaign_utils.clean_campaign_info(campaign_info)
            self.logger.info("Successfully cleaned data")
            self.logger.debug(f"cleaned_campaign_info: {cleaned_campaign_info}")

            self.logger.info("generating contact strategies")

            if cleaned_campaign_info.has_primary:
                self.logger.info("Generating contact strategies for primary and general elections")
                primary_contact_strategy = self.campaign_utils.optimize_contact_strategy(
                    date.today(),
                    cleaned_campaign_info.primary_date
                )
                general_contact_strategy = self.campaign_utils.optimize_contact_strategy(
                    cleaned_campaign_info.primary_date,
                    cleaned_campaign_info.election_date
                )
                self.logger.info("Successfully generated both primary and general contact strategies")
                self.logger.debug(f"primary_contact_strategy: {primary_contact_strategy}")
                self.logger.debug(f"general_contact_strategy: {general_contact_strategy}")
            else:
                self.logger.info("Generating contact strategy for general election only")
                general_contact_strategy = self.campaign_utils.optimize_contact_strategy(
                    date.today(),
                    cleaned_campaign_info.election_date
                )
                primary_contact_strategy = None
                self.logger.info("Successfully generated general contact strategy")
                self.logger.debug(f"general_contact_strategy: {general_contact_strategy}")

            # Step 2: Generate individual sections
            self.logger.info("Step 2: Generating campaign plan sections")

            sections = {}

            # Generate sections 1, 2, 4, 5, and 6 in parallel
            self.logger.info("Generating sections 1, 2, 4, 5, and 6 in parallel")

            async def generate_section_1():
                try:
                    self.logger.debug("Generating Section 1: Overview")
                    result = generate_campaign_overview(
                        incumbent_status=campaign_info.incumbent_status,
                        office_and_jurisdiction=campaign_info.office_and_jurisdiction
                    )
                    self._track_llm_usage("Section 1")
                    self.logger.info("✓ Section 1: Overview complete")
                    return result
                except Exception as e:
                    self.logger.error(f"Failed to generate Section 1: {e!s}")
                    self.logger.debug(f"Section 1 error details: {type(e).__name__}: {e}", exc_info=True)
                    return "1. OVERVIEW\n\nSection could not be generated due to an error."

            async def generate_section_2():
                try:
                    self.logger.debug("Generating Section 2: Strategic Landscape & Electoral Goals")
                    result = self.strategic_generator.generate_section(campaign_info)
                    self._track_llm_usage("Section 2")
                    self.logger.info("✓ Section 2: Strategic Landscape & Electoral Goals complete")
                    return result
                except Exception as e:
                    self.logger.error(f"Failed to generate Section 2: {e!s}")
                    self.logger.debug(f"Section 2 error details: {type(e).__name__}: {e}", exc_info=True)
                    return "2. STRATEGIC LANDSCAPE & ELECTORAL GOALS\n\nSection could not be generated due to an error."

            async def generate_section_4():
                try:
                    self.logger.debug("Generating Section 4: Recommended Total Budget")
                    result = generate_recommended_total_budget(cleaned_campaign_info)
                    self._track_llm_usage("Section 4")
                    self.logger.info("✓ Section 4: Recommended Total Budget complete")
                    return result
                except Exception as e:
                    self.logger.error(f"Failed to generate Section 4: {e!s}")
                    self.logger.debug(f"Section 4 error details: {type(e).__name__}: {e}", exc_info=True)
                    return "4. RECOMMENDED TOTAL BUDGET\n\nSection could not be generated due to an error."

            async def generate_section_5():
                try:
                    self.logger.debug("Generating Section 5: Know Your Community")
                    result = await self.community_generator.generate_section(cleaned_campaign_info)
                    self._track_llm_usage("Section 5")
                    # Estimate Tavily searches for community section (typically 4-5 searches)
                    self._track_tavily_usage("Section 5", 4)
                    self.logger.info("✓ Section 5: Know Your Community complete")
                    return result
                except Exception as e:
                    self.logger.error(f"Failed to generate Section 5: {e!s}")
                    self.logger.debug(f"Section 5 error details: {type(e).__name__}: {e}", exc_info=True)
                    return "5. KNOW YOUR COMMUNITY\n\nSection could not be generated due to an error."

            async def generate_section_6():
                try:
                    self.logger.debug("Generating Section 6: Voter Contact Plan")
                    result = await self.voter_contact_plan_generator.generate_section(cleaned_campaign_info, primary_contact_strategy, general_contact_strategy)
                    self._track_llm_usage("Section 6")
                    self.logger.info("✓ Section 6: Voter Contact Plan complete")
                    return result
                except Exception as e:
                    self.logger.error(f"Failed to generate Section 6: {e!s}")
                    self.logger.debug(f"Section 6 error details: {type(e).__name__}: {e}", exc_info=True)
                    return "6. VOTER CONTACT PLAN\n\nSection could not be generated due to an error."

            # Execute all independent sections in parallel
            section_results = await asyncio.gather(
                generate_section_1(),
                generate_section_2(),
                generate_section_4(),
                generate_section_5(),
                generate_section_6()
            )

            # Assign results to sections dictionary
            sections[1] = section_results[0]
            sections[2] = section_results[1]
            sections[4] = section_results[2]
            sections[5] = section_results[3]
            sections[6] = section_results[4]

            # Log summary of section generation results
            failed_sections = []
            successful_sections = []
            for section_num, content in [(1, sections[1]), (2, sections[2]), (4, sections[4]), (5, sections[5]), (6, sections[6])]:
                if "Section could not be generated due to an error." in content:
                    failed_sections.append(section_num)
                else:
                    successful_sections.append(section_num)

            if successful_sections:
                self.logger.info(f"✓ Successfully generated sections: {', '.join(map(str, successful_sections))}")
            if failed_sections:
                self.logger.warning(f"✗ Failed to generate sections: {', '.join(map(str, failed_sections))}")

            self.logger.info("✓ All independent sections (1, 2, 4, 5, 6) completed in parallel")

            # Section 3: Campaign Timeline (depends on sections 5 and 6)
            self.logger.debug("Generating Section 3: Campaign Timeline")
            try:
                sections[3] = await self.timeline_generator.generate_section(cleaned_campaign_info, sections[5], sections[6])
                self._track_llm_usage("Section 3")
                # Estimate Tavily searches for timeline section (typically 1-2 searches for ballot dates)
                self._track_tavily_usage("Section 3", 2)
                self.logger.info("✓ Section 3: Campaign Timeline complete")
            except Exception as e:
                self.logger.error(f"Failed to generate Section 3: {e!s}")
                self.logger.debug(f"Section 3 error details: {type(e).__name__}: {e}", exc_info=True)
                sections[3] = "3. CAMPAIGN TIMELINE\n\nSection could not be generated due to an error."



            # Step 3: Assemble final document
            self.logger.info("Step 3: Assembling final campaign plan document")
            final_plan = self._assemble_final_document(campaign_info, sections)

            # Generate and log cost report
            cost_report = self.get_generation_cost_report()
            self.logger.info("Campaign plan generation completed - Cost Summary:")
            self.logger.info(cost_report)

            # Get final cost breakdown for return
            cost_breakdown = self.cost_tracker.get_cost_breakdown()

            self.logger.info(f"Successfully generated complete campaign plan for {campaign_info.candidate_name}")
            self.logger.info(f"Total generation cost: ${cost_breakdown.total_cost:.6f}")
            self.logger.debug(f"Final document length: {len(final_plan)} characters")


            return final_plan

        except Exception as e:
            self.logger.error(f"Failed to generate campaign plan: {e!s}")
            self.logger.debug(f"Exception details: {type(e).__name__}: {e}", exc_info=True)
            raise

    def _assemble_final_document(self, campaign_info: CampaignInfo, sections: dict) -> str:
        """
        Assemble individual sections into a complete campaign plan document.
        
        Args:
            campaign_info: Original campaign information for header
            sections: Dictionary of generated sections keyed by section number
            
        Returns:
            str: Complete formatted campaign plan document
        """
        self.logger.debug("Assembling final campaign plan document")

        # Document header
        header = f"""CAMPAIGN PLAN
{campaign_info.candidate_name}
{campaign_info.office_and_jurisdiction}
Election Date: {campaign_info.election_date.strftime('%B %d, %Y')}
{"Primary Date: " + campaign_info.primary_date.strftime('%B %d, %Y') if campaign_info.primary_date else "No Primary Election"}

Generated on: {date.today().strftime('%B %d, %Y')}

═══════════════════════════════════════════════════════════════════

"""

        # Process all sections with consistent numbering
        formatted_sections = []

        for section_num in sorted(sections.keys()):
            section_content = sections[section_num]

            formatted_sections.append(section_content)

        # Combine all parts
        final_document = header + "\n\n".join(formatted_sections)

        self.logger.debug(f"Assembled document with {len(sections)} sections")
        return final_document

    def generate_campaign_plan_sync(self, campaign_info: CampaignInfo) -> str:
        """
        Synchronous wrapper for generating campaign plan (runs async code).
        
        Args:
            campaign_info: Raw campaign information input
            
        Returns:
            str: Complete formatted campaign plan document
        """
        self.logger.info("Running campaign plan generation in synchronous mode")
        return asyncio.run(self.generate_complete_campaign_plan(campaign_info))

    async def generate_campaign_plan_with_costs(self, campaign_info: CampaignInfo) -> tuple[str, CostBreakdown]:
        """
        Generate campaign plan and return both the plan and detailed cost breakdown.
        
        Args:
            campaign_info: Raw campaign information input
            
        Returns:
            tuple: (campaign_plan, cost_breakdown)
        """
        self.logger.info("Generating campaign plan with detailed cost tracking")

        campaign_plan = await self.generate_complete_campaign_plan(campaign_info)
        cost_breakdown = self.cost_tracker.get_cost_breakdown()

        return campaign_plan, cost_breakdown

    def generate_campaign_plan_with_costs_sync(self, campaign_info: CampaignInfo) -> tuple[str, CostBreakdown]:
        """
        Synchronous wrapper for generating campaign plan with costs.
        
        Args:
            campaign_info: Raw campaign information input
            
        Returns:
            tuple: (campaign_plan, cost_breakdown)
        """
        self.logger.info("Running campaign plan generation with cost tracking in synchronous mode")
        return asyncio.run(self.generate_campaign_plan_with_costs(campaign_info))


if __name__ == "__main__":
    logger = get_logger(__name__)
    logger.info("Starting campaign plan orchestrator in standalone mode")

    try:
        from ai_generated_campaign_plan.schema.models import IncumbentStatus, RaceType

        # Create example campaign
        example_campaign = CampaignInfo(
            candidate_name="Sarah Johnson",
            primary_date=date(2025, 9, 15),
            election_date=date(2025, 11, 5),
            office_and_jurisdiction="School Board, At-Large, Chicopee, MA",
            incumbent_status=IncumbentStatus.NOT_APPLICABLE,
            race_type=RaceType.NONPARTISAN,
            seats_available=3,
            number_of_opponents=7,
            win_number=2500,
            total_likely_voters=8500,
            available_cell_phones=1200,
            available_landlines=300,
            additional_race_context="Focus on education funding and infrastructure improvements"
        )

        logger.info(f"Generating campaign plan for {example_campaign.candidate_name}")

        # Generate plan
        orchestrator = CampaignPlanOrchestrator()
        campaign_plan, cost_breakdown = orchestrator.generate_campaign_plan_with_costs_sync(example_campaign)

        print("=" * 80)
        print("GENERATED CAMPAIGN PLAN")
        print("=" * 80)
        print(campaign_plan)
        print("=" * 80)

        print("\n" + "=" * 80)
        print("COST BREAKDOWN")
        print("=" * 80)
        print(f"Total Cost: ${cost_breakdown.total_cost:.6f}")
        print(f"LLM Cost: ${cost_breakdown.total_llm_cost:.6f}")
        print(f"  - Gemini: ${cost_breakdown.gemini_cost:.6f}")
        print(f"  - TogetherAI: ${cost_breakdown.together_cost:.6f}")
        print(f"Tavily Searches: {cost_breakdown.tavily_searches} (${cost_breakdown.tavily_cost:.6f})")
        print(f"Total Tokens: {cost_breakdown.total_tokens:,}")
        print(f"  - Prompt: {cost_breakdown.total_prompt_tokens:,}")
        print(f"  - Completion: {cost_breakdown.total_completion_tokens:,}")
        print("=" * 80)

        logger.info("Successfully completed campaign plan generation example with cost tracking")

    except Exception as e:
        logger.error(f"Orchestrator execution failed: {e!s}")
        logger.debug(f"Exception in main: {type(e).__name__}: {e}", exc_info=True)
        raise
