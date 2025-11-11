#!/usr/bin/env python3

from dataclasses import asdict, dataclass


@dataclass
class PollIssueAnalysisData:
    pollId: str
    rank: int
    theme: str
    summary: str
    analysis: str
    quotes: list[dict[str, str]]
    responseCount: int

@dataclass
class PollAnalysisCompleteData:
    pollId: str
    totalResponses: int
    issues: list[PollIssueAnalysisData]

@dataclass
class PollAnalysisCompleteEvent:
    data: PollAnalysisCompleteData
    type: str = 'pollAnalysisComplete'

    def to_json(self) -> dict:
        return {
            'type': self.type,
            'data': asdict(self.data)
        }
