# Multi-Scenario Architecture

## Overview

The Salehi CallCenter project now supports **multiple call flow scenarios** running simultaneously through a single codebase. This architecture replaces the previous branch-based approach (salehi/agrad branches) with a dynamic, YAML-driven system that allows:

- **Multiple scenarios running in parallel**: Different call flows can be executed concurrently
- **Round-robin scenario assignment**: Contacts are automatically distributed across active scenarios
- **Dynamic scenario management**: Panel can enable/disable scenarios without code changes
- **Per-scenario agent rosters**: Separate inbound and outbound agent lists for each scenario
- **YAML-based configuration**: Define call flows without writing Python code

## Architecture Components

### 1. ScenarioRegistry (`logic/scenario_registry.py`)

**Purpose**: Loads and manages YAML scenario definitions

**Key Features**:
- Loads all `.yaml` files from `config/scenarios/` directory
- Provides round-robin selection for outbound and inbound calls
- Updates active scenarios from panel API
- Separate cursors for outbound and inbound scenario rotation

**API**:
```python
registry = ScenarioRegistry(scenarios_dir="config/scenarios")

# Get next scenario for outbound call (round-robin)
scenario_name = registry.next_scenario()

# Get next scenario for inbound call (round-robin, only scenarios with inbound_flow)
scenario_name = registry.next_inbound_scenario()

# Update active scenarios from panel
registry.set_enabled(["salehi_language", "agrad_marketing"])
```

### 2. FlowEngine (`logic/flow_engine.py`)

**Purpose**: Generic flow execution engine that interprets YAML-defined scenarios

**Replaces**: `MarketingScenario` (legacy)

**Key Features**:
- Step-based flow execution (play_prompt, record, classify_intent, route_by_intent, etc.)
- Per-scenario agent rosters (inbound_agents, outbound_agents)
- STT/LLM configuration per scenario
- Event-driven playback completion (no hardcoded delays)
- Supports both outbound and inbound flows

**Flow Step Types**:
- `entry`: Entry point for a flow
- `play_prompt`: Play audio to customer
- `record`: Record customer response
- `classify_intent`: Classify transcript using LLM
- `route_by_intent`: Branch by intent (yes/no/number_question/unknown)
- `check_retry_limit`: Check counter and branch
- `set_result`: Set call result
- `transfer_to_operator`: Connect to agent
- `disconnect`: Hangup call
- `hangup`: Hangup call
- `wait`: Pause execution (call stays active)

### 3. YAML Scenario Format (`config/scenarios/*.yaml`)

**Structure**:
```yaml
scenario:
  name: scenario_name
  display_name: "Display Name"

  # Audio prompts (key -> ARI media path)
  prompts:
    hello: "sound:custom/hello"
    yes: "sound:custom/yes"
    goodby: "sound:custom/goodby"
    onhold: "sound:custom/onhold"

  # STT configuration
  stt:
    hotwords:
      - "بله"
      - "آره"
      - "نه"
    max_duration: 10
    max_silence: 2

  # LLM configuration
  llm:
    prompt_template: |
      Classify intent into: yes / no / number_question / unknown.
      User: {transcript}
    intent_categories:
      - "yes"
      - "no"
      - "number_question"
      - "unknown"
    fallback_tokens:
      yes:
        - "بله"
        - "آره"
      no:
        - "نه"
        - "نمیخوام"

  # Outbound call flow (required)
  flow:
    - step: start
      type: entry
      next: play_hello

    - step: play_hello
      type: play_prompt
      prompt: hello
      next: record_response

    - step: record_response
      type: record
      next: classify
      on_empty: hangup_empty
      on_failure: hangup_failed

    - step: classify
      type: classify_intent
      next: route
      on_failure: hangup_failed

    - step: route
      type: route_by_intent
      routes:
        yes: handle_yes
        no: handle_no
        number_question: handle_number
        unknown: handle_unknown

    - step: handle_yes
      type: play_prompt
      prompt: yes
      next: result_connected

    - step: result_connected
      type: set_result
      result: connected_to_operator
      next: do_disconnect

    - step: do_disconnect
      type: disconnect

  # Inbound call flow (optional)
  inbound_flow:
    - step: start
      type: entry
      next: connect_agent

    - step: connect_agent
      type: transfer_to_operator
      agent_type: inbound
      on_success: result_inbound
      on_failure: result_disconnected

    - step: result_inbound
      type: set_result
      result: inbound_call
      next: wait_hangup

    - step: wait_hangup
      type: wait
```

## Integration Flow

### Startup Sequence (main.py)

