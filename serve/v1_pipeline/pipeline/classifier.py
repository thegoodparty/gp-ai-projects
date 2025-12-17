#!/usr/bin/env python3

import asyncio
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pandas as pd

project_root = Path(__file__).resolve().parent.parent.parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from shared.llm_gemini import GeminiClient, GeminiModelType
from shared.logger import get_logger

logger = get_logger(__name__)


@dataclass
class ClassificationResult:
    option: str
    reason: str | None
    confidence: float


@dataclass
class Quote:
    quote: str
    phone_number: str

    def to_dict(self) -> dict:
        return {
            "quote": self.quote,
            "phone_number": self.phone_number
        }


@dataclass
class IssueResult:
    poll_id: str
    rank: int
    theme: str
    summary: str
    analysis: str
    quotes: list[Quote]
    response_count: int

    def to_dict(self) -> dict:
        return {
            "pollId": self.poll_id,
            "rank": self.rank,
            "theme": self.theme,
            "summary": self.summary,
            "analysis": self.analysis,
            "quotes": [q.to_dict() for q in self.quotes],
            "responseCount": self.response_count
        }


@dataclass
class PipelineResult:
    poll_id: str
    question_text: str
    options: list[str]
    total_responses: int
    issues: list[IssueResult]
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return [
            {
                "type": "pollAnalysisComplete",
                "data": {
                    "pollId": self.poll_id,
                    "totalResponses": self.total_responses,
                    "issues": [issue.to_dict() for issue in self.issues]
                }
            }
        ]


class ResponseClassifier:
    def __init__(self, options: list[str], question_text: str):
        self.options = options
        self.question_text = question_text
        self.llm = GeminiClient(
            default_model=GeminiModelType.FLASH,
            default_temperature=0.0,
            thinking_budget=0,
            max_connections=100,
            max_keepalive_connections=25
        )
        self.thread_pool = ThreadPoolExecutor(max_workers=100)

    async def classify(self, response_text: str) -> ClassificationResult:
        if not self.options:
            return ClassificationResult(option="Other", reason=response_text, confidence=1.0)

        opt_out_keywords = ["stop", "unsubscribe", "quit", "cancel", "remove", "optout", "opt out"]
        if response_text.lower().strip() in opt_out_keywords:
            return ClassificationResult(option="_filtered", reason="opt-out", confidence=1.0)

        options_str = ", ".join(f'"{opt}"' for opt in self.options)
        prompt = f"""You are classifying poll responses. Given a question and response, classify the response into one of the predefined options or "Other".

Question: {self.question_text}
Valid options: {options_str}

Response to classify: "{response_text}"

Instructions:
1. If the response clearly matches one of the options (even with typos, slang, or casual language), return that option
2. If the response is unclear, off-topic, or doesn't fit any option, return "Other"
3. Extract any reason or explanation given (text beyond the yes/no)

Return ONLY valid JSON in this exact format:
{{"option": "Yes|No|Other", "reason": "extracted reason or null", "confidence": 0.0-1.0}}"""

        try:
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                self.thread_pool,
                lambda: self.llm.generate_content(prompt=prompt)
            )

            if not response:
                return ClassificationResult(option="Other", reason=response_text, confidence=0.5)

            response_text_clean = response.strip()
            if response_text_clean.startswith("```json"):
                response_text_clean = response_text_clean[7:]
            if response_text_clean.startswith("```"):
                response_text_clean = response_text_clean[3:]
            if response_text_clean.endswith("```"):
                response_text_clean = response_text_clean[:-3]

            result = json.loads(response_text_clean.strip())

            option = result.get("option", "Other")
            if option not in self.options and option != "Other":
                option = "Other"

            return ClassificationResult(
                option=option,
                reason=result.get("reason"),
                confidence=result.get("confidence", 0.8)
            )

        except Exception as e:
            logger.warning(f"Classification failed for '{response_text[:50]}...': {e}")
            return ClassificationResult(option="Other", reason=response_text, confidence=0.3)

    async def classify_batch(self, responses: list[str]) -> list[ClassificationResult]:
        tasks = [self.classify(r) for r in responses]
        return await asyncio.gather(*tasks, return_exceptions=False)


@dataclass
class SummaryAndAnalysis:
    summary: str
    analysis: str


