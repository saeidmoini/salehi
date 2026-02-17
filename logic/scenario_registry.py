"""
Scenario registry: loads YAML scenario definitions and provides round-robin
assignment for contacts and inbound calls.
"""
import logging
from pathlib import Path
from typing import Dict, List, Optional

import yaml

from config.flow_definition import FlowStep, LLMConfig, ScenarioConfig, STTConfig


logger = logging.getLogger(__name__)


def _parse_flow_steps(raw_steps: list) -> List[FlowStep]:
    """Parse a list of raw YAML dicts into FlowStep objects."""
    steps = []
    for raw in raw_steps:
        steps.append(FlowStep(
            step=raw["step"],
            type=raw["type"],
            next=raw.get("next"),
            prompt=raw.get("prompt"),
            on_empty=raw.get("on_empty"),
            on_failure=raw.get("on_failure"),
            routes=raw.get("routes"),
            counter=raw.get("counter"),
            max_count=raw.get("max_count"),
            within_limit=raw.get("within_limit"),
            exceeded=raw.get("exceeded"),
            result=raw.get("result"),
            agent_type=raw.get("agent_type"),
            on_success=raw.get("on_success"),
        ))
    return steps


def _parse_scenario(data: dict) -> ScenarioConfig:
    """Parse a single YAML scenario dict into a ScenarioConfig."""
    sc = data.get("scenario", data)

    # Prompts
    prompts = dict(sc.get("prompts", {}))

    # STT config
    stt_raw = sc.get("stt", {})
    stt = STTConfig(
        hotwords=stt_raw.get("hotwords", []),
        max_duration=stt_raw.get("max_duration", 10),
        max_silence=stt_raw.get("max_silence", 2),
    )

    # LLM config
    llm_raw = sc.get("llm", {})
    llm = LLMConfig(
        prompt_template=llm_raw.get("prompt_template", ""),
        intent_categories=llm_raw.get("intent_categories", ["yes", "no", "number_question", "unknown"]),
        fallback_tokens=dict(llm_raw.get("fallback_tokens", {})),
    )

    # Flows
    flow = _parse_flow_steps(sc.get("flow", []))
    inbound_flow = _parse_flow_steps(sc.get("inbound_flow", []))

    return ScenarioConfig(
        name=sc.get("name", ""),
        display_name=sc.get("display_name", sc.get("name", "")),
        company=sc.get("company", ""),
        prompts=prompts,
        stt=stt,
        llm=llm,
        flow=flow,
        inbound_flow=inbound_flow,
    )


class ScenarioRegistry:
    """
    Loads scenario YAML files and provides round-robin access.
    """

    def __init__(self, scenarios_dir: str = "config/scenarios", company: str = ""):
        self._scenarios: Dict[str, ScenarioConfig] = {}
        self._enabled: List[str] = []
        self._outbound_cursor: int = 0
        self._inbound_cursor: int = 0
        self._company = (company or "").strip().lower()
        self._load_all(scenarios_dir)

    def _load_all(self, scenarios_dir: str) -> None:
        """Load all .yaml / .yml files from the scenarios directory."""
        path = Path(scenarios_dir)
        if not path.is_dir():
            logger.warning("Scenarios directory does not exist: %s", scenarios_dir)
            return
        for yaml_file in sorted(path.glob("*.yaml")) + sorted(path.glob("*.yml")):
            try:
                with open(yaml_file, encoding="utf-8") as f:
                    data = yaml.safe_load(f)
                if not data:
                    continue
                config = _parse_scenario(data)
                if not config.name:
                    config.name = yaml_file.stem
                scenario_company = (config.company or "").strip().lower()
                if self._company and scenario_company and scenario_company != self._company:
                    logger.info(
                        "Skipping scenario '%s' from %s (company=%s, current=%s)",
                        config.name, yaml_file.name, scenario_company, self._company
                    )
                    continue
                self._scenarios[config.name] = config
                self._enabled.append(config.name)
                logger.info("Loaded scenario '%s' from %s (%d outbound steps, %d inbound steps)",
                           config.name, yaml_file.name, len(config.flow), len(config.inbound_flow))
            except Exception as exc:
                logger.error("Failed to load scenario from %s: %s", yaml_file, exc)

    def get(self, name: str) -> Optional[ScenarioConfig]:
        return self._scenarios.get(name)

    def get_all(self) -> Dict[str, ScenarioConfig]:
        return dict(self._scenarios)

    def get_names(self) -> List[str]:
        return list(self._scenarios.keys())

    def get_enabled(self) -> List[str]:
        return list(self._enabled)

    def set_enabled(self, names: List[str]) -> None:
        """Update enabled scenarios from panel's active_scenarios list."""
        valid = [n for n in names if n in self._scenarios]
        if valid:
            self._enabled = valid
            self._outbound_cursor = 0
            self._inbound_cursor = 0
            logger.info("Active scenarios updated: %s", valid)
        else:
            logger.warning("No valid scenarios in active_scenarios list: %s", names)

    def next_scenario(self) -> Optional[str]:
        """Round-robin pick from enabled scenarios for outbound contacts."""
        if not self._enabled:
            return None
        name = self._enabled[self._outbound_cursor % len(self._enabled)]
        self._outbound_cursor = (self._outbound_cursor + 1) % len(self._enabled)
        return name

    def next_inbound_scenario(self) -> Optional[str]:
        """
        Round-robin pick from enabled scenarios that have an inbound_flow.
        Returns None if no scenario has inbound_flow (use direct-to-agent default).
        """
        candidates = [
            n for n in self._enabled
            if self._scenarios.get(n) and self._scenarios[n].inbound_flow
        ]
        if not candidates:
            return None
        name = candidates[self._inbound_cursor % len(candidates)]
        self._inbound_cursor = (self._inbound_cursor + 1) % len(candidates)
        return name
