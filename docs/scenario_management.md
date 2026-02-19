# Scenario Management Guide

## Overview

The Salehi CallCenter project now supports **multiple call flow scenarios** through a single codebase. Instead of maintaining separate branches for different scenarios (Salehi and Sina), you can switch between scenarios using a simple environment variable.

## Benefits of This Approach

✅ **Single Codebase**: All shared code, bugfixes, and features in one place
✅ **Easy Deployment**: Just change `.env` on each server
✅ **No Merge Conflicts**: One branch to maintain
✅ **Testable**: Can test both scenarios from same codebase
✅ **Maintainable**: Update common features once, benefits both scenarios

## Available Scenarios

### Salehi Scenario

**Configuration**: `SCENARIO=salehi`

**Behavior**:
- Flow: hello → alo → record → classify intent
- When customer says YES: Play "yes" prompt → Mark as `connected_to_operator` → Disconnect
- Result status: `connected_to_operator` (reported as CONNECTED to panel)
- **No operator transfer** - disconnect is the success outcome
- **Audio prompts**: hello, alo, goodby, yes, number (for "where did you get my number" question)
- **STT hotwords**: Course and language-specific vocabulary (e.g., "دوره ایلتس", "دوره مکالمه", "ترکی", "فرانسه", "آلمانی")
- **LLM examples**: Language course names and language names for better intent classification

**Use Case**: Language academy marketing where you just want to confirm customer interest and report success without immediate operator transfer.

---

### Sina Scenario

**Configuration**: `SCENARIO=sina`

**Behavior**:
- Flow: hello → alo → record → classify intent
- When customer says YES: Play "yes" prompt → Play "onhold" → Connect to operator
- Result status: `connected_to_operator` (if operator answers and reports CONNECTED to panel) or `disconnected` (if customer hangs up before operator)
- **Full operator transfer** enabled
- **Audio prompts**: hello, alo, goodby, yes, onhold (number prompt NOT used)
- **STT hotwords**: General response vocabulary (e.g., "بله", "آره", "نه", "باشه")
- **LLM examples**: General yes/no responses (no number_question intent)

**Use Case**: General marketing where interested customers are immediately connected to a live operator for conversation.

---

## Configuration

### Environment Variable

Add this to your `.env` file:

```bash
# Scenario configuration
# Options: salehi, sina
SCENARIO=salehi
```

### How It Works

The `SCENARIO` environment variable controls multiple aspects of the system:

- `SCENARIO=salehi`:
  - `transfer_to_operator = False`
  - Audio files loaded from `assets/audio/salehi/src/`
  - STT hotwords: Course/language-specific vocabulary (from commit fc47e34)
  - LLM examples: Language course names and languages for intent classification
  - Number question prompt enabled

- `SCENARIO=sina`:
  - `transfer_to_operator = True`
  - Audio files loaded from `assets/audio/sina/src/`
  - STT hotwords: General response vocabulary
  - LLM examples: General yes/no responses
  - Number question NOT used

The marketing scenario logic ([logic/marketing_outreach.py](../logic/marketing_outreach.py:1-1200)) checks these settings to determine call flow, which prompts to play, which vocabulary to use for STT, and which examples to use for LLM classification.

---

## Deployment Scenarios

### Scenario 1: Two Servers, Different Scenarios

If you have two separate servers:

**Server 1 (Salehi)**:
```bash
# .env
SCENARIO=salehi
OUTBOUND_NUMBERS=02191302954
PANEL_BASE_URL=https://panel-salehi.example.com
# ... other settings
```

**Server 2 (Sina)**:
```bash
# .env
SCENARIO=sina
OUTBOUND_NUMBERS=02191302955
PANEL_BASE_URL=https://panel-sina.example.com
OPERATOR_MOBILE_NUMBERS=09121111111,09122222222
# ... other settings
```

