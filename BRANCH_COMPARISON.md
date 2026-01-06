# Complete Branch Comparison: Salehi vs Agrad

**Analysis Date**: 2026-01-06
**Purpose**: Comprehensive comparison of ALL differences between salehi and agrad branches

---

## Summary of Differences

| Category | Salehi Branch | Agrad Branch | Impact |
|----------|---------------|--------------|--------|
| **Call Flow** | Disconnect on YES | Transfer to operator on YES | Critical |
| **Audio Files** | 5 files (some different content) | 7 files (includes alo.mp3, repeat.mp3) | Critical |
| **Failure Handling** | Basic "missed" status | Enhanced with busy/banned detection | Important |
| **Session Cleanup** | Single cleanup | Duplicate cleanup protection | Bug fix |
| **Early Failure Detection** | No | Yes (SIP cause code detection) | Important |
| **Rate Limits** | Conservative (2/10/200) | Aggressive (50/50/1000) | Config |
| **Operator Config** | Panel agents disabled | Panel agents enabled | Config |
| **Update Script** | Basic | Enhanced with better error handling | Minor |

---

## 1. Audio Files Differences

### Files Present in Agrad but NOT in Salehi

1. **alo.mp3** - Quick acknowledgment sound (25KB)
2. **repeat.mp3** - Prompt repetition (77KB)

### Files with Different Content (Same Names, Different Size/Content)

| File | Salehi Size | Agrad Size | Difference |
|------|-------------|------------|------------|
| **hello.mp3** | 324KB | 391KB | **+67KB** - Likely different wording/voice |
| **goodby.mp3** | 46KB | 139KB | **+93KB** - Significantly different |
| **yes.mp3** | 129KB | 116KB | **-13KB** - Slightly different |

### Files That Are the Same

- **number.mp3** - Appears unchanged
- **onhold.mp3** - Appears unchanged

### Impact

🔴 **CRITICAL**: Audio files contain different spoken content between scenarios. Cannot simply use env var to switch - need scenario-specific audio directories!

---

## 2. Code Differences

### A. logic/marketing_outreach.py

#### Salehi Branch (lines 677-690)

```python
async def _handle_yes(self, session: Session) -> None:
    async with session.lock:
        session.metadata["intent_yes"] = "1"
        session.metadata["yes_at"] = str(time.time())
    # Salehi branch: do not connect to operator. Just acknowledge and end the call.
    await self._play_prompt(session, "yes")
    # Give the prompt a moment to play before tearing down the call.
    try:
        await asyncio.sleep(2)
    except Exception:
        pass
    await self._set_result(session, "disconnected", force=True, report=True)
    channel_id = self._customer_channel_id(session)
    if channel_id:
        try:
            await self.ari_client.hangup_channel(channel_id)
        except Exception as exc:
            logger.debug("Hangup after yes failed for session %s: %s", session.session_id, exc)
```

#### Agrad Branch (lines 676-679)

```python
async def _handle_yes(self, session: Session) -> None:
    async with session.lock:
        session.metadata["intent_yes"] = "1"
        session.metadata["yes_at"] = str(time.time())
    await self._play_prompt(session, "yes")
    # (Then on_playback_finished handles operator transfer)
```

**Impact**: Salehi immediately disconnects after "yes". Agrad continues to operator transfer.

---

#### Failure Detection Enhancement (Agrad only)

**Lines 241-246** in agrad:
```python
# Customer leg failed/busy/unanswered => missed
reason_l = reason.lower() if reason else ""
result_value = "missed"
if "busy" in reason_l:
    result_value = "busy"
elif "congest" in reason_l or "failed" in reason_l:
    result_value = "banned"
```

**Impact**: Agrad provides better failure classification (busy, banned vs just "missed").

---

### B. sessions/session_manager.py

#### Early Failure Detection (Agrad Addition - lines 292-304)

```python
else:
    # Detect early busy/congestion signals so we don't wait for timeout.
    # PJSIP may send Progress (183) with Reason cause=17/34/41/42 or text.
    cause_raw = event.get("cause") or channel.get("cause")
    cause = str(cause_raw) if cause_raw is not None else None
    cause_txt = event.get("cause_txt") or channel.get("cause_txt")
    busy_like = {"17", "18", "19", "20", "21", "34", "41", "42"}
    if (
        (cause in busy_like)
        or (cause_raw in {17, 18, 19, 20, 21, 34, 41, 42})
        or (cause_txt and any(x in cause_txt.lower() for x in ["busy", "congest"]))
    ):
        if self.scenario_handler:
            await self.scenario_handler.on_call_failed(session, reason=cause_txt or cause)
```