```python
# 1. Load scenarios from YAML files
scenario_registry = ScenarioRegistry(scenarios_dir="config/scenarios")

# 2. Create SessionManager with registry
session_manager = SessionManager(
    ari_client,
    scenario_handler=None,
    scenario_registry=scenario_registry,
    allowed_inbound_numbers=settings.dialer.outbound_numbers,
)

# 3. Initialize FlowEngine
flow_engine = FlowEngine(
    settings=settings,
    ari_client=ari_client,
    llm_client=llm_client,
    stt_client=stt_client,
    session_manager=session_manager,
    registry=scenario_registry,
    panel_client=panel_client,
)
session_manager.scenario_handler = flow_engine

# 4. Initialize Dialer with registry
dialer = Dialer(
    settings,
    ari_client,
    session_manager,
    scenario_registry=scenario_registry,
    panel_client=panel_client,
)

# 5. Register scenarios with panel
if panel_client:
    await panel_client.register_scenarios(scenario_registry.get_names())
```

### Outbound Call Flow

```
1. Dialer fetches batch from panel
   ↓
2. Panel returns:
   - contacts
   - active_scenarios (list of enabled scenario names)
   - inbound_agents (list)
   - outbound_agents (list)
   ↓
3. Dialer updates registry: registry.set_enabled(active_scenarios)
   ↓
4. Dialer updates agent rosters: flow_engine.set_inbound_agents(), set_outbound_agents()
   ↓
5. For each contact:
   - scenario_name = registry.next_scenario()  # Round-robin
   - metadata["scenario_name"] = scenario_name
   - Create outbound session with metadata
   ↓
6. FlowEngine receives session via on_outbound_channel_created()
   ↓
7. FlowEngine looks up scenario: scenario = registry.get(session.metadata["scenario_name"])
   ↓
8. FlowEngine executes scenario.flow steps
```

### Inbound Call Flow

```
1. Inbound call arrives (StasisStart)
   ↓
2. SessionManager creates inbound session
   ↓
3. If scenario_registry available:
   - scenario_name = registry.next_inbound_scenario()  # Round-robin from scenarios with inbound_flow
   - metadata["scenario_name"] = scenario_name
   ↓
4. SessionManager calls flow_engine.on_inbound_channel_created()
   ↓
5. FlowEngine checks if scenario has inbound_flow:
   - If YES: Execute scenario.inbound_flow
   - If NO: Default direct-to-agent behavior
```

## Panel API Enhancements

### 1. Get Next Batch Endpoint

**Request**:
```http
GET /api/dialer/next-batch?size=10&company=salehi
```

**Response**:
```json
{
  "call_allowed": true,
  "batch": {
    "batch_id": "batch_123",
    "numbers": [
      {"id": 1, "phone_number": "09123456789"},
      {"id": 2, "phone_number": "09987654321"}
    ]
  },
  "active_scenarios": ["salehi_language", "agrad_marketing"],
  "inbound_agents": [
    {"id": 1, "phone_number": "09121111111"},
    {"id": 2, "phone_number": "09122222222"}
  ],
  "outbound_agents": [
    {"id": 3, "phone_number": "09123333333"},
    {"id": 4, "phone_number": "09124444444"}
  ],
  "timezone": "+0330",
  "server_time": "2026-02-14T10:30:00Z"
}
```

**Notes**:
- `active_scenarios`: List of scenario names to enable (round-robin assignment)
- `inbound_agents`: Agents for inbound calls (direct-to-agent)
- `outbound_agents`: Agents for outbound operator transfer
- Legacy `active_agents` field still supported for backward compatibility

### 2. Report Result Endpoint

**Request**:
```http
POST /api/dialer/report-result
Content-Type: application/json

{
  "company": "salehi",
  "number_id": 123,
  "phone_number": "09123456789",
  "status": "CONNECTED",
  "reason": "User said yes",
  "attempted_at": "2026-02-14T10:30:00Z",
  "batch_id": "batch_123",
  "agent_id": 3,
  "agent_phone": "09123333333",
  "user_message": "بله حتما",
  "scenario": "salehi_language",
  "outbound_line": "02191302954"
}
```

**New Fields**:
- `company`: Company identifier (from COMPANY env var)
- `scenario`: Scenario name that handled this call
- `outbound_line`: Which line was used for this call

### 3. Register Scenarios Endpoint

**Request**:
```http
POST /api/dialer/register-scenarios
Content-Type: application/json

{
  "company": "salehi",
  "scenarios": ["salehi_language", "agrad_marketing"]
}
```

**Purpose**: Called at startup to notify panel of available scenarios