**Deployment**:
1. Push changes to main branch
2. On each server: `./update.sh` (auto-detects scenario from `.env`)
3. Each server runs the scenario configured in its `.env`
4. Services are named by scenario: `salehi.service` or `sina.service`

---

### Scenario 2: One Server, Switch Scenarios

If you need to switch scenarios on the same server:

```bash
# Edit .env
nano .env
# Change: SCENARIO=salehi to SCENARIO=sina

# Restart appropriate service (service name matches scenario)
sudo systemctl restart salehi.service  # If switching from salehi
# OR
sudo systemctl restart sina.service   # If switching to sina

# Note: You may need to update service file name/config when switching scenarios
```

---

### Scenario 3: Testing Both Scenarios Locally

```bash
# Test Salehi scenario
SCENARIO=salehi python main.py

# Test Sina scenario (in another terminal)
SCENARIO=sina python main.py
```

---

## Migration from Branch-Based Approach

### Old Approach (Separate Branches)

**Problems**:
- Had to maintain code in both `salehi` and `sina` branches
- Merge conflicts when syncing changes
- Bugfixes needed to be applied twice
- Features added to one branch might not make it to the other
- Branch divergence over time
- Audio files had same names but different content, causing conflicts during merges

**Structure**:
```
git branch salehi:
  - Marketing scenario: disconnect on YES (result: disconnected)
  - Audio files: assets/audio/src/ (course-specific prompts)
  - STT/LLM: Language course vocabulary
  - Deployment config for Salehi environment

git branch sina:
  - Marketing scenario: transfer to operator on YES
  - Audio files: assets/audio/src/ (general prompts - DIFFERENT CONTENT!)
  - STT/LLM: General vocabulary
  - Deployment config for Sina environment
```

---

### New Approach (Single Branch with Config)

**Benefits**:
- One branch (`main`)
- Scenario switched via `.env` variable
- Shared code updated once
- Easy to test both scenarios
- No branch divergence
- No audio file conflicts (separate directories per scenario)

**Structure**:
```
git branch main:
  - Marketing scenario: checks settings.scenario.transfer_to_operator
  - Audio files separated by scenario:
    - assets/audio/salehi/src/ (course-specific prompts)
    - assets/audio/sina/src/ (general prompts)
  - STT hotwords: scenario-specific in code (line 67-122)
  - LLM examples: scenario-specific in code (line 611-673)
  - Config-driven deployment (.env with SCENARIO variable)
  - Service naming: Dynamic based on scenario (salehi.service or sina.service)
```

---

## Migration Steps

### Automated Migration (Recommended)

**Use the migration script for production servers:**

```bash
# From your current branch (salehi or sina)
bash migrate_to_main.sh
```

**What the script does:**
1. Detects your current scenario from branch name or `.env`
2. Backs up existing audio files with timestamp
3. Removes old `assets/audio/src/` and `assets/audio/wav/` directories (prevents conflicts)
4. Switches to `main` branch cleanly
5. Updates `.env` with detected scenario
6. Shows verification steps
7. Reminds you to run `./update.sh` and restart service

**After migration:**
```bash
# Run update script to install dependencies and sync audio
./update.sh

# Restart service (script shows correct command)
sudo systemctl restart salehi.service  # or sina.service
```

---

### Manual Migration (Development/Understanding)

**Step 1: Merge Your Branches (One-Time - Already Done)**

```bash
# This has been completed and main branch now exists with:
# - Scenario-specific audio directories
# - Scenario-based STT hotwords (from fc47e34)
# - Scenario-based LLM examples
# - Dynamic service naming in update.sh
```

### Step 2: Update Environment Files

**On Salehi Server**:
```bash
# Add to .env
SCENARIO=salehi
```

**On Sina Server**:
```bash
# Add to .env
SCENARIO=sina
OPERATOR_MOBILE_NUMBERS=09121111111,09122222222
```

### Step 3: Test

```bash
# On each server
python main.py

# Check logs for:
# "MarketingScenario initialized with scenario=salehi (transfer_to_operator=False)"
# or
# "MarketingScenario initialized with scenario=sina (transfer_to_operator=True)"
```