**Impact**: Agrad detects failures earlier via SIP cause codes instead of waiting for timeout.

---

#### Duplicate Cleanup Protection (Agrad Addition - lines 416-418)

```python
async def _cleanup_session(self, session: Session) -> None:
    async with session.lock:
        if session.metadata.get("cleanup_done") == "1":
            return
```

**Impact**: Bug fix preventing double cleanup.

---

#### Enhanced Hangup Failure Notification (Agrad Addition - lines 354-365)

```python
# If we have a clear failure cause (busy/congest/power-off/banned), notify scenario before hangup finish.
busy_like = {"17", "18", "19", "20", "21", "34", "41", "42"}
if self.scenario_handler and (
    (cause and (str(cause) in busy_like or cause in {17, 18, 19, 20, 21, 34, 41, 42}))
    or (cause_txt and any(x in cause_txt.lower() for x in ["busy", "congest"]))
):
    try:
        await self.scenario_handler.on_call_failed(
            session, reason=(cause_txt or (str(cause) if cause is not None else None))
        )
    except Exception as exc:  # best-effort; don't block cleanup
        logger.debug("on_call_failed during hangup failed for %s: %s", session.session_id, exc)
```

**Impact**: Better failure handling and reporting.

---

#### Additional Logging (Agrad Additions)

**Line 111-112**:
```python
if not (event.get("channel") or {}).get("id") in self.channel_to_session:
    logger.debug("HangupRequest before session map: %s", event)
```

**Lines 126-128**:
```python
elif event_type == "Dial":
    # Visibility into pre-Stasis dial failures (cause/dialstatus may appear here).
    logger.info("Dial event: %s", event)
```

**Line 268**:
```python
logger.info("Channel state change for unknown channel %s payload=%s", channel_id, event)
```

**Impact**: Better debugging/troubleshooting in Agrad.

---

### C. .env.example Configuration Differences

| Setting | Salehi | Agrad | Reason |
|---------|--------|-------|--------|
| `STATIC_CONTACTS` | Not set | Empty line | Documentation |
| `MAX_CONCURRENT_CALLS` | 2 | 50 | **Agrad handles more volume** |
| `MAX_CALLS_PER_MINUTE` | 10 | 50 | **Agrad handles more volume** |
| `MAX_CALLS_PER_DAY` | 200 | 1000 | **Agrad handles more volume** |
| `MAX_ORIGINATIONS_PER_SECOND` | 3 | 1 | **Agrad more conservative per-second** |
| `DIALER_BATCH_SIZE` | 10 | 50 | **Agrad larger batches** |
| `OPERATOR_ENDPOINT` | `Local/6005@from-internal` | `Local/6005@internal` | Minor context difference |
| `USE_PANEL_AGENTS` | false | **true** | **Agrad uses panel agents** |
| `AST_SOUND_DIR` | Not set | `/usr/share/asterisk/sounds/custom` | Explicit path |

**Impact**: Agrad is configured for higher throughput and uses panel agents.

---

### D. logic/dialer.py

**Line 1 change** (minor):
```diff
- # (Salehi version - no visible diff in main logic)
+ # (Agrad version - no visible diff in main logic)
```

No significant functional difference shown in diff.

---

### E. update.sh

Agrad has enhanced error handling and better status messages (based on diffstat showing changes).

---

### F. utils/audio_sync.py

Agrad has 47 lines of changes/enhancements to audio sync utility (likely better error handling or support for additional formats).

---

### G. Additional Files

**callcenter_agent.md** - 82 new lines in Agrad (additional documentation).

---

## 3. Key Insights

### Audio Files Are Scenario-Specific! 🔴

The most critical finding: **Audio files are NOT just renamed - they have different content!**

- `hello.mp3` is 67KB larger in Agrad (different spoken message)
- `goodby.mp3` is 93KB larger in Agrad (completely different message)
- `yes.mp3` is 13KB smaller in Agrad (slightly different acknowledgment)
- Agrad has 2 extra files: `alo.mp3` and `repeat.mp3`

