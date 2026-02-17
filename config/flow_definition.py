"""
Data models for YAML-defined scenario flow configurations.

Each scenario defines prompts, STT/LLM config, and a step-based flow
for outbound calls (required) and optionally inbound calls.
"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class STTConfig:
    hotwords: List[str] = field(default_factory=list)
    max_duration: int = 10
    max_silence: int = 2


@dataclass
class LLMConfig:
    prompt_template: str = ""
    intent_categories: List[str] = field(default_factory=lambda: ["yes", "no", "number_question", "unknown"])
    fallback_tokens: Dict[str, List[str]] = field(default_factory=dict)


@dataclass
class FlowStep:
    """A single step in a call flow."""
    step: str  # unique step ID
    type: str  # entry, play_prompt, record, classify_intent, route_by_intent,
               # check_retry_limit, set_result, transfer_to_operator,
               # disconnect, hangup, wait
    # Navigation
    next: Optional[str] = None

    # play_prompt
    prompt: Optional[str] = None

    # record
    on_empty: Optional[str] = None
    on_failure: Optional[str] = None

    # classify_intent / route_by_intent
    routes: Optional[Dict[str, str]] = None

    # check_retry_limit
    counter: Optional[str] = None
    max_count: Optional[int] = None
    within_limit: Optional[str] = None
    exceeded: Optional[str] = None

    # set_result
    result: Optional[str] = None

    # transfer_to_operator
    agent_type: Optional[str] = None  # "inbound" or "outbound"
    on_success: Optional[str] = None
    # on_failure reused from record


@dataclass
class ScenarioConfig:
    """Full scenario configuration loaded from YAML."""
    name: str
    display_name: str = ""
    panel_name: str = ""

    # Prompt key -> ARI media path
    prompts: Dict[str, str] = field(default_factory=dict)

    stt: STTConfig = field(default_factory=STTConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)

    # Outbound call flow (required)
    flow: List[FlowStep] = field(default_factory=list)

    # Inbound call flow (optional — if missing, use direct-to-agent)
    inbound_flow: List[FlowStep] = field(default_factory=list)

    def get_step(self, step_id: str, inbound: bool = False) -> Optional[FlowStep]:
        """Look up a step by ID in the appropriate flow."""
        steps = self.inbound_flow if inbound else self.flow
        for s in steps:
            if s.step == step_id:
                return s
        return None

    def get_entry_step(self, inbound: bool = False) -> Optional[FlowStep]:
        """Find the entry step (type=entry) in the appropriate flow."""
        steps = self.inbound_flow if inbound else self.flow
        for s in steps:
            if s.type == "entry":
                return s
        return steps[0] if steps else None