## Configuration

### Environment Variables

**Old (branch-based)**:
```bash
SCENARIO=salehi  # or agrad
```

**New (multi-scenario)**:
```bash
# Company identifier for panel API
COMPANY=salehi

# Directory containing scenario YAML files
SCENARIOS_DIR=config/scenarios
```

### Removed Variables

```bash
# These are removed in multi-scenario architecture
MAX_CONCURRENT_INBOUND_CALLS=0
MAX_CONCURRENT_OUTBOUND_CALLS=0
```

**Reason**: Per-line limits (MAX_CONCURRENT_CALLS) now apply to combined inbound+outbound on each line. Global limits were rarely used and added complexity.

## Migration from Branch-Based Model

### Before (Salehi/Agrad Branches)

```
Repository
├── salehi (branch)
│   └── logic/marketing_outreach.py  # Salehi-specific flow
└── agrad (branch)
    └── logic/marketing_outreach.py  # Agrad-specific flow
```

**Problems**:
- Code duplication across branches
- Difficult to maintain shared improvements
- Cannot run multiple scenarios simultaneously
- Requires git checkout to switch scenarios

### After (Multi-Scenario Architecture)

```
Repository (single main branch)
├── config/scenarios/
│   ├── salehi_language.yaml
│   └── agrad_marketing.yaml
├── logic/
│   ├── flow_engine.py (generic interpreter)
│   └── scenario_registry.py
```

**Benefits**:
- Single codebase, no branches
- Multiple scenarios run concurrently
- Dynamic scenario management via panel
- YAML-based configuration (no code changes)
- Easy to add new scenarios

## Adding a New Scenario

### Step 1: Create YAML File

Create `config/scenarios/my_scenario.yaml`:

```yaml
scenario:
  name: my_scenario
  display_name: "My Custom Scenario"

  prompts:
    hello: "sound:custom/my_hello"
    yes: "sound:custom/yes"
    goodby: "sound:custom/goodby"

  stt:
    hotwords:
      - "بله"
      - "نه"
    max_duration: 10
    max_silence: 2

  llm:
    prompt_template: |
      Classify intent: yes / no / unknown.
      User: {transcript}
    intent_categories: ["yes", "no", "unknown"]
    fallback_tokens:
      yes: ["بله", "آره"]
      no: ["نه"]

  flow:
    - step: start
      type: entry
      next: play_hello

    - step: play_hello
      type: play_prompt
      prompt: hello
      next: record_response

    - step: record_response
      type: record
      next: classify

    - step: classify
      type: classify_intent
      next: route

    - step: route
      type: route_by_intent
      routes:
        yes: handle_yes
        no: handle_no
        unknown: handle_unknown

    - step: handle_yes
      type: set_result
      result: connected_to_operator
      next: do_disconnect

    - step: handle_no
      type: set_result
      result: not_interested
      next: do_hangup

    - step: handle_unknown
      type: set_result
      result: unknown
      next: do_hangup

    - step: do_disconnect
      type: disconnect

    - step: do_hangup
      type: hangup

  # Optional inbound flow
  inbound_flow:
    - step: start
      type: entry
      next: connect_agent

    - step: connect_agent
      type: transfer_to_operator
      agent_type: inbound
      on_success: result_inbound
      on_failure: result_disconnected

    - step: result_inbound
      type: set_result
      result: inbound_call
      next: wait_hangup

    - step: result_disconnected
      type: set_result
      result: disconnected
      next: do_hangup

    - step: wait_hangup
      type: wait

    - step: do_hangup
      type: hangup
```

### Step 2: Prepare Audio Files

Place audio files in `assets/audio/src/`:
- `my_hello.mp3` (or use existing prompts)

Run audio sync:
```bash
bash scripts/sync_audio.sh
```

### Step 3: Restart Application

```bash
sudo systemctl restart salehi
```

The new scenario will be automatically:
1. Loaded by ScenarioRegistry
2. Registered with panel API
3. Available for round-robin assignment

### Step 4: Enable in Panel

Update panel to include `"my_scenario"` in the `active_scenarios` list returned from `/api/dialer/next-batch`.

## Testing

### Test Outbound Scenario Assignment

1. Check logs at startup:
```bash
tail -f logs/app.log | grep "Loaded scenario"
```

Expected output:
```
Loaded scenario 'salehi_language' from salehi_language.yaml (32 outbound steps, 13 inbound steps)
Loaded scenario 'agrad_marketing' from agrad_marketing.yaml (30 outbound steps, 13 inbound steps)
Registered 2 scenarios with panel
```

