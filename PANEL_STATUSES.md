# Complete Panel Status Reference

**Date**: 2026-01-06
**Purpose**: Complete documentation of all panel result statuses, when they occur, and scenario differences

---

## Panel Status Overview

The system maps internal result values to standardized panel statuses. This document provides a complete reference of all possible outcomes.

---

## Status Mapping Table

| Internal Result | Panel Status | When It Occurs | Includes Transcript | Scenario Difference |
|-----------------|--------------|----------------|---------------------|---------------------|
| `connected_to_operator` | **CONNECTED** | User said YES | ✅ Yes | **Both scenarios** - Salehi: no transfer, Agrad: with operator |
| `not_interested` | **NOT_INTERESTED** | User said NO / declined offer | ✅ Yes | Both scenarios |
| `missed` | **MISSED** | No answer / timeout / unreachable | ❌ No | Both scenarios |
| `user_didnt_answer` | **MISSED** | Dialer timeout (no events received) | ❌ No | Both scenarios |
| `hangup` | **HANGUP** | User hung up during call | ❌ No | Both scenarios |
| `disconnected` | **DISCONNECTED** | Operator transfer failed (Agrad only) | ✅ Yes | **Agrad only** - Salehi uses connected_to_operator |
| `unknown` | **UNKNOWN** | Unclear intent / LLM couldn't classify | ✅ Yes | Both scenarios |
| `failed:stt_failure` | **NOT_INTERESTED** | STT couldn't transcribe (treated as no response) | ❌ No | Both scenarios |
| `failed:*` | **FAILED** | Technical failure (recording, LLM, etc.) | ❌ No | Both scenarios |
| `busy` | **BUSY** | Line busy (SIP cause 17) | ❌ No | Both scenarios |
| `power_off` | **POWER_OFF** | Unreachable (SIP cause 18/19/20) | ❌ No | Both scenarios |
| `banned` | **BANNED** | Rejected (SIP cause 21/34/41/42) | ❌ No | Both scenarios |

---

## Detailed Status Descriptions

### 1. CONNECTED (Both Scenarios)

