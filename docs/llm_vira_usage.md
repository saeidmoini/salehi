## GapGPT (LLM) usage
- Endpoint: `POST {GAPGPT_BASE_URL}/chat/completions` (OpenAI-compatible).
- Auth: `Authorization: Bearer <GAPGPT_API_KEY>`, `Content-Type: application/json`.
- Request: `{"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "Hi"}], "temperature": 0.2}`.
- Response: text in `choices[0].message.content`.
- Client: prefer `httpx.AsyncClient` with pooling (`Limits(max_connections=N, max_keepalive_connections=N)`), timeout ~20s.
- Concurrency: wrap calls in `asyncio.Semaphore` (e.g., `MAX_PARALLEL_LLM=10` from config).
- Error handling: log status/response on non-2xx; consider limited retries on 5xx/timeout with backoff.

### Minimal async snippet
```python
import asyncio, httpx

sem = asyncio.Semaphore(10)
async def chat(messages, base_url, api_key, timeout=20.0):
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    limits = httpx.Limits(max_connections=100, max_keepalive_connections=100)
    async with sem, httpx.AsyncClient(base_url=base_url, headers=headers, timeout=timeout, limits=limits) as c:
        r = await c.post("/chat/completions", json={"model": "gpt-4o-mini", "messages": messages, "temperature": 0.2})
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]
```

## Vira STT usage
- Endpoint: `POST {VIRA_STT_URL}`.
- Auth: header `gateway-token: <VIRA_STT_TOKEN>` (or `VIRA_TOKEN` fallback).
- Payload: multipart form with `audio` (`audio.wav`, `audio/wav`) plus fields:
  - `model=default`, `srt=false`, `inverseNormalizer=false`, `timestamp=false`,
    `spokenPunctuation=false`, `punctuation=false`, `numSpeakers=0`, `diarize=false`,
    optional `hotwords[]`.
- Response: transcript in `data.text` (or nested `data.data.text` / `data.data.aiResponse.result.text`), status in `data.status`.
- Client: `httpx.AsyncClient` with pooling; timeout ~30s.
- Concurrency: `asyncio.Semaphore` (e.g., `MAX_PARALLEL_STT=50`).

### Minimal async snippet
```python
import httpx, asyncio

stt_sem = asyncio.Semaphore(50)
async def transcribe(audio_bytes, stt_url, token, timeout=30.0):
    headers = {"gateway-token": token, "accept": "application/json"}
    files = {"audio": ("audio.wav", audio_bytes, "audio/wav")}
    data = [
        ("model","default"),("srt","false"),("inverseNormalizer","false"),("timestamp","false"),
        ("spokenPunctuation","false"),("punctuation","false"),("numSpeakers","0"),("diarize","false")
    ]
    async with stt_sem, httpx.AsyncClient(timeout=timeout) as c:
        r = await c.post(stt_url, headers=headers, data=data, files=files)
        r.raise_for_status()
        payload = r.json()
    text = (
        payload.get("data", {}).get("text")
        or payload.get("data", {}).get("data", {}).get("text")
        or payload.get("data", {}).get("data", {}).get("aiResponse", {}).get("result", {}).get("text")
        or ""
    )
    return text
```

## Vira TTS usage
- Endpoint: `POST {VIRA_TTS_URL}`.
- Auth: header `gateway-token: <VIRA_TTS_TOKEN>` (or `VIRA_TOKEN`).
- Payload JSON: `{"text": "...", "speaker": "female", "speed": 1.0, "timestamp": false}`.
- Response: audio info in `data.url` / `data.filename`, status in `status`.
- Client: `httpx.AsyncClient`; timeout ~30s; protect with `asyncio.Semaphore` (e.g., `MAX_PARALLEL_TTS=50`).

## General best practices
- Pooling: configure `httpx.Limits` to cap concurrent sockets.
- Timeouts: per-service timeouts; fail fast on hangs.
- Retries: only on transient errors (5xx/timeout) with small backoff.
- Concurrency caps: semaphores per service family (STT/TTS/LLM) driven by config.
- Audio: send 16 kHz mono WAV for STT; ensure UTF-8 text for TTS/LLM.
- Logging: log status and response body on failure; distinguish empty-transcript cases.
- Config knobs (env): `GAPGPT_BASE_URL`, `GAPGPT_API_KEY`, `VIRA_STT_URL`, `VIRA_STT_TOKEN`, `VIRA_TTS_URL`, `VIRA_TTS_TOKEN`, `MAX_PARALLEL_STT`, `MAX_PARALLEL_TTS`, `MAX_PARALLEL_LLM`, `HTTP_MAX_CONNECTIONS`, `STT_TIMEOUT`, `TTS_TIMEOUT`, `LLM_TIMEOUT`.