class BucketSummarizer:
    def __init__(self, question_text: str):
        self.question_text = question_text
        self.llm = GeminiClient(
            default_model=GeminiModelType.FLASH,
            default_temperature=0.3,
            thinking_budget=0
        )
        self.thread_pool = ThreadPoolExecutor(max_workers=10)

    async def summarize_and_analyze(self, option: str, reasons: list[str], quotes: list[Quote]) -> SummaryAndAnalysis:
        if not reasons and not quotes:
            return SummaryAndAnalysis(
                summary=f"No specific reasons provided for '{option}'",
                analysis=f"Respondents selected '{option}' without providing additional explanation or context."
            )

        reasons_sample = reasons[:50]
        reasons_str = "\n".join(f"- {r}" for r in reasons_sample if r)

        quotes_sample = quotes[:20]
        quotes_str = "\n".join(f'- "{q.quote}"' for q in quotes_sample if q.quote)

        content_str = reasons_str if reasons_str.strip() else quotes_str

        if not content_str.strip():
            return SummaryAndAnalysis(
                summary=f"Responses for '{option}' without additional explanation",
                analysis=f"Respondents selected '{option}' but did not provide detailed reasoning."
            )

        prompt = f"""Analyze responses from people who answered "{option}" to this poll question.

Question: {self.question_text}

Here are their responses/explanations:
{content_str}

Provide TWO outputs:

1. SUMMARY: A concise 1-2 sentence summary of the common themes. Focus on the "why" behind their answers.

2. ANALYSIS: A detailed 2-3 paragraph analysis that:
   - Identifies the main themes and concerns expressed
   - Notes any specific examples, locations, or issues mentioned
   - Describes the overall sentiment and urgency
   - Highlights any notable patterns or insights

Return as JSON:
{{"summary": "1-2 sentence summary", "analysis": "2-3 paragraph detailed analysis"}}"""

        try:
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                self.thread_pool,
                lambda: self.llm.generate_content(prompt=prompt)
            )

            if response:
                response_clean = response.strip()
                if response_clean.startswith("```json"):
                    response_clean = response_clean[7:]
                if response_clean.startswith("```"):
                    response_clean = response_clean[3:]
                if response_clean.endswith("```"):
                    response_clean = response_clean[:-3]

                result = json.loads(response_clean.strip())
                return SummaryAndAnalysis(
                    summary=result.get("summary", f"Responses for '{option}'"),
                    analysis=result.get("analysis", f"Analysis of '{option}' responses")
                )

            return SummaryAndAnalysis(
                summary=f"Responses for '{option}' with varied explanations",
                analysis=f"Respondents provided varied explanations for selecting '{option}'."
            )
        except Exception as e:
            logger.warning(f"Summarization failed for '{option}': {e}")
            return SummaryAndAnalysis(
                summary=f"Responses for '{option}' with varied explanations",
                analysis=f"Respondents provided varied explanations for selecting '{option}'."
            )


