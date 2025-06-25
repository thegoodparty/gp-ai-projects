from datetime import date
from typing import Optional
from enum import Enum
from pydantic import BaseModel, Field


class RaceType(str, Enum):
    """Race type enumeration."""
    PARTISAN = "Partisan"
    NONPARTISAN = "Nonpartisan"


class IncumbentStatus(str, Enum):
    """Incumbent status enumeration."""
    ELECTED = "Elected"
    APPOINTED = "Appointed"
    NOT_APPLICABLE = "N/A"


class CampaignInfo(BaseModel):
    """Campaign information schema."""
    
    candidate_name: str = Field(..., description="Candidate's full name (First Last)")
    primary_date: Optional[date] = Field(None, description="Primary election date if necessary")
    election_date: date = Field(..., description="General election date")
    office_and_jurisdiction: str = Field(..., description="Office and jurisdiction (e.g., School Board, At-Large, Chicopee, MA)")
    incumbent_status: Optional[IncumbentStatus] = Field(None, description="If incumbent, were they elected or appointed")
    race_type: RaceType = Field(..., description="Whether the race is partisan or nonpartisan")
    seats_available: int = Field(..., ge=1, description="Number of seats available in the race")
    number_of_opponents: int = Field(..., ge=0, description="Number of opponents in the race")
    win_number: int = Field(..., ge=1, description="Number of votes needed to win")
    total_likely_voters: int = Field(..., ge=0, description="Total number of likely voters")
    available_cell_phones: int = Field(..., ge=0, description="Number of available cell phone contacts")
    available_landlines: int = Field(..., ge=0, description="Number of available landline contacts")
    additional_race_context: Optional[str] = Field(None, description="Additional context about race themes, district dynamics, turnout trends, etc.")
    
    class Config:
        """Pydantic model configuration."""
        use_enum_values = True
        json_encoders = {
            date: lambda v: v.strftime("%m/%d/%Y") if v else None
        }
        json_schema_extra = {
            "example": {
                "candidate_name": "Jane Smith",
                "primary_date": "09/15/2024",
                "election_date": "11/05/2024",
                "office_and_jurisdiction": "School Board, At-Large, Chicopee, MA",
                "incumbent_status": "N/A",
                "race_type": "Nonpartisan",
                "seats_available": 3,
                "number_of_opponents": 7,
                "win_number": 2500,
                "total_likely_voters": 8500,
                "available_cell_phones": 1200,
                "available_landlines": 300,
                "additional_race_context": "Focus on education funding and infrastructure improvements"
            }
        }


class CleanedCampaignInfo(CampaignInfo):
    """Extended campaign information with parsed and cleaned fields."""
    
    city: str = Field(..., description="The city name extracted from the jurisdiction")
    state: str = Field(..., description="The state name or abbreviation extracted from the jurisdiction")
    state_full: str = Field(..., description="The full state name (e.g., Massachusetts)")
    election_date_formatted: str = Field(..., description="The election date formatted as YYYY-MM-DD")
    primary_date_formatted: Optional[str] = Field(None, description="The primary date formatted as YYYY-MM-DD if exists")
    
    class Config:
        """Pydantic model configuration."""
        use_enum_values = True
        json_encoders = {
            date: lambda v: v.strftime("%m/%d/%Y") if v else None
        }
        json_schema_extra = {
            "example": {
                "candidate_name": "Jane Smith",
                "primary_date": "09/15/2024",
                "election_date": "11/05/2024",
                "office_and_jurisdiction": "School Board, At-Large, Chicopee, MA",
                "incumbent_status": "N/A",
                "race_type": "Nonpartisan",
                "seats_available": 3,
                "number_of_opponents": 7,
                "win_number": 2500,
                "total_likely_voters": 8500,
                "available_cell_phones": 1200,
                "available_landlines": 300,
                "additional_race_context": "Focus on education funding and infrastructure improvements",
                "city": "Chicopee",
                "state": "MA",
                "state_full": "Massachusetts",
                "election_date_formatted": "2024-11-05",
                "primary_date_formatted": "2024-09-15"
            }
        }
