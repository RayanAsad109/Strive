"""
Strive API: FastAPI backend.

A small, stateless server. Every request carries everything it needs (a PNG of
the student's page, a few options, an optional learner profile), so the server
keeps no session data. Notebooks live in the browser and in Supabase, not here.

How each AI route works:

    browser --JSON--> route --> decode image + build prompt
                            --> Gemini (image + system prompt + response schema)
            <--JSON-- validated Pydantic response

Files:
    main.py     routes and small helpers (this file)
    schemas.py  Pydantic request/response models
    prompts.py  every piece of text sent to Gemini
"""

import base64
import json
import os
from typing import List, Type, TypeVar

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from google import genai
from pydantic import BaseModel

import prompts
from schemas import (
    AnalyticsRequest,
    AnalyticsResponse,
    FrontendRequest,
    HistoryRecord,
    MarkingResponse,
    MathTutorResponse,
    WalkthroughResponse,
)

# ──────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(BASE_DIR, ".env")

# Models are tried in this order; if one fails (quota, outage, bad output) the
# next one gets the same request.
ACTIVE_MODELS = [
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.7-flash",
]

# Sampling temperatures per feature (lower = more deterministic).
TEMPERATURE_TUTOR = 0.2
TEMPERATURE_WALKTHROUGH = 0.2
TEMPERATURE_MARKING = 0.15
TEMPERATURE_ANALYTICS = 0.3

# The learner profile is user-supplied JSON that ends up inside the prompt, so
# its size is capped.
MAX_LEARNER_PROFILE_CHARS = 1500


def _read_dotenv(name: str) -> str:
    """Return `name` from the .env file next to this module, or '' if not set."""
    if not os.path.exists(ENV_FILE):
        return ""
    try:
        with open(ENV_FILE, "r", encoding="utf-8") as file:
            for line in file:
                key, separator, raw_value = line.strip().partition("=")
                if separator and key == name:
                    value = raw_value.strip().strip('"').strip("'")
                    if value:
                        return value
    except (OSError, UnicodeError):
        pass
    return ""


def get_api_key() -> str:
    """Gemini key from the environment first, then the local .env file."""
    key = (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or "").strip()
    if key:
        return key
    key = _read_dotenv("GEMINI_API_KEY")
    if key:
        return key
    raise RuntimeError("GEMINI_API_KEY not found in environment or .env file.")


def get_public_setting(name: str) -> str:
    """A browser-safe setting (e.g. the Supabase URL) from the environment or .env."""
    return os.environ.get(name, "").strip() or _read_dotenv(name)


# ──────────────────────────────────────────────────────────
# App setup
# ──────────────────────────────────────────────────────────

app = FastAPI(title="Calculus Copilot API")
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_client = None


def get_client() -> genai.Client:
    """Create the Gemini client on first use and reuse it afterwards."""
    global _client
    if _client is None:
        try:
            key = get_api_key()
        except RuntimeError:
            raise HTTPException(
                status_code=503,
                detail="Your notebook is saved. To enable the tutor, configure GEMINI_API_KEY on the server and restart it.",
            )
        _client = genai.Client(api_key=key)
    return _client


# ──────────────────────────────────────────────────────────
# Helpers shared by the AI routes
# ──────────────────────────────────────────────────────────

ModelT = TypeVar("ModelT", bound=BaseModel)


def log(message: str) -> None:
    print(message, flush=True)


def decode_image(image_data: str) -> bytes:
    """Decode the browser's base64 PNG (with or without a `data:...;base64,` prefix)."""
    try:
        return base64.b64decode(image_data.split("base64,")[-1])
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid base64 image data: {exc}")


def image_contents(image_bytes: bytes, prompt_text: str) -> list:
    """Gemini input for the image routes: the page image followed by the instruction."""
    return [genai.types.Part.from_bytes(data=image_bytes, mime_type="image/png"), prompt_text]


def clean_and_parse_json(text: str) -> dict:
    """Parse model output as JSON, tolerating ```json fences and stray text around it."""
    text = text.strip()
    for fence in ("```json", "```"):
        if text.startswith(fence):
            text = text[len(fence):]
            break
    if text.endswith("```"):
        text = text[:-3]
    text = text.strip()

    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end != -1:
        text = text[start:end + 1]

    return json.loads(text)


def generate_structured(
    *,
    contents: list,
    system_instruction: str,
    schema: Type[ModelT],
    temperature: float,
    label: str,
    failure_message: str,
) -> ModelT:
    """
    Ask Gemini for JSON matching `schema`, trying each model in ACTIVE_MODELS.

    Returns the parsed `schema` instance from the first model that works. If every
    model fails, raises HTTP 500 with `failure_message` and the last error.
    A missing API key surfaces as HTTP 503 (see get_client).
    """
    client = get_client()
    last_error = None

    for model_name in ACTIVE_MODELS:
        try:
            log(f"[{label}] Calling {model_name}...")
            response = client.models.generate_content(
                model=model_name,
                contents=contents,
                config=genai.types.GenerateContentConfig(
                    system_instruction=system_instruction,
                    temperature=temperature,
                    response_mime_type="application/json",
                    response_schema=schema,
                ),
            )
            return schema(**clean_and_parse_json(response.text or ""))
        except Exception as exc:
            last_error = exc
            log(f"[{label} WARN] {model_name} error: {exc}. Trying next model...")

    raise HTTPException(status_code=500, detail=f"{failure_message}: {last_error}")


# ──────────────────────────────────────────────────────────
# Prompt builders (pure functions: easy to test)
# ──────────────────────────────────────────────────────────