**Internal Result**: `connected_to_operator`
**Panel Status**: `CONNECTED`
**Reason**: Scenario-dependent
**Transcript**: ✅ Included (user's response)

**When It Occurs**:

#### Salehi Scenario:
- User says YES (intent classified as "yes")
- "yes" prompt plays successfully
- Result set to `connected_to_operator`
- Call disconnects immediately (no operator transfer)
- **This is the success outcome for Salehi**

**Code Location**: [logic/marketing_outreach.py:257](logic/marketing_outreach.py#L257)

**Call Flow (Salehi)**:
```
hello → record → alo → classify
  └─ YES intent
      └─ play "yes"
          └─ set result: "connected_to_operator"
              └─ disconnect
                  └─ Panel: "CONNECTED" ✅ (Success!)
```

#### Agrad Scenario:
- User says YES (intent classified as "yes")
- "yes" prompt plays successfully
- "onhold" prompt plays successfully
- Operator leg is originated
- Operator answers the call
- Call is bridged successfully
- Result set to `connected_to_operator`
- **This is the success outcome for Agrad**

**Code Location**: [logic/marketing_outreach.py:222](logic/marketing_outreach.py#L222)

**Call Flow (Agrad)**:
```
hello → record → alo → classify
  └─ YES intent
      └─ play "yes"
          └─ play "onhold"
              └─ originate operator
                  └─ operator answers
                      └─ bridge call
                          └─ result: "connected_to_operator"
                              └─ Panel: "CONNECTED" ✅ (Success!)
```

**Scenario Difference**:
- **Salehi**: Result set immediately after YES, no operator transfer
- **Agrad**: Result set after operator answers, with full bridge

---

### 2. NOT_INTERESTED

**Internal Result**: `not_interested`
**Panel Status**: `NOT_INTERESTED`
**Reason**: "User declined"
**Transcript**: ✅ Included (user's response)

**When It Occurs**:
- User says NO (intent classified as "no")
- "goodby" prompt plays
- Call hangs up

**Code Location**: [logic/marketing_outreach.py:711](logic/marketing_outreach.py#L711)

**Scenario Difference**: Identical in both scenarios

**Call Flow**:
```
hello → record → alo → classify
  └─ NO intent
      └─ play "goodby"
          └─ hangup
              └─ result: "not_interested"
```

---

### 3. MISSED

**Internal Result**: `missed` or `user_didnt_answer`
**Panel Status**: `MISSED`
**Reason**: "No answer/busy/unreachable"
**Transcript**: ❌ Not included

**When It Occurs**:

#### Scenario A: Customer never answered
- Outbound call originated
- Customer didn't pick up (ring timeout)
- No StasisStart event received
- Dialer watchdog timeout triggered

**Code Location**: [logic/marketing_outreach.py:745](logic/marketing_outreach.py#L745), [logic/dialer.py:338](logic/dialer.py#L338)

#### Scenario B: Early failure detection
- SIP cause code detected during Progress/Ringing state
- Non-busy failure codes trigger "missed"

**Code Location**: [sessions/session_manager.py:287-300](sessions/session_manager.py#L287-L300)

**Scenario Difference**: Identical in both scenarios

**Call Flow**:
```
Originate call
  └─ Ring... Ring... Ring...
      └─ Timeout (no answer)
          └─ result: "missed" or "user_didnt_answer"
```

---

### 4. HANGUP

**Internal Result**: `hangup`
**Panel Status**: `HANGUP`
**Reason**: "Caller hung up"
**Transcript**: ❌ Not included

**When It Occurs**:

#### Customer hung up during prompts
- Call was answered
- Customer disconnected before completing flow
- No recording was captured yet

**Code Locations**:
- [logic/marketing_outreach.py:488](logic/marketing_outreach.py#L488) - During hello playback
- [logic/marketing_outreach.py:539](logic/marketing_outreach.py#L539) - During alo playback
- [logic/marketing_outreach.py:739](logic/marketing_outreach.py#L739) - After classify if empty response

**Scenario Difference**: Identical in both scenarios

**Call Flow**:
```
hello → (customer hangs up)
  └─ result: "hangup"
```

**Note**: STT failures are now treated as NOT_INTERESTED (see section below)

---

### 5. DISCONNECTED (Agrad Only)

**Internal Result**: `disconnected`
**Panel Status**: **DISCONNECTED**
**Reason**: "Caller said yes but disconnected before operator answered"
**Transcript**: ✅ Included (user's response)

**When It Occurs** (Agrad only):
- User says YES (intent classified as "yes")
- "yes" prompt plays successfully
- "onhold" prompt plays successfully
- Operator leg origination FAILS (no operators available, timeout, etc.)
- OR customer hangs up while waiting for operator
- **This is a FAILURE outcome for Agrad**

**Code Locations**:
- [logic/marketing_outreach.py:297](logic/marketing_outreach.py#L297) - on_call_failed with YES intent

**Call Flow (Agrad)**:
```
hello → record → alo → classify
  └─ YES intent
      └─ play "yes"
          └─ play "onhold"
              └─ originate operator
                  └─ OPERATOR FAILS or CUSTOMER HANGS UP
                      └─ result: "disconnected"
                          └─ Panel: "DISCONNECTED" ❌ (Failure)
```

**Scenario Difference**:
- **Salehi**: Does NOT use `disconnected` result (uses `connected_to_operator` instead)
- **Agrad**: Uses `disconnected` only when operator transfer fails after user says YES

---

### 6. UNKNOWN

**Internal Result**: `unknown`
**Panel Status**: `UNKNOWN`
**Reason**: "Unknown intent"
**Transcript**: ✅ Included (user's response)

**When It Occurs**:
- User's response was transcribed successfully
- LLM classified intent as "unknown"
- Response doesn't match yes/no/number patterns
- OR LLM fallback heuristic couldn't determine intent

**Code Location**: [logic/marketing_outreach.py:735](logic/marketing_outreach.py#L735)

**Scenario Difference**: Identical in both scenarios

**Call Flow**:
```
hello → record → alo → STT success
  └─ LLM classify
      └─ Intent: "unknown"
          └─ play "goodby"
              └─ hangup
                  └─ result: "unknown"
```

**Example Transcripts That Trigger UNKNOWN**:
- "چی؟" (What?)
- "کی هستی؟" (Who are you?)
- "بعدا زنگ بزنید" (Call later)
- Unclear/garbled speech

---

### 7. BUSY

**Internal Result**: `busy`
**Panel Status**: `BUSY`
**Reason**: "Line busy or rejected"
**Transcript**: ❌ Not included

**When It Occurs**:
- SIP cause code 17 (User Busy) detected
- OR cause_txt contains "busy"
- Detected during Progress/Ringing or Hangup

**Code Locations**:
- [sessions/session_manager.py:287-300](sessions/session_manager.py#L287-L300) - Early detection
- [logic/marketing_outreach.py:254-260](logic/marketing_outreach.py#L254-L260) - Failure handler

**Scenario Difference**: Identical in both scenarios

**SIP Cause Code**: 17

**Call Flow**:
```
Originate call
  └─ SIP Response: 486 Busy Here (cause=17)
      └─ Early failure detection
          └─ result: "busy"
```

---

### 8. POWER_OFF

**Internal Result**: `power_off`
**Panel Status**: `POWER_OFF`
**Reason**: "Unavailable / powered off / no response"
**Transcript**: ❌ Not included

**When It Occurs**:
- SIP cause codes 18, 19, or 20 detected
  - 18: No User Responding
  - 19: No Answer from User
  - 20: Subscriber Absent

**Code Locations**:
- [sessions/session_manager.py:287-300](sessions/session_manager.py#L287-L300) - Early detection
- [logic/marketing_outreach.py:254-260](logic/marketing_outreach.py#L254-L260) - Failure handler

**Scenario Difference**: Identical in both scenarios

**SIP Cause Codes**: 18, 19, 20

**Call Flow**:
```
Originate call
  └─ SIP Response: 480/404 (cause=18/19/20)
      └─ Early failure detection
          └─ result: "power_off"
```

---

### 9. BANNED

**Internal Result**: `banned`
**Panel Status**: `BANNED`
**Reason**: "Rejected by operator"
**Transcript**: ❌ Not included

**When It Occurs**:
- SIP cause codes 21, 34, 41, or 42 detected
  - 21: Call Rejected
  - 34: No Circuit Available
  - 41: Temporary Failure
  - 42: Congestion
- OR cause_txt contains "congest" or "failed"

**Code Locations**:
- [sessions/session_manager.py:287-300](sessions/session_manager.py#L287-L300) - Early detection
- [logic/marketing_outreach.py:254-260](logic/marketing_outreach.py#L254-L260) - Failure handler

**Scenario Difference**: Identical in both scenarios

**SIP Cause Codes**: 21, 34, 41, 42

**Call Flow**:
```
Originate call
  └─ SIP Response: 603/488 (cause=21/34/41/42)
      └─ Early failure detection
          └─ result: "banned"
```

---

### 10. FAILED

**Internal Result**: `failed` or `failed:<reason>`
**Panel Status**: `FAILED`
**Reason**: Various technical failures
**Transcript**: ❌ Not included

**When It Occurs**:

#### failed:recording
- Recording file missing after completion
- Recording failed to start
- Audio file not found

**Code Location**: [logic/marketing_outreach.py:743](logic/marketing_outreach.py#L743)

#### failed:llm_quota
- LLM API quota exceeded (HTTP 403)
- LLM service unavailable
- Dialer auto-pauses on this error
- SMS alert sent to admins
- Panel notified with FAILED status

**Code Location**: [logic/marketing_outreach.py:652](logic/marketing_outreach.py#L652)

#### failed:vira_quota
- Vira STT API quota exceeded (HTTP 403)
- Vira balance exhausted or credit below threshold
- Dialer auto-pauses on this error
- SMS alert sent to admins
- Panel notified with FAILED status
- **NEW**: Now detects 403 errors in addition to balance messages

**Code Location**: [logic/marketing_outreach.py:536-554](logic/marketing_outreach.py#L536-L554)

#### failed:operator_failed
- Operator leg failed to originate (Agrad only)
- **PRESERVED**: No longer overridden by "disconnected"
- Reported to panel as FAILED

**Code Location**: [logic/marketing_outreach.py:834](logic/marketing_outreach.py#L834)

#### failed:hangup
- Generic hangup failure
- Customer hung up during playback without clear reason

**Code Location**: [logic/marketing_outreach.py:331](logic/marketing_outreach.py#L331)

**Scenario Difference**: `failed:operator_failed` only occurs in Agrad (operator transfer)

**Call Flow Example**:
```
hello → record → recording file missing
  └─ result: "failed:recording"

OR

hello → record → STT success → LLM quota exceeded
  └─ result: "failed:llm_quota"
      └─ Dialer PAUSED
```

---

## Scenario-Specific Summary

### Salehi Scenario

**Call Flow**:
```
hello → record → alo → classify:
  ├─ YES → yes → set result: "connected_to_operator" → disconnect
  ├─ NO → goodby → hangup (result: "not_interested")
  ├─ NUMBER_QUESTION → number → record again → classify (loop)
  └─ UNKNOWN → goodby → hangup (result: "unknown")
```

**Possible Results**:
- ✅ **connected_to_operator** → Panel: **CONNECTED** - Success! User said yes
- ❌ not_interested - User declined
- ❌ unknown - Unclear response
- ❌ hangup - User hung up
- ❌ missed/user_didnt_answer - No answer
- ❌ busy/power_off/banned - SIP failures
- ❌ failed:* - Technical failures (vira_quota, llm_quota, recording, etc.)

**Key Point**: `connected_to_operator` internal result → Panel status **CONNECTED** = **success outcome** in Salehi (no operator transfer, immediate disconnect)

---

### Agrad Scenario

**Call Flow**:
```
hello → record → alo → classify:
  ├─ YES → yes → onhold → connect to operator
  │         └─ If operator answers: result "connected_to_operator"
  │         └─ If operator fails: result "disconnected"
  ├─ NO → goodby → hangup (result: "not_interested")
  └─ UNKNOWN → goodby → hangup (result: "unknown")
```

**Possible Results**:
- ✅ **connected_to_operator** - Success! User connected to agent
- ⚠️ disconnected - User said yes but operator failed/customer hung up
- ❌ not_interested - User declined
- ❌ unknown - Unclear response
- ❌ hangup - User hung up
- ❌ missed/user_didnt_answer - No answer
- ❌ busy/power_off/banned - SIP failures
- ❌ failed:* - Technical failures

**Key Point**: `connected_to_operator` is the **success outcome** in Agrad

---

## Transcript Inclusion Rules

User transcripts are **ONLY** included for these panel statuses:
- **CONNECTED** (Agrad only)
- **DISCONNECTED** (both scenarios)
- **NOT_INTERESTED** (both scenarios)
- **UNKNOWN** (both scenarios)

**Code Reference**: [logic/marketing_outreach.py:1068](logic/marketing_outreach.py#L1068)

```python
user_message=user_message if status in {"UNKNOWN", "DISCONNECTED", "CONNECTED", "NOT_INTERESTED"} else None
```

**Rationale**: These statuses represent cases where we captured and classified user intent, so the transcript is valuable for analysis.

---

## YES Prompt Behavior

**Question**: Does the "yes" prompt play in both scenarios after user says yes?

**Answer**: ✅ **YES** - The "yes" prompt plays in **BOTH** scenarios

**Code Location**: [logic/marketing_outreach.py:694](logic/marketing_outreach.py#L694)

```python
async def _handle_yes(self, session: Session) -> None:
    async with session.lock:
        session.metadata["intent_yes"] = "1"
        session.metadata["yes_at"] = str(time.time())
    await self._play_prompt(session, "yes")  # ← Plays in BOTH scenarios
    # Then scenario-specific handling via on_playback_finished
```

**After "yes" prompt finishes**:
- **Salehi**: Disconnects immediately (result: "disconnected")
- **Agrad**: Plays "onhold" and connects to operator

**Audio Files**:
- Salehi: `assets/audio/salehi/src/yes.mp3` (129KB)
- Agrad: `assets/audio/agrad/src/yes.mp3` (116KB)
- **Different content** (different sizes, different spoken messages)

---

## Duplicate Report Prevention

The system prevents duplicate reports with the same status:

**Code Location**: [logic/marketing_outreach.py:1053-1057](logic/marketing_outreach.py#L1053-L1057)

```python
async with session.lock:
    last_status = session.metadata.get("panel_last_status")
    if last_status == status:
        return  # Skip duplicate
    session.metadata["panel_last_status"] = status
```

This ensures the panel doesn't receive multiple reports with the same status for a single call.

---

## Summary Statistics

| Category | Count | Statuses |
|----------|-------|----------|
| **Success** | 1-2 | CONNECTED (Agrad), DISCONNECTED (Salehi success) |
| **User Actions** | 3 | NOT_INTERESTED, HANGUP, UNKNOWN |
| **No Answer** | 1 | MISSED |
| **SIP Failures** | 3 | BUSY, POWER_OFF, BANNED |
| **Technical** | 1 | FAILED |
| **Total** | 8 | 8 distinct panel statuses |

**Internal Result Values**: 13+ (including failed:* variants)

---

## Quick Reference: Result → Panel Status

```python
{
    "connected_to_operator": "CONNECTED",       # Agrad only
    "not_interested": "NOT_INTERESTED",         # Both
    "missed": "MISSED",                         # Both
    "user_didnt_answer": "MISSED",             # Both
    "hangup": "HANGUP",                        # Both
    "disconnected": "DISCONNECTED",            # Both (different meaning!)
    "unknown": "UNKNOWN",                      # Both
    "failed:stt_failure": "HANGUP",            # Both (treated as hangup)
    "failed:*": "FAILED",                      # Both
    "busy": "BUSY",                            # Both
    "power_off": "POWER_OFF",                  # Both
    "banned": "BANNED",                        # Both
}
```

---

**Last Updated**: 2026-01-06
**Applies To**: Salehi branch (scenario-based architecture)
**Documentation**: This file complements [CLAUDE.md](CLAUDE.md)