2. Watch scenario assignment:
```bash
tail -f logs/app.log | grep "Assigned scenario"
```

Expected output (round-robin):
```
Assigned scenario 'salehi_language' to contact 09123456789
Assigned scenario 'agrad_marketing' to contact 09987654321
Assigned scenario 'salehi_language' to contact 09111111111
```

### Test Inbound Scenario Assignment

1. Call one of the OUTBOUND_NUMBERS
2. Check logs:
```bash
tail -f logs/app.log | grep "Assigned inbound scenario"
```

Expected output:
```
Assigned inbound scenario 'salehi_language' to session abc123
Running inbound flow 'salehi_language' for session abc123
```

### Test Panel Integration

1. Check scenario registration:
```bash
curl -X POST http://panel.example.com/api/dialer/register-scenarios \
  -H "Authorization: Bearer $PANEL_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"company": "salehi", "scenarios": ["salehi_language", "agrad_marketing"]}'
```

2. Test batch fetch:
```bash
curl "http://panel.example.com/api/dialer/next-batch?size=10&company=salehi" \
  -H "Authorization: Bearer $PANEL_API_TOKEN" | jq
```

Expected response should include:
```json
{
  "active_scenarios": ["salehi_language", "agrad_marketing"],
  "inbound_agents": [...],
  "outbound_agents": [...]
}
```

## Troubleshooting

### Scenario Not Loaded

**Symptoms**: Scenario missing from logs at startup

**Checks**:
1. Verify YAML file exists in SCENARIOS_DIR:
   ```bash
   ls -lah config/scenarios/
   ```

2. Validate YAML syntax:
   ```bash
   python3 -c "import yaml; yaml.safe_load(open('config/scenarios/my_scenario.yaml'))"
   ```

3. Check for errors in logs:
   ```bash
   grep "Failed to load scenario" logs/app.log
   ```

### Scenario Not Assigned

**Symptoms**: All calls use same scenario, no round-robin

**Checks**:
1. Verify active_scenarios from panel:
   ```bash
   grep "Active scenarios updated" logs/app.log
   ```

2. Check if panel is returning active_scenarios:
   ```bash
   grep "active_scenarios" logs/app.log
   ```

3. If panel doesn't support active_scenarios, all loaded scenarios will be active by default.

### Agent Not Available

**Symptoms**: "No available agents" errors

**Checks**:
1. Verify agent roster updates:
   ```bash
   grep "Updated inbound agents\|Updated outbound agents" logs/app.log
   ```

2. Check agent busy state:
   - Agents are marked busy while handling a call
   - Check if agents are stuck in busy state (likely a bug)

3. Fallback to static OPERATOR_MOBILE_NUMBERS:
   ```bash
   # In .env
   OPERATOR_MOBILE_NUMBERS=09121111111,09122222222
   ```

## Performance Considerations

### Scenario Count

- **Recommended**: 2-5 active scenarios
- **Maximum**: 10-20 scenarios (limited by YAML parsing overhead)
- Each scenario adds ~50-100ms to startup time

### Round-Robin Overhead

- Negligible: O(1) cursor increment per assignment
- No performance impact even with 100+ scenarios

### YAML Parsing

- Scenarios loaded once at startup
- No runtime YAML parsing
- ScenarioConfig objects cached in memory

## Future Enhancements

### 1. Hot Reload

Support reloading scenarios without restart:
```python
# POST /admin/reload-scenarios
await scenario_registry.reload()
```

### 2. Weighted Distribution

Instead of round-robin:
```yaml
scenario:
  weight: 70  # 70% of calls
```

### 3. Conditional Assignment

Assign scenarios based on contact attributes:
```yaml
scenario:
  conditions:
    - field: contact_tags
      contains: "premium"
```

### 4. Per-Scenario Rate Limits

```yaml
scenario:
  rate_limits:
    max_calls_per_minute: 5
    max_calls_per_day: 50
```

## Summary

The multi-scenario architecture provides:

✅ **Dynamic scenario management**: No code changes to add/modify scenarios
✅ **Concurrent execution**: Multiple scenarios run in parallel
✅ **Round-robin distribution**: Fair assignment across active scenarios
✅ **Per-scenario configuration**: STT/LLM/audio/flow customizable per scenario
✅ **Panel integration**: Scenarios controlled remotely via API
✅ **Backward compatibility**: Existing panel APIs still work (legacy active_agents)
✅ **Zero-downtime updates**: Add scenarios without restart (future)

This architecture eliminates the need for git branches, simplifies deployment, and enables advanced campaign management through the panel API.
