#!/usr/bin/env python3

from typing import List, Dict, Any, Optional, Tuple
from dataclasses import dataclass
from shared.logger import get_logger

from .models import EnrichedMessage, SmartCategorization, HierarchicalIssueWithContext, MessageData
from .classification_rules import ClassificationRules

logger = get_logger(__name__)


@dataclass
class ValidationIssue:
    """Represents a validation issue found in classification"""
    severity: str  # "error", "warning", "suggestion"
    issue_type: str
    description: str
    suggested_fix: Optional[str] = None


@dataclass
class ValidationResult:
    """Result of validation for a single message"""
    is_valid: bool
    issues: List[ValidationIssue]
    confidence_adjustment: Optional[float] = None


class ClassificationValidator:
    """
    Validates classification quality based on learned patterns from manual review
    """

    def __init__(self):
        self.validation_rules = self._initialize_validation_rules()

    def _initialize_validation_rules(self) -> Dict[str, Any]:
        """Initialize validation rules based on manual review insights"""
        return {
            "property_tax_specificity": {
                "description": "Property tax complaints should be specific, not general",
                "severity": "error"
            },
            "ebike_police_context": {
                "description": "E-bike/scooter issues should only be police if enforcement mentioned",
                "severity": "warning"
            },
            "truck_zoning_connection": {
                "description": "Truck complaints should include zoning as root cause",
                "severity": "suggestion"
            },
            "uncategorized_appropriateness": {
                "description": "Messages should be appropriately uncategorized",
                "severity": "error"
            },
            "root_cause_identification": {
                "description": "Root causes should be properly identified",
                "severity": "suggestion"
            }
        }

    def validate_message_classification(self, message: MessageData, classification: SmartCategorization) -> ValidationResult:
        """
        Validate a single message classification
        """
        issues = []

        # Use existing ClassificationRules validation
        rule_issues = ClassificationRules.validate_classification_quality(
            message.message_text, classification.issues
        )

        for rule_issue in rule_issues:
            issues.append(ValidationIssue(
                severity="error",
                issue_type="classification_rule_violation",
                description=rule_issue,
                suggested_fix=self._get_suggested_fix_for_rule_issue(rule_issue)
            ))

        # Additional validation checks
        issues.extend(self._validate_uncategorized_logic(message, classification))
        issues.extend(self._validate_issue_completeness(message, classification))
        issues.extend(self._validate_root_cause_logic(message, classification))
        issues.extend(self._validate_stance_consistency(classification))

        # Calculate overall validity
        error_count = sum(1 for issue in issues if issue.severity == "error")
        is_valid = error_count == 0

        # Suggest confidence adjustment based on issues
        confidence_adjustment = None
        if error_count > 0:
            confidence_adjustment = max(0.1, classification.confidence_score - (error_count * 0.2))
        elif any(issue.severity == "warning" for issue in issues):
            confidence_adjustment = max(0.3, classification.confidence_score - 0.1)

        return ValidationResult(
            is_valid=is_valid,
            issues=issues,
            confidence_adjustment=confidence_adjustment
        )

    def _validate_uncategorized_logic(self, message: MessageData, classification: SmartCategorization) -> List[ValidationIssue]:
        """Validate the logic for uncategorized messages"""
        issues = []

        # Check if message should be uncategorized but isn't
        should_uncategorize, reason = ClassificationRules.should_be_uncategorized(message.message_text)

        if should_uncategorize and not classification.should_be_uncategorized:
            issues.append(ValidationIssue(
                severity="error",
                issue_type="missed_uncategorized",
                description=f"Message should be uncategorized: {reason}",
                suggested_fix="Mark as uncategorized with appropriate reason"
            ))

        # Check if message is uncategorized but has substantial issues
        if classification.should_be_uncategorized and classification.issues:
            issues.append(ValidationIssue(
                severity="warning",
                issue_type="uncategorized_with_issues",
                description="Message marked uncategorized but has identified issues",
                suggested_fix="Review if uncategorization is appropriate"
            ))

        return issues

    def _validate_issue_completeness(self, message: MessageData, classification: SmartCategorization) -> List[ValidationIssue]:
        """Validate that all expected issues are captured"""
        issues = []

        # Check for missing required categories
        required_categories = ClassificationRules.get_required_categories(message.message_text)
        existing_categories = set(
            f"{issue.primary_category}/{issue.secondary_category}"
            for issue in classification.issues
        )

        for req_cat in required_categories:
            cat_key = f"{req_cat['primary']}/{req_cat['secondary']}"
            if cat_key not in existing_categories:
                issues.append(ValidationIssue(
                    severity="error",
                    issue_type="missing_required_category",
                    description=f"Missing required category: {cat_key}",
                    suggested_fix=f"Add category: {cat_key}"
                ))

        # Check for categories that should be avoided
        categories_to_avoid = ClassificationRules.get_categories_to_avoid(message.message_text)
        for issue in classification.issues:
            cat_key = f"{issue.primary_category}/{issue.secondary_category}"
            if cat_key in categories_to_avoid:
                issues.append(ValidationIssue(
                    severity="warning",
                    issue_type="avoided_category_used",
                    description=f"Used category that should be avoided: {cat_key}",
                    suggested_fix=f"Consider removing or replacing: {cat_key}"
                ))

        return issues

    def _validate_root_cause_logic(self, message: MessageData, classification: SmartCategorization) -> List[ValidationIssue]:
        """Validate root cause identification"""
        issues = []

        # Check for truck/warehouse complaints without zoning root cause
        text_lower = message.message_text.lower()
        has_truck_complaint = any(
            keyword in text_lower
            for keyword in ["truck", "warehouse", "intermodal", "cn rail", "data center"]
        )

        if has_truck_complaint:
            has_zoning_root_cause = any(
                issue.is_root_cause and "zoning" in issue.secondary_category.lower()
                for issue in classification.issues
            )

            if not has_zoning_root_cause:
                issues.append(ValidationIssue(
                    severity="suggestion",
                    issue_type="missing_root_cause",
                    description="Truck/warehouse complaint should include zoning as root cause",
                    suggested_fix="Add zoning/permits category as root cause"
                ))

        return issues

    def _validate_stance_consistency(self, classification: SmartCategorization) -> List[ValidationIssue]:
        """Validate that issue stances are consistent with concerns"""
        issues = []

        for issue in classification.issues:
            concern_lower = issue.specific_concern.lower()

            # Check for stance mismatch
            if issue.stance.value == "positive":
                if any(negative_word in concern_lower for negative_word in ["too high", "bad", "terrible", "awful", "problem"]):
                    issues.append(ValidationIssue(
                        severity="warning",
                        issue_type="stance_concern_mismatch",
                        description=f"Positive stance but negative concern: {issue.specific_concern}",
                        suggested_fix="Review stance assignment for consistency"
                    ))
            elif issue.stance.value == "negative":
                if any(positive_word in concern_lower for positive_word in ["great", "good", "love", "excellent", "wonderful"]):
                    issues.append(ValidationIssue(
                        severity="warning",
                        issue_type="stance_concern_mismatch",
                        description=f"Negative stance but positive concern: {issue.specific_concern}",
                        suggested_fix="Review stance assignment for consistency"
                    ))

        return issues

    def _get_suggested_fix_for_rule_issue(self, rule_issue: str) -> str:
        """Get suggested fix for classification rule issues"""
        if "Property tax misclassified" in rule_issue:
            return "Change from government_operations/budget_and_taxes to housing_and_development/taxes_and_assessments"
        elif "E-bike/scooter incorrectly" in rule_issue:
            return "Change from public_safety/police to infrastructure_and_transportation/transit_and_walkways"
        else:
            return "Review classification against learned rules"

    def validate_batch(self, messages: List[MessageData], classifications: List[SmartCategorization]) -> Dict[str, Any]:
        """
        Validate a batch of classifications and return summary statistics
        """
        if len(messages) != len(classifications):
            raise ValueError("Number of messages must match number of classifications")

        logger.info(f"Validating batch of {len(classifications)} classifications")

        validation_results = []
        for message, classification in zip(messages, classifications):
            result = self.validate_message_classification(message, classification)
            validation_results.append(result)

        # Calculate summary statistics
        total_messages = len(validation_results)
        valid_messages = sum(1 for result in validation_results if result.is_valid)

        issue_counts = {}
        severity_counts = {"error": 0, "warning": 0, "suggestion": 0}

        for result in validation_results:
            for issue in result.issues:
                issue_counts[issue.issue_type] = issue_counts.get(issue.issue_type, 0) + 1
                severity_counts[issue.severity] += 1

        confidence_adjustments = [
            result.confidence_adjustment for result in validation_results
            if result.confidence_adjustment is not None
        ]

        summary = {
            "total_messages": total_messages,
            "valid_messages": valid_messages,
            "validation_pass_rate": valid_messages / total_messages if total_messages > 0 else 0,
            "error_count": severity_counts["error"],
            "warning_count": severity_counts["warning"],
            "suggestion_count": severity_counts["suggestion"],
            "most_common_issues": sorted(issue_counts.items(), key=lambda x: x[1], reverse=True)[:5],
            "confidence_adjustments_applied": len(confidence_adjustments),
            "average_confidence_adjustment": sum(confidence_adjustments) / len(confidence_adjustments) if confidence_adjustments else None
        }

        logger.info(f"Validation complete: {valid_messages}/{total_messages} messages valid ({summary['validation_pass_rate']:.1%})")
        if summary['error_count'] > 0:
            logger.warning(f"Found {summary['error_count']} classification errors")

        return {
            "summary": summary,
            "results": validation_results
        }

    def generate_validation_report(self, validation_data: Dict[str, Any]) -> str:
        """Generate a human-readable validation report"""
        summary = validation_data["summary"]
        results = validation_data["results"]

        report_lines = [
            "# Classification Validation Report",
            "",
            f"**Total Messages:** {summary['total_messages']}",
            f"**Valid Classifications:** {summary['valid_messages']} ({summary['validation_pass_rate']:.1%})",
            f"**Errors:** {summary['error_count']}",
            f"**Warnings:** {summary['warning_count']}",
            f"**Suggestions:** {summary['suggestion_count']}",
            "",
            "## Most Common Issues"
        ]

        for issue_type, count in summary['most_common_issues']:
            report_lines.append(f"- **{issue_type}:** {count} occurrences")

        if summary['confidence_adjustments_applied'] > 0:
            report_lines.extend([
                "",
                "## Confidence Adjustments",
                f"**Applied to:** {summary['confidence_adjustments_applied']} messages",
                f"**Average adjustment:** {summary['average_confidence_adjustment']:.2f}"
            ])

        # Add sample issues for debugging
        error_examples = []
        for result in results:
            for issue in result.issues:
                if issue.severity == "error" and len(error_examples) < 3:
                    error_examples.append(f"- {issue.description}")

        if error_examples:
            report_lines.extend([
                "",
                "## Sample Errors",
                *error_examples
            ])

        return "\n".join(report_lines)


