#!/usr/bin/env python3

"""
World-Class Civic Message Classification Pipeline

This package provides comprehensive classification of civic engagement messages
using advanced LLM techniques and learned patterns from manual review.
"""

from .models import (
    MessageData,
    EnrichedMessage,
    SmartCategorization,
    HierarchicalIssueWithContext,
    IssueStance,
    Sentiment,
    MessageQuality,
    ContentType
)

from .data_loader import DataLoader
from .data_cleaner import SmartDataCleaner
from .smart_classifier import WorldClassClassifier
from .batch_processor import BatchProcessor, BatchProcessingConfig
from .smart_aggregator import SmartAggregator
from .validator import ClassificationValidator
from .classification_rules import ClassificationRules

__version__ = "1.0.0"
__author__ = "Claude Code Assistant"
__description__ = "World-Class Civic Message Classification Pipeline"