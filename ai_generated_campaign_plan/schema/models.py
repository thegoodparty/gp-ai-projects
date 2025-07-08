from datetime import date
from typing import Optional, List
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
    
class AdditionalCampaignInfo(BaseModel):
    """Additional campaign information schema."""
    
    city: str = Field(..., description="The city name extracted from the jurisdiction")
    state: str = Field(..., description="The state name or abbreviation extracted from the jurisdiction")
    state_full: str = Field(..., description="The full state name (e.g., Massachusetts)")
    election_date_formatted: str = Field(..., description="The election date formatted as YYYY-MM-DD")
    has_primary: bool = Field(..., description="Whether the election has a primary")
    primary_date_formatted: Optional[str] = Field(None, description="The primary date formatted as YYYY-MM-DD if exists")

class ContactOptimization(BaseModel):
    """Contact optimization schema for voter contact planning."""
    
    p2p_texts: int = Field(..., ge=0, le=4, description="Number of P2P text messages (0-4)")
    robocalls: int = Field(..., ge=0, le=3, description="Number of robocalls (0-3)")

class CleanedCampaignInfo(CampaignInfo, AdditionalCampaignInfo):
    """Extended campaign information with parsed and cleaned fields."""

class SearchTermsList(BaseModel):
    """Pydantic model for a list of search terms."""
    search_terms: List[str] = Field(..., description="List of search terms")