### Step 4: Deploy

```bash
# On each server
./update.sh

# The script will:
# - Auto-detect scenario from .env
# - Pull from main branch
# - Update dependencies
# - Sync audio files from assets/audio/<scenario>/src/
# - Restart correct service (salehi.service or sina.service)
```

### Step 5: Verify Migration

```bash
# Check scenario loaded correctly
tail -f logs/app.log | grep "MarketingScenario initialized"

# Should see:
# "MarketingScenario initialized with scenario=salehi (transfer_to_operator=False)"
# OR
# "MarketingScenario initialized with scenario=sina (transfer_to_operator=True)"

# Verify audio files
ls -lh assets/audio/salehi/src/  # Or sina
ls -lh /var/lib/asterisk/sounds/custom/

# Test a call and verify correct flow
```

### Step 6: Archive Old Branches (Optional)

```bash
# Once confirmed working on all servers, you can archive old branches
git tag archive/salehi salehi
git tag archive/sina sina
git branch -d salehi sina
git push origin :salehi :sina  # Delete remote branches (if desired)
```

---

## Customizing Scenarios

### Adding a New Scenario

If you need a third scenario (e.g., "custom"):

**1. Update [config/settings.py](../config/settings.py)**:

```python
# In get_settings()
scenario_name = os.getenv("SCENARIO", "salehi").lower()

# Add custom logic
if scenario_name == "custom":
    transfer_to_operator = False
    # Add other custom flags here
else:
    transfer_to_operator = (scenario_name == "sina")

scenario = ScenarioSettings(
    name=scenario_name,
    transfer_to_operator=transfer_to_operator,
)
```

**2. Update [logic/marketing_outreach.py](../logic/marketing_outreach.py)**:

```python
elif prompt_key == "yes":
    if self.settings.scenario.name == "custom":
        # Custom scenario logic
        await self._play_prompt(session, "custom_prompt")
        # ... custom flow
    elif self.settings.scenario.transfer_to_operator:
        # Sina scenario
        await self._play_onhold(session)
        await asyncio.sleep(0.5)
        await self._connect_to_operator(session)
    else:
        # Salehi scenario
        await self._set_result(session, "connected_to_operator", force=True, report=True)
        await self._hangup(session)
```

**3. Add scenario-specific STT hotwords and LLM examples:**

```python
# In __init__ method
if settings.scenario.name == "custom":
    self.stt_hotwords = ["custom", "vocab", "words"]
else:
    # Existing salehi/sina logic
    ...
```

**4. Add custom audio files**:

```bash
# Create scenario directory
mkdir -p assets/audio/custom/src/

# Add audio files
# assets/audio/custom/src/custom_prompt.mp3
# assets/audio/custom/src/hello.mp3
# ... etc

# Run audio sync
bash scripts/sync_audio.sh
```

**5. Use the new scenario**:

```bash
# In .env
SCENARIO=custom
```

---

### Adding Scenario-Specific Settings

You can extend `ScenarioSettings` to include more flags:

```python
@dataclass
class ScenarioSettings:
    name: str
    transfer_to_operator: bool
    use_sms_confirmation: bool  # New flag
    max_retries: int  # New flag

# In get_settings():
scenario = ScenarioSettings(
    name=scenario_name,
    transfer_to_operator=(scenario_name == "sina"),
    use_sms_confirmation=(scenario_name == "salehi"),
    max_retries=3 if scenario_name == "sina" else 1,
)
```

---

## Troubleshooting

### Issue: Wrong scenario is running

**Check**:
```bash
# View current scenario
grep SCENARIO .env

# Check logs
grep "MarketingScenario initialized" logs/app.log
```

**Fix**:
```bash
# Update .env
nano .env
# Set: SCENARIO=salehi (or sina)

# Restart
sudo systemctl restart salehi
```

---

