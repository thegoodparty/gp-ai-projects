# Question-Type Routing Architecture

## Overview

Extend the v1_pipeline to handle multiple question types:
- **Open-ended**: Free-form text responses (current behavior)
- **Multiple choice**: Select from predefined options (1-5, A/B/C, Yes/No)
- **Rating scale**: Numeric ratings (1-10, star ratings)
- **Mixed**: Structured choice + open-ended explanation

## Current vs Proposed Flow

```
CURRENT FLOW:
┌─────────┐    ┌───────────────┐    ┌────────────────┐    ┌─────────┐
│ CSV     │───▶│ Consolidation │───▶│ Hierarchical   │───▶│ Output  │
│ Input   │    │               │    │ Clustering     │    │         │
└─────────┘    └───────────────┘    └────────────────┘    └─────────┘
                                           │
                                    (assumes all open-ended)


PROPOSED FLOW:
┌─────────┐    ┌───────────────┐    ┌──────────────┐
│ CSV     │───▶│ Consolidation │───▶│ Question     │
│ Input   │    │ + Poll Meta   │    │ Type Router  │
└─────────┘    └───────────────┘    └──────┬───────┘
                                           │
              ┌────────────────────────────┼────────────────────────────┐
              │                            │                            │
              ▼                            ▼                            ▼
    ┌─────────────────┐        ┌─────────────────┐        ┌─────────────────┐
    │ OPEN-ENDED      │        │ STRUCTURED      │        │ MIXED           │
    │ Processor       │        │ Processor       │        │ Processor       │
    │                 │        │                 │        │                 │
    │ • Clustering    │        │ • Aggregation   │        │ • Split response│
    │ • Theme extract │        │ • Cross-tabs    │        │ • Route parts   │
    │ • LLM analysis  │        │ • Distribution  │        │ • Merge results │
    └────────┬────────┘        └────────┬────────┘        └────────┬────────┘
              │                            │                            │
              └────────────────────────────┼────────────────────────────┘
                                           ▼
                              ┌─────────────────────┐
                              │ Unified Output      │
                              │ Formatter           │
                              └─────────────────────┘
```

## Data Model Changes

### 1. Poll Metadata (New)

```python
@dataclass
class PollQuestion:
    """Metadata for a single poll question"""
    question_id: str
    question_text: str
    question_type: QuestionType  # enum: OPEN_ENDED, MULTIPLE_CHOICE, RATING_SCALE, MIXED

    # For structured questions
    options: list[QuestionOption] | None = None  # e.g., [Option(value="1", label="Roads"), ...]
    scale_min: int | None = None  # For rating scales
    scale_max: int | None = None

    # For mixed questions
    has_follow_up: bool = False
    follow_up_prompt: str | None = None  # e.g., "Please explain your choice"


class QuestionType(Enum):
    OPEN_ENDED = "open_ended"
    MULTIPLE_CHOICE = "multiple_choice"
    RATING_SCALE = "rating_scale"
    YES_NO = "yes_no"
    MIXED = "mixed"  # Structured + explanation


@dataclass
class QuestionOption:
    """A single option in a multiple choice question"""
    value: str      # What user might type: "1", "A", "Roads"
    label: str      # Display label: "Fix the roads"
    position: int   # Order in list
```

### 2. Extended ConsolidatedMessage

```python
@dataclass
class ConsolidatedMessage:
    # ... existing fields ...

    # NEW: Question context
    question_id: str | None = None
    question_type: QuestionType = QuestionType.OPEN_ENDED
    question_text: str | None = None

    # NEW: Parsed response (for structured)
    selected_option: str | None = None      # The structured choice: "2", "B", "Yes"
    selected_option_label: str | None = None  # Resolved label: "Schools"
    follow_up_text: str | None = None       # Open-ended explanation if mixed
```

### 3. Question-Aware Output