class ClassificationPipeline:
    def __init__(
        self,
        poll_id: str,
        campaign_id: str,
        question_text: str,
        options: list[str],
        callback_success_url: str | None = None,
        callback_failure_url: str | None = None,
        input_dir: str | None = None,
        output_dir: str | None = None
    ):
        self.poll_id = poll_id
        self.campaign_id = campaign_id
        self.question_text = question_text
        self.options = options
        self.callback_success_url = callback_success_url
        self.callback_failure_url = callback_failure_url

        self.input_dir = input_dir or os.environ.get(
            "V2_INPUT_DIR",
            str(Path(__file__).parent.parent.parent / "input")
        )

        self.output_dir = output_dir or os.environ.get(
            "V2_OUTPUT_DIR",
            str(Path(__file__).parent.parent / "output")
        )

        self.classifier = ResponseClassifier(options, question_text)
        self.summarizer = BucketSummarizer(question_text)

        self.errors: list[str] = []

    def _save_events_locally(self, result: "PipelineResult") -> str:
        from serve.v1_pipeline.pipeline.event_saver import save_events
        return save_events(result.to_dict(), self.output_dir)

    def _load_responses(self) -> list[dict]:
        input_path = Path(self.input_dir)

        csv_files = []
        poll_dir = input_path / self.poll_id
        if poll_dir.exists() and poll_dir.is_dir():
            csv_files = list(poll_dir.glob("*.csv"))
        else:
            all_csv = list(input_path.glob("*.csv"))
            csv_files = [f for f in all_csv if self.poll_id.lower() in f.stem.lower()]

        if not csv_files:
            single_file = input_path / f"{self.poll_id}.csv"
            if single_file.exists():
                csv_files = [single_file]

        if not csv_files:
            logger.warning(f"No CSV files found for poll_id '{self.poll_id}' in {input_path}")
            return []

        logger.info(f"Loading {len(csv_files)} CSV file(s) for poll '{self.poll_id}'")

        responses = []
        for csv_file in csv_files:
            try:
                df = pd.read_csv(csv_file)
                df.columns = df.columns.str.lower().str.replace(' ', '_')

                text_col = None
                for col in ['message_text', 'message', 'response', 'text', 'answer']:
                    if col in df.columns:
                        text_col = col
                        break

                if not text_col:
                    logger.warning(f"No message text column found in {csv_file.name}")
                    continue

                for _, row in df.iterrows():
                    msg_text = str(row.get(text_col, ''))
                    if not msg_text or msg_text.lower() == 'nan':
                        continue

                    responses.append({
                        'message_text': msg_text,
                        'phone_number': str(row.get('phone_number', row.get('contact_phone_number', ''))),
                        'sent_at': row.get('sent_at', row.get('sent_at', None))
                    })

                logger.info(f"Loaded {len(responses)} responses from {csv_file.name}")

            except Exception as e:
                logger.error(f"Failed to load {csv_file.name}: {e}")
                self.errors.append(f"Failed to load {csv_file.name}: {e}")

        return responses

    async def run(self) -> PipelineResult:
        start_time = time.time()
        logger.info(f"Starting Classification Pipeline for poll: {self.poll_id}")

        try:
            responses = self._load_responses()
            logger.info(f"Loaded {len(responses)} responses to classify")

            if not responses:
                result = PipelineResult(
                    poll_id=self.poll_id,
                    question_text=self.question_text,
                    options=self.options,
                    total_responses=0,
                    issues=[],
                    errors=self.errors
                )
                await self._call_callback(result, success=True)
                return result

            classified = {opt: {"count": 0, "reasons": [], "quotes": []} for opt in self.options + ["Other"]}

            message_texts = [r['message_text'] for r in responses]

            logger.info(f"Classifying {len(message_texts)} responses...")
            classification_results = await self.classifier.classify_batch(message_texts)

            for response, classification in zip(responses, classification_results):
                if classification.option == "_filtered":
                    continue

                option = classification.option if classification.option in self.options else "Other"

                if option not in classified:
                    classified[option] = {"count": 0, "reasons": [], "quotes": []}

                classified[option]["count"] += 1

                quote = Quote(
                    quote=response['message_text'],
                    phone_number=response.get('phone_number', '').replace('+1', '').replace('+', '')
                )
                classified[option]["quotes"].append(quote)

                if classification.reason:
                    classified[option]["reasons"].append(classification.reason)

            total = sum(data["count"] for data in classified.values())
            logger.info(f"Classification complete. Total valid responses: {total}")

            sorted_options = sorted(
                [(opt, data) for opt, data in classified.items() if data["count"] > 0],
                key=lambda x: x[1]["count"],
                reverse=True
            )

            issues = []
            for rank, (option, data) in enumerate(sorted_options, start=1):
                logger.info(f"Summarizing {data['count']} responses for '{option}'...")
                summary_analysis = await self.summarizer.summarize_and_analyze(
                    option, data["reasons"], data["quotes"]
                )

                top_quotes = data["quotes"][:5]

                issues.append(IssueResult(
                    poll_id=self.poll_id,
                    rank=rank,
                    theme=option,
                    summary=summary_analysis.summary,
                    analysis=summary_analysis.analysis,
                    quotes=top_quotes,
                    response_count=data["count"]
                ))

            processing_time = time.time() - start_time
            logger.info(f"Classification Pipeline completed in {processing_time:.2f}s")

            result = PipelineResult(
                poll_id=self.poll_id,
                question_text=self.question_text,
                options=self.options,
                total_responses=total,
                issues=issues,
                errors=self.errors
            )

            output_file = self._save_events_locally(result)
            logger.info(f"📁 Output saved to: {output_file}")

            await self._call_callback(result, success=True)

            return result

        except Exception as e:
            logger.error(f"Classification Pipeline failed: {e}", exc_info=True)
            self.errors.append(str(e))

            result = PipelineResult(
                poll_id=self.poll_id,
                question_text=self.question_text,
                options=self.options,
                total_responses=0,
                issues=[],
                errors=self.errors
            )

            await self._call_callback(result, success=False)
            raise

    async def _call_callback(self, result: PipelineResult, success: bool):
        url = self.callback_success_url if success else self.callback_failure_url
        if not url:
            return

        try:
            payload = result.to_dict()
            if not success:
                payload[0]["data"]["errors"] = result.errors

            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(url, json=payload)
                response.raise_for_status()
                logger.info(f"Callback sent to {url}: status={response.status_code}")

        except Exception as e:
            logger.error(f"Failed to call callback {url}: {e}")
            self.errors.append(f"Callback failed: {e}")