def main():
    """Test the validator"""
    from .models import MessageData, SmartCategorization, HierarchicalIssueWithContext, IssueStance

    validator = ClassificationValidator()

    # Test message with property tax issue
    message = MessageData(
        campaign_id="test",
        campaign_name="Test",
        contact_phone_number="1234567890",
        carrier="TEST",
        campaign_number="123",
        is_automatic_reply=False,
        send_direction="INBOUND",
        send_status="",
        error_code="",
        sent_at="2025-01-01T00:00:00.000Z",
        message_text="Property taxes are too high!",
        texter_name="",
        message_type="SMS",
        mms_attachments=""
    )

    # Incorrect classification (should trigger validation error)
    classification = SmartCategorization(
        issues=[
            HierarchicalIssueWithContext(
                primary_category="government_operations",
                secondary_category="budget_and_taxes",
                stance=IssueStance.NEGATIVE,
                specific_concern="taxes too high",
                is_root_cause=False
            )
        ],
        should_be_uncategorized=False,
        overall_sentiment="frustrated_urgent",
        message_quality="substantive",
        content_type="general_complaint",
        confidence_score=0.8
    )

    result = validator.validate_message_classification(message, classification)

    print(f"Valid: {result.is_valid}")
    print(f"Issues found: {len(result.issues)}")
    for issue in result.issues:
        print(f"  {issue.severity}: {issue.description}")
        if issue.suggested_fix:
            print(f"    Fix: {issue.suggested_fix}")


if __name__ == "__main__":
    main()