from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class StepState(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    WARNING = "warning"
    BLOCKED = "blocked"
    RUNNING = "running"
    IDLE = "idle"


STATUS_META = {
    StepState.PASSED: ("🟢 Passed", "#1f7a1f"),
    StepState.FAILED: ("🔴 Failed", "#b00020"),
    StepState.WARNING: ("🟡 Warning", "#9a6700"),
    StepState.BLOCKED: ("⏸ Blocked", "#666666"),
    StepState.RUNNING: ("🔄 Running", "#005cc5"),
    StepState.IDLE: ("Not started", "#444444"),
}


@dataclass
class CheckResult:
    step_id: int
    title: str
    state: StepState
    summary: str
    details: str = ""
    severity: str = "Info"
    suggestion: str = ""
    cause_code: Optional[str] = None
    metadata: dict = field(default_factory=dict)
