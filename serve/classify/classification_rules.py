#!/usr/bin/env python3

from typing import Dict, List, Optional, Set
import re
from .models import HierarchicalIssueWithContext, IssueStance

class ClassificationRules:
    """
    World-class classification rules based on manual review insights
    These patterns encode the learned behaviors from expert manual categorization
    """

    # Patterns that require multiple related categories
    CLASSIFICATION_PATTERNS = {
        "truck_warehouse_complaints": {
            "description": "Complaints about trucks, warehouses, industrial development",
            "triggers": [
                r"truck\s+traffic", r"semi\s+truck", r"warehouse", r"intermodal",
                r"cn\s+rail", r"rail\s+yard", r"data\s+center", r"industrial"
            ],
            "required_categories": [
                {
                    "primary": "infrastructure_and_transportation",
                    "secondary": "roads_and_bridges",
                    "concern_template": "truck traffic impact on roads",
                    "stance": "negative"
                },
                {
                    "primary": "housing_and_development",
                    "secondary": "zoning_and_permits",
                    "concern_template": "zoning approvals enabling development",
                    "stance": "negative",
                    "is_root_cause": True
                }
            ],
            "optional_categories": [
                {
                    "primary": "economic_development",
                    "secondary": "industrial_development",
                    "concern_template": "industrial development impact"
                }
            ]
        },

        "property_tax_specific": {
            "description": "Property tax complaints should be categorized specifically",
            "triggers": [
                r"property\s+tax", r"home\s+tax", r"house\s+tax", r"property\s+taxes"
            ],
            "required_categories": [
                {
                    "primary": "housing_and_development",
                    "secondary": "taxes_and_assessments",
                    "concern_template": "property tax burden",
                    "stance": "negative"
                }
            ],
            # Don't categorize as general government budget/taxes
            "avoid_categories": [
                "government_operations/budget_and_taxes"
            ]
        },

        "ebike_scooter_default": {
            "description": "E-bikes/scooters are infrastructure issues unless enforcement mentioned",
            "triggers": [
                r"e-?bike", r"electric\s+bike", r"e-?scooter", r"electric\s+scooter"
            ],
            "required_categories": [
                {
                    "primary": "infrastructure_and_transportation",
                    "secondary": "transit_and_walkways",
                    "concern_template": "e-bike/scooter infrastructure needs",
                    "stance": "neutral"
                }
            ],
            "only_add_police_if": [
                r"enforcement", r"fine", r"ticket", r"police", r"ban", r"ordinance"
            ]
        },

        "water_bill_utilities": {
            "description": "Water bill complaints should be utilities, not general taxes",
            "triggers": [
                r"water\s+bill", r"water\s+cost", r"water\s+price"
            ],
            "required_categories": [
                {
                    "primary": "quality_of_life",
                    "secondary": "utilities_and_waste",
                    "concern_template": "water utility costs",
                    "stance": "negative"
                }
            ]
        }
    }

    # Patterns that indicate message should be uncategorized
    UNCATEGORIZED_PATTERNS = {
        "federal_politics": {
            "description": "Federal/national political issues not relevant to local government",
            "patterns": [
                r"trump", r"biden", r"federal", r"president", r"democrat", r"republican",
                r"national\s+election", r"congress", r"senate"
            ],
            "reason": "federal/national political issue"
        },

        "wrong_number": {
            "description": "Messages from people who don't live in the area or wrong numbers",
            "patterns": [
                r"wrong\s+number", r"don\'t\s+live", r"moved\s+out", r"not\s+eligible",
                r"different\s+state", r"connecticut", r"florida"
            ],
            "reason": "wrong number/location"
        },

        "personal_attacks": {
            "description": "Pure personal attacks without policy content",
            "patterns": [
                r"fuck\s+off", r"fuck\s+you", r"go\s+to\s+hell", r"🖕"
            ],
            "reason": "personal attack without substantive content"
        },

        "simple_acknowledgments": {
            "description": "Simple thanks/greetings without issues",
            "patterns": [
                r"^(thanks?|thank\s+you)[\s\w]*$",
                r"^(great|good)\s*(thanks?)?[\s\w]*$",
                r"^(hi|hello|hey)[\s\w]*$"
            ],
            "reason": "simple acknowledgment without issues"
        },

        "general_support": {
            "description": "General support messages without specific issues",
            "patterns": [
                r"happy\s+that\s+you\s+were\s+elected",
                r"glad\s+that\s+you\s+reached\s+out",
                r"i\'ll\s+be\s+watching"
            ],
            "reason": "general support without specific issues"
        }
    }

    # Context-specific refinement rules
    REFINEMENT_RULES = {
        "school_construction_vs_school_issues": {
            "description": "Construction near schools isn't necessarily a school issue",
            "pattern": r"construction.*school",
            "rule": "Only categorize as education if discussing school operations, not nearby construction"
        },

        "zoning_for_business_permits": {
            "description": "Business permit issues should include zoning when relevant",
            "pattern": r"business.*permit|permit.*business",
            "add_category": {
                "primary": "housing_and_development",
                "secondary": "zoning_and_permits",
                "is_root_cause": True
            }
        }
    }

    @classmethod
    def should_be_uncategorized(cls, message_text: str) -> tuple[bool, Optional[str]]:
        """
        Check if message should be uncategorized based on learned patterns
        Returns (should_uncategorize, reason)
        """
        text_lower = message_text.lower().strip()

        for category_name, pattern_info in cls.UNCATEGORIZED_PATTERNS.items():
            for pattern in pattern_info["patterns"]:
                if re.search(pattern, text_lower, re.IGNORECASE):
                    return True, pattern_info["reason"]

        return False, None

    @classmethod
    def get_required_categories(cls, message_text: str) -> List[Dict]:
        """
        Get required categories based on message patterns
        """
        text_lower = message_text.lower()
        required_categories = []

        for pattern_name, pattern_info in cls.CLASSIFICATION_PATTERNS.items():
            # Check if any trigger matches
            for trigger in pattern_info["triggers"]:
                if re.search(trigger, text_lower, re.IGNORECASE):
                    required_categories.extend(pattern_info["required_categories"])
                    break

        return required_categories

    @classmethod
    def get_categories_to_avoid(cls, message_text: str) -> List[str]:
        """
        Get categories that should be avoided based on patterns
        """
        text_lower = message_text.lower()
        avoid_categories = []

        for pattern_name, pattern_info in cls.CLASSIFICATION_PATTERNS.items():
            # Check if any trigger matches
            for trigger in pattern_info["triggers"]:
                if re.search(trigger, text_lower, re.IGNORECASE):
                    if "avoid_categories" in pattern_info:
                        avoid_categories.extend(pattern_info["avoid_categories"])
                    break

        return avoid_categories

    @classmethod
    def should_add_police_category(cls, message_text: str, category: str) -> bool:
        """
        Special rule for e-bike/scooter: only add police if enforcement mentioned
        """
        if "transit_and_walkways" not in category:
            return True  # Not relevant to this rule

        text_lower = message_text.lower()

        # Check if e-bike/scooter mentioned
        ebike_mentioned = any(
            re.search(trigger, text_lower, re.IGNORECASE)
            for trigger in cls.CLASSIFICATION_PATTERNS["ebike_scooter_default"]["triggers"]
        )

        if not ebike_mentioned:
            return True  # Rule doesn't apply

        # Check if enforcement keywords mentioned
        enforcement_mentioned = any(
            re.search(keyword, text_lower, re.IGNORECASE)
            for keyword in cls.CLASSIFICATION_PATTERNS["ebike_scooter_default"]["only_add_police_if"]
        )

        return enforcement_mentioned

    @classmethod
    def apply_refinement_rules(cls, issues: List[HierarchicalIssueWithContext], message_text: str) -> List[HierarchicalIssueWithContext]:
        """
        Apply context-specific refinement rules
        """
        text_lower = message_text.lower()

        # Check zoning rule for business permits
        if re.search(cls.REFINEMENT_RULES["zoning_for_business_permits"]["pattern"], text_lower, re.IGNORECASE):
            # Check if business/economic development mentioned but zoning not included
            has_business = any(
                "economic_development" in issue.primary_category or "business" in issue.secondary_category
                for issue in issues
            )
            has_zoning = any(
                "zoning_and_permits" in issue.secondary_category
                for issue in issues
            )

            if has_business and not has_zoning:
                rule_data = cls.REFINEMENT_RULES["zoning_for_business_permits"]["add_category"]
                issues.append(
                    HierarchicalIssueWithContext(
                        primary_category=rule_data["primary"],
                        secondary_category=rule_data["secondary"],
                        stance=IssueStance.NEGATIVE,
                        specific_concern="zoning/permits enabling business issues",
                        is_root_cause=rule_data.get("is_root_cause", False)
                    )
                )

        return issues

    @classmethod
    def validate_classification_quality(cls, message_text: str, issues: List[HierarchicalIssueWithContext]) -> List[str]:
        """
        Validate classification quality and return list of potential issues
        """
        validation_issues = []
        text_lower = message_text.lower()

        # Check property tax misclassification
        if re.search(r"property\s+tax", text_lower, re.IGNORECASE):
            has_property_tax = any(
                "taxes_and_assessments" in issue.secondary_category
                for issue in issues
            )
            has_general_tax = any(
                "budget_and_taxes" in issue.secondary_category
                for issue in issues
            )

            if has_general_tax and not has_property_tax:
                validation_issues.append("Property tax misclassified as general budget/taxes")

        # Check e-bike/scooter police misclassification
        ebike_mentioned = any(
            re.search(trigger, text_lower, re.IGNORECASE)
            for trigger in cls.CLASSIFICATION_PATTERNS["ebike_scooter_default"]["triggers"]
        )

        if ebike_mentioned:
            has_police = any("police" in issue.secondary_category for issue in issues)
            has_enforcement_keywords = any(
                re.search(keyword, text_lower, re.IGNORECASE)
                for keyword in cls.CLASSIFICATION_PATTERNS["ebike_scooter_default"]["only_add_police_if"]
            )

            if has_police and not has_enforcement_keywords:
                validation_issues.append("E-bike/scooter incorrectly categorized as police issue without enforcement context")

        return validation_issues