```python
@dataclass
class QuestionAnalysisResult:
    """Results for a single question across all respondents"""
    question_id: str
    question_type: QuestionType
    question_text: str
    total_responses: int

    # For structured questions
    option_distribution: dict[str, OptionStats] | None = None

    # For open-ended questions
    cluster_analysis: list[ClusterTheme] | None = None

    # For mixed questions - both
    structured_distribution: dict[str, OptionStats] | None = None
    explanation_themes: dict[str, list[ClusterTheme]] | None = None  # Themes per option


@dataclass
class OptionStats:
    """Statistics for a single option"""
    option_value: str
    option_label: str
    count: int
    percentage: float

    # Demographics breakdown
    by_age_group: dict[str, int] | None = None
    by_location: dict[str, int] | None = None
    by_income: dict[str, int] | None = None
```

## Processing Strategies by Type

### 1. Open-Ended (Current Behavior)
```
Response: "The roads are terrible and taxes are too high"

Processing:
  → Hierarchical clustering
  → Theme extraction via LLM
  → Sentiment analysis

Output:
  cluster_id: 5
  theme: "Infrastructure & Fiscal Concerns"
  sentiment: "negative"
```

### 2. Multiple Choice
```
Question: "What's your top priority? 1=Roads 2=Schools 3=Safety 4=Taxes"
Response: "2"

Processing:
  → Parse response to option value
  → Resolve to label ("Schools")
  → Aggregate counts
  → Cross-tabulate with demographics

Output:
  selected_option: "2"
  selected_label: "Schools"
  distribution: {Roads: 25%, Schools: 35%, Safety: 20%, Taxes: 20%}
```

### 3. Rating Scale
```
Question: "Rate the mayor's performance 1-5"
Response: "3"

Processing:
  → Parse numeric value
  → Calculate mean, median, distribution
  → Segment by demographics

Output:
  rating: 3
  mean_rating: 3.2
  distribution: {1: 10%, 2: 15%, 3: 30%, 4: 25%, 5: 20%}
```

### 4. Mixed (Most Complex)
```
Question: "Which matters most? 1=Roads 2=Schools 3=Safety. Please explain."
Response: "2 - my kids school is overcrowded and underfunded"

Processing:
  → Split: structured_part="2", explanation="my kids school is overcrowded..."
  → Route structured to aggregator
  → Route explanation to clustering (grouped by selected option)
  → Merge results

Output:
  selected_option: "2"
  selected_label: "Schools"
  explanation_cluster: {
    theme: "School Capacity & Funding",
    sentiment: "negative"
  }

  # Aggregate view: For people who chose "Schools", top themes are:
  # 1. Overcrowding (45%)
  # 2. Funding cuts (30%)
  # 3. Teacher quality (25%)
```

## Implementation: Question Type Router

```python
class QuestionTypeRouter:
    """Routes messages to appropriate processors based on question type"""

    def __init__(self, config: dict):
        self.open_ended_processor = OpenEndedProcessor(config)
        self.structured_processor = StructuredProcessor(config)
        self.mixed_processor = MixedProcessor(config)

    async def route_and_process(
        self,
        messages: list[ConsolidatedMessage],
        poll_metadata: PollMetadata
    ) -> dict[str, QuestionAnalysisResult]:
        """Process all messages, routing by question type"""

        # Group messages by question_id
        by_question = self._group_by_question(messages)

        results = {}
        for question_id, question_messages in by_question.items():
            question = poll_metadata.get_question(question_id)

            if question.question_type == QuestionType.OPEN_ENDED:
                result = await self.open_ended_processor.process(
                    question_messages, question
                )

            elif question.question_type in (QuestionType.MULTIPLE_CHOICE,
                                            QuestionType.RATING_SCALE,
                                            QuestionType.YES_NO):
                result = await self.structured_processor.process(
                    question_messages, question
                )

            elif question.question_type == QuestionType.MIXED:
                result = await self.mixed_processor.process(
                    question_messages, question
                )

            results[question_id] = result

        return results
```

## CSV Input Format Options

### Option A: Question metadata in separate file
```
# poll_metadata.json
{
  "poll_id": "berkeley-oct-2025",
  "questions": [
    {
      "question_id": "Q1",
      "question_type": "multiple_choice",
      "question_text": "What's your top priority?",
      "options": [
        {"value": "1", "label": "Roads"},
        {"value": "2", "label": "Schools"},
        {"value": "3", "label": "Safety"}
      ]
    },
    {
      "question_id": "Q2",
      "question_type": "open_ended",
      "question_text": "What other issues matter to you?"
    }
  ]
}

# responses.csv
phone_number,question_id,message_text
555-1234,Q1,2
555-1234,Q2,Property taxes are killing me
555-5678,Q1,1
555-5678,Q2,Roads in my neighborhood are falling apart
```