**Implication**: Cannot use a single `assets/audio/src/` directory. Need:
```
assets/audio/salehi/src/
assets/audio/agrad/src/
```

---

### Salehi Branch Has Immediate Disconnect Logic

Salehi branch has explicit disconnect code in `_handle_yes()` that was removed in Agrad. This confirms the core behavioral difference.

---

### Agrad Has Better Failure Detection

Agrad branch includes:
1. **Early SIP cause code detection** (don't wait for timeout)
2. **Better failure classification** (busy, banned, congestion vs generic "missed")
3. **Duplicate cleanup protection**
4. **Enhanced logging** for debugging

These are **bug fixes and improvements** that should be in BOTH scenarios!

---

### Agrad Is Tuned for Higher Volume

Rate limit configs in Agrad are 5-25x higher than Salehi. This might be:
- Production vs development environments
- Different use cases (high-volume vs low-volume campaigns)

---

## 4. Recommended Implementation Strategy

### Option A: Scenario-Specific Audio Directories (Recommended)

```
assets/
  audio/
    salehi/
      src/
        hello.mp3
        goodby.mp3
        yes.mp3
        number.mp3
        onhold.mp3
    agrad/
      src/
        hello.mp3
        goodby.mp3
        yes.mp3
        number.mp3
        onhold.mp3
        alo.mp3
        repeat.mp3
    wav/  # Shared output directory
```

**Config**:
```python
# In settings.py
audio_src_dir = f"assets/audio/{scenario_name}/src"
```

---

### Option B: Keep Current Structure, Copy Audio on Switch

Not recommended - error-prone and wastes disk space.

---

### Option C: Scenario-Specific Asterisk Sound Directories

```
/var/lib/asterisk/sounds/salehi/
/var/lib/asterisk/sounds/agrad/
```

**Config**:
```python
# In settings.py
ast_sound_dir = f"/var/lib/asterisk/sounds/{scenario_name}"
```

Play with:
```python
await self.ari.play_on_bridge(bridge_id, "sound:salehi/hello")
```

---

## 5. What Needs to Be Done

### Must Do (Critical)

1. ✅ Separate audio directories by scenario
2. ✅ Update audio sync to handle scenario-specific paths
3. ✅ Port Agrad improvements to both scenarios:
   - Early failure detection
   - Better failure classification
   - Duplicate cleanup protection
   - Enhanced logging
4. ✅ Update scenario config to include audio paths

### Should Do (Important)

1. ✅ Document rate limit differences
2. ✅ Make rate limits scenario-specific config (or at least document why they differ)
3. ✅ Port audio sync enhancements from Agrad

### Nice to Have

1. Port update.sh improvements
2. Include callcenter_agent.md in main branch

---

## 6. Proposed New ScenarioSettings

```python
@dataclass
class ScenarioSettings:
    """
    Scenario configuration to support different call flows.
    """
    name: str  # "salehi" or "agrad"
    transfer_to_operator: bool  # Transfer YES intents to operator
    audio_src_dir: str  # Scenario-specific audio source directory
    audio_files: dict  # Map of prompt_key to filename (in case names differ)

    # Optional scenario-specific rate limits
    max_concurrent_calls: Optional[int] = None
    max_calls_per_minute: Optional[int] = None
```

---

## 7. Migration Impact

### Breaking Changes

❌ **Cannot just set SCENARIO env var** - audio files are incompatible!

### Required Steps

1. Reorganize audio directories
2. Update audio sync script
3. Update marketing scenario to load scenario-specific audio
4. Port Agrad bug fixes to both scenarios
5. Test BOTH scenarios thoroughly

---

## Summary

The two branches differ in:

1. **Audio content** (CRITICAL - different files!)
2. **Call flow logic** (YES → disconnect vs transfer)
3. **Failure detection** (Agrad has better detection)
4. **Cleanup logic** (Agrad has bug fix)
5. **Rate limits** (Agrad 5-25x higher)
6. **Operator config** (Agrad uses panel agents)
7. **Logging** (Agrad more verbose)

**Next Steps**:
- Implement scenario-specific audio directory structure
- Port Agrad improvements to both scenarios
- Update configuration system to handle all differences