### Issue: Operator transfer not working (Sina scenario)

**Check**:
```bash
# Verify scenario is set correctly
grep SCENARIO .env

# Check operator configuration
grep OPERATOR .env

# Check logs
grep "operator" logs/app.log -i
```

**Fix**:
```bash
# Ensure SCENARIO=sina
SCENARIO=sina

# Ensure operator mobiles are set
OPERATOR_MOBILE_NUMBERS=09121111111,09122222222
# OR
USE_PANEL_AGENTS=true
```

---

### Issue: Both scenarios behaving the same

**Possible Causes**:
1. `.env` not updated after code changes
2. Service not restarted after `.env` changes
3. Typo in SCENARIO value

**Fix**:
```bash
# Verify SCENARIO value (must be exactly "salehi" or "sina")
grep SCENARIO .env

# Restart service
sudo systemctl restart salehi

# Check logs for confirmation
tail -f logs/app.log | grep scenario
```

---

## Best Practices

### 1. Use Version Control for .env Templates

Don't commit `.env` files, but do maintain templates:

```bash
# Create scenario-specific templates
cp .env .env.salehi.template
cp .env .env.sina.template

# Add to .gitignore
echo ".env" >> .gitignore
echo ".env.*.template" >> .gitignore

# Document differences
git add docs/scenario_management.md
```

### 2. Test Both Scenarios Before Deployment

```bash
# Local testing
SCENARIO=salehi python main.py &
# Make a test call, verify behavior

# Kill
kill %1

SCENARIO=sina python main.py &
# Make a test call, verify operator transfer
```

### 3. Use Feature Flags for Experimental Features

Instead of creating new scenarios for experiments, use feature flags:

```bash
# In .env
ENABLE_VOICEMAIL=true
ENABLE_CALLBACK_QUEUE=false
```

```python
# In code
if self.settings.enable_voicemail:
    await self._send_to_voicemail(session)
```

### 4. Document Scenario Differences

Keep a table of what differs between scenarios:

| Feature | Salehi | Sina |
|---------|--------|-------|
| Operator Transfer (Outbound) | ❌ No | ✅ Yes |
| Inbound Handling | ✅ Direct to agent | ✅ Direct to agent |
| Inbound Result | `disconnected` (DISCONNECTED) | `disconnected` (DISCONNECTED) |
| "Yes" Prompt | ✅ Plays then disconnects | ✅ Plays then transfers |
| "Onhold" Prompt | ❌ Never plays (outbound) | ✅ Plays during transfer |
| "Number" Prompt | ✅ Yes (for "where did you get my number") | ❌ No |
| Result for YES | `connected_to_operator` | `connected_to_operator` |
| Panel Status for YES | CONNECTED | CONNECTED |
| Audio Directory | `assets/audio/salehi/src/` | `assets/audio/sina/src/` |
| STT Hotwords | Course/language names | General vocabulary |
| LLM Examples | Language courses, languages | General yes/no |
| Service Name | `salehi.service` | `sina.service` |
| Panel Integration | ✅ Yes | ✅ Yes |

### 5. Monitor Scenario in Production

Add scenario name to logs and monitoring:

```python
# Already implemented in marketing_outreach.py
logger.info(
    "MarketingScenario initialized with scenario=%s (transfer_to_operator=%s)",
    settings.scenario.name,
    settings.scenario.transfer_to_operator,
)
```

Check on startup:
```bash
tail -f logs/app.log | grep "MarketingScenario initialized"
```

---

## Summary

✅ **No more separate branches** - one codebase, multiple scenarios
✅ **Simple configuration** - just `SCENARIO=salehi` or `SCENARIO=sina`
✅ **Easy to extend** - add new scenarios or flags as needed
✅ **Testable** - test both scenarios locally before deployment
✅ **Maintainable** - shared features update both scenarios automatically

For questions or issues, refer to [CLAUDE.md](../CLAUDE.md) or [README.md](../README.md).