### Option B: Question metadata inline in CSV
```
phone_number,question_id,question_type,question_text,options,message_text
555-1234,Q1,multiple_choice,"Top priority?","1=Roads|2=Schools|3=Safety",2
555-1234,Q2,open_ended,"Other issues?",,Property taxes are killing me
```

### Option C: Infer from response patterns (Auto-detect)
```python
class QuestionTypeDetector:
    """Attempt to auto-detect question type from responses"""

    def detect(self, responses: list[str]) -> QuestionType:
        # Check if all responses are single digits/letters
        if all(self._is_single_option(r) for r in responses):
            unique = set(responses)
            if unique <= {'1','2','3','4','5'} or unique <= {'yes','no','y','n'}:
                return QuestionType.MULTIPLE_CHOICE

        # Check if responses look like "2 - explanation" pattern
        mixed_pattern = r'^\d+\s*[-:]\s*.+'
        if sum(1 for r in responses if re.match(mixed_pattern, r)) > len(responses) * 0.5:
            return QuestionType.MIXED

        return QuestionType.OPEN_ENDED
```

## Integration with Existing Pipeline

### Modified V1PipelineOrchestrator

```python
async def run_pipeline(self, campaign_name: str) -> PipelineResult:
    # ... existing consolidation ...

    # NEW: Load poll metadata if available
    poll_metadata = await self._load_poll_metadata(campaign_name)

    # NEW: Enrich messages with question context
    if poll_metadata:
        messages = self._enrich_with_question_context(messages, poll_metadata)

    # NEW: Route by question type
    if poll_metadata and poll_metadata.has_structured_questions:
        router = QuestionTypeRouter(self.config)
        results = await router.route_and_process(messages, poll_metadata)
    else:
        # Fallback to current behavior (all open-ended)
        results = await self._run_clustering_stage(messages, campaign_name)

    # ... rest of pipeline ...
```

## Output Format Changes

### Current Output (Open-ended only)
```json
{
  "poll_id": "berkeley-oct-2025",
  "clusters": [
    {
      "cluster_id": 1,
      "theme": "Infrastructure Concerns",
      "message_count": 45,
      "sentiment": "negative"
    }
  ]
}
```

### New Output (Question-aware)
```json
{
  "poll_id": "berkeley-oct-2025",
  "questions": [
    {
      "question_id": "Q1",
      "question_type": "multiple_choice",
      "question_text": "What's your top priority?",
      "total_responses": 150,
      "distribution": {
        "1": {"label": "Roads", "count": 45, "pct": 30.0},
        "2": {"label": "Schools", "count": 52, "pct": 34.7},
        "3": {"label": "Safety", "count": 53, "pct": 35.3}
      },
      "demographics": {
        "by_age": {
          "18-34": {"1": 12, "2": 25, "3": 18},
          "35-54": {"1": 20, "2": 15, "3": 22},
          "55+": {"1": 13, "2": 12, "3": 13}
        }
      }
    },
    {
      "question_id": "Q2",
      "question_type": "open_ended",
      "question_text": "What other issues matter to you?",
      "total_responses": 120,
      "clusters": [
        {
          "cluster_id": 1,
          "theme": "Property Tax Burden",
          "message_count": 35,
          "sentiment": "negative"
        }
      ]
    }
  ]
}
```

## Migration Path

1. **Phase 1**: Add question_type field to ConsolidatedMessage (default: OPEN_ENDED)
2. **Phase 2**: Implement StructuredProcessor for multiple choice
3. **Phase 3**: Implement MixedProcessor for hybrid responses
4. **Phase 4**: Add auto-detection for backwards compatibility
5. **Phase 5**: Update DynamoDB schema and frontend consumers

## Open Questions

1. **Where does poll metadata come from?** Serve API? Separate upload? Inline in CSV?
2. **How to handle responses that don't match expected options?** (e.g., "2 or 3" when single-select expected)
3. **Should structured responses still get sentiment analysis?** (e.g., is "1" positive or negative?)
4. **Multi-question polls**: One CSV per question, or all questions in single CSV?
