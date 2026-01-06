# Scenario Management Guide

## Overview

The Salehi CallCenter project now supports **multiple call flow scenarios** through a single codebase. Instead of maintaining separate branches for different scenarios (Salehi and Agrad), you can switch between scenarios using a simple environment variable.

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
- When customer says YES: Play "yes" prompt → Disconnect
- Result status: `disconnected`
- **No operator transfer**

**Use Case**: Simple acknowledgment flow where you just want to confirm customer interest without immediate transfer.

---

### Agrad Scenario

**Configuration**: `SCENARIO=agrad`

**Behavior**:
- When customer says YES: Play "yes" prompt → Play "onhold" → Connect to operator
- Result status: `connected_to_operator` (if operator answers) or `disconnected` (if operator unavailable)
- **Full operator transfer**

**Use Case**: Full-service flow where interested customers are immediately connected to a live operator.

---

## Configuration

### Environment Variable

Add this to your `.env` file:

```bash
# Scenario configuration
# Options: salehi, agrad
SCENARIO=salehi
```

### How It Works

The `SCENARIO` environment variable controls the `transfer_to_operator` flag in the system:

- `SCENARIO=salehi` → `transfer_to_operator = False`
- `SCENARIO=agrad` → `transfer_to_operator = True`

The marketing scenario logic ([logic/marketing_outreach.py](../logic/marketing_outreach.py)) checks this flag to determine the flow after the "yes" prompt.

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

**Server 2 (Agrad)**:
```bash
# .env
SCENARIO=agrad
OUTBOUND_NUMBERS=02191302955
PANEL_BASE_URL=https://panel-agrad.example.com
OPERATOR_MOBILE_NUMBERS=09121111111,09122222222
# ... other settings
```

**Deployment**:
1. Push changes to main branch
2. On each server: `git pull && ./update.sh`
3. Each server runs the scenario configured in its `.env`

---

### Scenario 2: One Server, Switch Scenarios

If you need to switch scenarios on the same server:

```bash
# Edit .env
nano .env
# Change: SCENARIO=salehi to SCENARIO=agrad

# Restart service
sudo systemctl restart salehi
```

---

### Scenario 3: Testing Both Scenarios Locally

```bash
# Test Salehi scenario
SCENARIO=salehi python main.py

# Test Agrad scenario (in another terminal)
SCENARIO=agrad python main.py
```

---

## Migration from Branch-Based Approach

### Old Approach (Separate Branches)

**Problems**:
- Had to maintain code in both `salehi` and `agrad` branches
- Merge conflicts when syncing changes
- Bugfixes needed to be applied twice
- Features added to one branch might not make it to the other
- Branch divergence over time

**Structure**:
```
git branch salehi:
  - Marketing scenario: disconnect on YES
  - Audio files specific to Salehi
  - Deployment config for Salehi environment

git branch agrad:
  - Marketing scenario: transfer to operator on YES
  - Audio files specific to Agrad
  - Deployment config for Agrad environment
```

---

### New Approach (Single Branch with Config)

**Benefits**:
- One branch (`main` or `salehi` or whatever you choose)
- Scenario switched via `.env` variable
- Shared code updated once
- Easy to test both scenarios
- No branch divergence

**Structure**:
```
git branch main:
  - Marketing scenario: checks settings.scenario.transfer_to_operator
  - Audio files for all scenarios (assets/audio/)
  - Config-driven deployment (.env)
```

---

## Migration Steps

### Step 1: Merge Your Branches (One-Time)

```bash
# Assuming you have 'salehi' and 'agrad' branches

# 1. Checkout the branch you want to keep as main (e.g., salehi)
git checkout salehi

# 2. Pull latest changes
git pull origin salehi

# 3. Review differences between branches
git diff salehi agrad

# 4. Manually merge any scenario-specific features from agrad
#    (Usually just different audio files or operator settings)

# 5. The code now supports both scenarios via SCENARIO env var
```

### Step 2: Update Environment Files

**On Salehi Server**:
```bash
# Add to .env
SCENARIO=salehi
```

**On Agrad Server**:
```bash
# Add to .env
SCENARIO=agrad
OPERATOR_MOBILE_NUMBERS=09121111111,09122222222
```

### Step 3: Test

```bash
# On each server
python main.py

# Check logs for:
# "MarketingScenario initialized with scenario=salehi (transfer_to_operator=False)"
# or
# "MarketingScenario initialized with scenario=agrad (transfer_to_operator=True)"
```

### Step 4: Deploy

```bash
# On each server
./update.sh
```

### Step 5: Archive Old Branches (Optional)

```bash
# Once confirmed working, you can archive the old branch
git tag archive/agrad agrad
git branch -d agrad
git push origin :agrad  # Delete remote branch
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
    transfer_to_operator = (scenario_name == "agrad")

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
        # Agrad scenario
        await self._play_onhold(session)
        await asyncio.sleep(0.5)
        await self._connect_to_operator(session)
    else:
        # Salehi scenario
        await self._set_result(session, "disconnected", force=True, report=True)
        await self._hangup(session)
```

**3. Add custom audio files**:

```bash
# Add assets/audio/src/custom_prompt.mp3
# Run audio sync
bash scripts/sync_audio.sh
```

**4. Use the new scenario**:

```bash
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
    transfer_to_operator=(scenario_name == "agrad"),
    use_sms_confirmation=(scenario_name == "salehi"),
    max_retries=3 if scenario_name == "agrad" else 1,
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
# Set: SCENARIO=salehi (or agrad)

# Restart
sudo systemctl restart salehi
```

---

### Issue: Operator transfer not working (Agrad scenario)

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
# Ensure SCENARIO=agrad
SCENARIO=agrad

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
# Verify SCENARIO value (must be exactly "salehi" or "agrad")
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
cp .env .env.agrad.template

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

SCENARIO=agrad python main.py &
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

| Feature | Salehi | Agrad |
|---------|--------|-------|
| Operator Transfer | ❌ No | ✅ Yes |
| "Yes" Prompt | ✅ Plays then disconnects | ✅ Plays then transfers |
| "Onhold" Prompt | ❌ Never plays | ✅ Plays during transfer |
| Result for YES | `disconnected` | `connected_to_operator` |
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
✅ **Simple configuration** - just `SCENARIO=salehi` or `SCENARIO=agrad`
✅ **Easy to extend** - add new scenarios or flags as needed
✅ **Testable** - test both scenarios locally before deployment
✅ **Maintainable** - shared features update both scenarios automatically

For questions or issues, refer to [CLAUDE.md](../CLAUDE.md) or [README.md](../README.md).
