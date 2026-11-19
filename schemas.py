"""
Pydantic models for the Strive API.

Each model plays two roles:
  1. Request models validate what the browser sends to FastAPI.
  2. Response models are passed to Gemini as `response_schema`, so the model is
     forced to return JSON of exactly this shape, and FastAPI validates it once
     more before it leaves the server.

Field order and types matter: they define the JSON schema Gemini sees, so change
them together with the frontend.
"""

from typing import List, Literal, Optional

from pydantic import BaseModel

# ──────────────────────────────────────────────────────────
# Tutor: check work and hints   (POST /tutor)
# ──────────────────────────────────────────────────────────

class ErrorItem(BaseModel):
    problem_label: str = "Problem"  # e.g., "Problem 1", "Induction Step", "Base Case", "Equation 2"
    error_type: str = "logical"     # "syntax", "arithmetic", "logical"
    correction_message: str         # Socratic clue describing what step to review without giving answer
    error_location_x: float         # Normalized 0.05 to 0.95 (X position on image)
    error_location_y: float         # Normalized 0.05 to 0.95 (Y position on image)


class CorrectStep(BaseModel):
    step_label: str = "Correct step"
    explanation: str  # Specific description of what the student did correctly and why it is valid


class FrontendRequest(BaseModel):
    image_data: str
    action_type: Literal["check_logic", "get_hint"]
    is_selection: bool = False
    hint_focus: Optional[Literal["start", "next_step", "rule", "direction"]] = None
    learner_profile: Optional[dict] = None


class MathTutorResponse(BaseModel):
    is_correct_so_far: bool
    status_message: str
    correct_steps: List[CorrectStep] = []
    errors: List[ErrorItem] = []  # List of EVERY error found on the canvas (empty if completely correct)
    faint_hint: Optional[str] = None
    hint_location_x: Optional[float] = None  # Normalized position of the work the hint addresses
    hint_location_y: Optional[float] = None
    current_latex: str


class WalkthroughStep(BaseModel):
    latex: str
    goal: str
    reason: str
    check: str
    placement_x: float
    placement_y: float


class WalkthroughResponse(BaseModel):
    problem_summary: str
    steps: List[WalkthroughStep]


class MarkingCriterion(BaseModel):
    criterion: str
    expectation: str
    marks_available: float
    marks_awarded: float


class MarkAnnotation(BaseModel):
    label: str
    message: str
    marks_delta: float
    annotation_type: Literal["earned", "lost", "presentation"]
    location_x: float
    location_y: float


class QuestionMarking(BaseModel):
    question_label: str
    question_type: str
    task_intent: str
    rubric_basis: str
    marks_awarded: float
    marks_available: float
    criteria: List[MarkingCriterion]
    annotations: List[MarkAnnotation]
    improvement_summary: str
    full_marks_latex: str


class MarkingResponse(BaseModel):
    questions: List[QuestionMarking]
    total_awarded: float
    total_available: float
    overall_feedback: str
    confidence_note: str


# Analytics Diagnostic Models
class HistoryRecord(BaseModel):
    timestamp: str
    is_correct: bool
    latex: str = ""
    summary: str = ""
    error_clues: List[str] = []


class AnalyticsRequest(BaseModel):
    records: List[HistoryRecord]


class AnalyticsResponse(BaseModel):
    overall_summary: str
    mastery_score: int  # 0 to 100
    strong_points: List[str]
    weak_points: List[str]
    frequent_pitfalls: List[str]
    actionable_advice: List[str]