def build_tutor_prompt(request: FrontendRequest) -> str:
    """Instruction text for /tutor: depends on hint-vs-check, selection-vs-page, and learner profile."""
    if request.action_type == "get_hint":
        prompt = prompts.HINT_PROMPT_SELECTION if request.is_selection else prompts.HINT_PROMPT_PAGE
        focus = prompts.HINT_FOCUS_GUIDANCE.get(request.hint_focus, "")
        if focus:
            prompt += " " + focus
    else:
        prompt = prompts.CHECK_PROMPT_SELECTION if request.is_selection else prompts.CHECK_PROMPT_PAGE

    if request.learner_profile:
        profile = json.dumps(request.learner_profile, ensure_ascii=True)[:MAX_LEARNER_PROFILE_CHARS]
        prompt += prompts.LEARNER_PROFILE_INSTRUCTION.format(profile=profile)
    return prompt


def build_analytics_prompt(records: List[HistoryRecord]) -> str:
    """Flatten the student's attempt history into one numbered text block."""
    lines = []
    for number, record in enumerate(records, start=1):
        status = "PASSED (Correct)" if record.is_correct else "FLAGGED ERRORS"
        errors = "; ".join(record.error_clues) if record.error_clues else "None"
        lines.append(
            f"Attempt #{number} [{record.timestamp}]: Status: {status} | "
            f"Math: {record.latex} | Summary: {record.summary} | Errors: {errors}"
        )
    return prompts.ANALYTICS_PROMPT_INTRO + "\n".join(lines) + prompts.ANALYTICS_PROMPT_OUTRO


def empty_analytics_response() -> AnalyticsResponse:
    """Friendly placeholder when there is no practice history yet (no model call needed)."""
    return AnalyticsResponse(
        overall_summary="No math checks recorded yet. Start solving problems on the scratchpad and your diagnostic profile will build automatically!",
        mastery_score=100,
        strong_points=["Ready to start practice"],
        weak_points=[],
        frequent_pitfalls=[],
        actionable_advice=["Draw or type your first calculus equation or proof to begin tracking mastery."],
    )


# ──────────────────────────────────────────────────────────
# Routes: frontend and config
# ──────────────────────────────────────────────────────────

@app.get("/")
async def serve_frontend():
    """Serve index.html from the same directory as main.py."""
    return FileResponse(os.path.join(BASE_DIR, "index.html"), media_type="text/html")


@app.get("/config")
async def public_config():
    """Expose only browser-safe Supabase project configuration."""
    url = get_public_setting("SUPABASE_URL")
    publishable_key = get_public_setting("SUPABASE_PUBLISHABLE_KEY") or get_public_setting("SUPABASE_ANON_KEY")
    return {
        "supabase_url": url,
        "supabase_publishable_key": publishable_key,
        "supabase_enabled": bool(url and publishable_key),
    }


# ──────────────────────────────────────────────────────────
# Routes: AI tutor
#
# These are plain `def` (not `async def`) on purpose: the Gemini SDK call is
# blocking, and FastAPI runs `def` routes in a thread pool so one slow request
# does not freeze every other user.
# ──────────────────────────────────────────────────────────

@app.post("/tutor", response_model=MathTutorResponse)
def tutor(request: FrontendRequest):
    """
    Check the student's work or give a hint.

    Takes a base64 PNG of the page (or a cropped selection) and returns feedback
    with LaTeX formatting and normalized (0 to 1) coordinates for each issue.
    """
    system_instruction = (
        prompts.SYSTEM_GET_HINT if request.action_type == "get_hint" else prompts.SYSTEM_CHECK_LOGIC
    )
    image_bytes = decode_image(request.image_data)
    return generate_structured(
        contents=image_contents(image_bytes, build_tutor_prompt(request)),
        system_instruction=system_instruction,
        schema=MathTutorResponse,
        temperature=TEMPERATURE_TUTOR,
        label="TUTOR",
        failure_message="Gemini API error across all models",
    )


@app.post("/walkthrough", response_model=WalkthroughResponse)
def walkthrough(request: FrontendRequest):
    """Generate a progressive, explicitly requested worked solution."""
    image_bytes = decode_image(request.image_data)
    prompt_text = (
        prompts.WALKTHROUGH_PROMPT_SELECTION if request.is_selection else prompts.WALKTHROUGH_PROMPT_PAGE
    )
    return generate_structured(
        contents=image_contents(image_bytes, prompt_text),
        system_instruction=prompts.SYSTEM_WALKTHROUGH,
        schema=WalkthroughResponse,
        temperature=TEMPERATURE_WALKTHROUGH,
        label="WALKTHROUGH",
        failure_message="Walkthrough generation failed across all models",
    )


@app.post("/marking", response_model=MarkingResponse)
def mark_submission(request: FrontendRequest):
    """Mark each visible question with a task-specific rubric and anchored feedback."""
    image_bytes = decode_image(request.image_data)
    prompt_text = prompts.MARKING_PROMPT_SELECTION if request.is_selection else prompts.MARKING_PROMPT_PAGE
    return generate_structured(
        contents=image_contents(image_bytes, prompt_text),
        system_instruction=prompts.SYSTEM_MARKING,
        schema=MarkingResponse,
        temperature=TEMPERATURE_MARKING,
        label="MARKING",
        failure_message="Marking failed across all models",
    )


@app.post("/analytics", response_model=AnalyticsResponse)
def generate_analytics(request: AnalyticsRequest):
    """
    Analyze the student's accumulated practice history and return a diagnostic
    report of strong points, weak points, and recurring pitfalls. Text only: no image.
    """
    if not request.records:
        return empty_analytics_response()

    return generate_structured(
        contents=[build_analytics_prompt(request.records)],
        system_instruction=prompts.SYSTEM_ANALYTICS,
        schema=AnalyticsResponse,
        temperature=TEMPERATURE_ANALYTICS,
        label="ANALYTICS",
        failure_message="Analytics generation error",
    )
