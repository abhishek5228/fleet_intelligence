"""Request/response models for the HTTP API."""
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class IncidentInput(BaseModel):
    # Generous caps: these strings are pasted into LLM prompts and WhatsApp messages, so an
    # unbounded field is both a cost and an abuse vector.
    vehicle_id: str = Field(max_length=40)
    location: str = Field(max_length=300)
    destination: Optional[str] = Field("Authorized Heavy Workshop Hub", max_length=300)
    issue_type: str = Field(max_length=500)
    severity: str = Field(max_length=40)
    cargo_type: str = Field(max_length=120)
    image_url: Optional[str] = Field(None, max_length=2000)  # 5. Multi-Modal Vision support


class AgentTrace(BaseModel):
    step_name: str
    agent_role: str
    status: str
    timestamp: float
    output_payload: Dict[str, Any]


class TriageResponse(BaseModel):
    incident_id: str
    status: str
    execution_time_ms: float
    deterministic_steps_executed: int
    assigned_whatsapp_number: str
    approval_status: str
    traces: List[AgentTrace]
    final_resolution: Dict[str, Any]
