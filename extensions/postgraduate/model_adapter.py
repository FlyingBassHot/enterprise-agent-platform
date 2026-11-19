"""OpenCode Go protocol adapter; forwards credentials without persisting them."""

from __future__ import annotations

import hashlib
import json

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
UPSTREAM = "https://opencode.ai/inference/go/openai/v1"


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/v1/models")
async def models():
    async with httpx.AsyncClient(timeout=20) as client:
        result = await client.get("https://opencode.ai/inference/go/v1/models")
        return Response(
            result.content, status_code=result.status_code, media_type="application/json"
        )


@app.post("/v1/chat/completions")
async def completions(request: Request):
    authorization = request.headers.get("authorization", "")
    if not authorization.startswith("Bearer "):
        return JSONResponse(
            {"error": {"message": "Bearer authorization required"}}, status_code=401
        )
    raw = await request.body()
    if len(raw) > 2 * 1024 * 1024:
        return JSONResponse({"error": {"message": "Request too large"}}, status_code=413)
    payload = json.loads(raw)
    messages = payload.get("messages", [])
    # The platform does not forward a conversation header. Use its stable
    # initial system/user prefix as the session anchor; no raw text in headers.
    anchor = [
        next((m.get("content") for m in messages if m.get("role") == role), "")
        for role in ("system", "user")
    ]
    session = hashlib.sha256(
        json.dumps(anchor, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()
    headers = {
        "Authorization": authorization,
        "User-Agent": "EnterpriseAgentPlatform/1.0",
        "x-opencode-session": session,
        "Accept-Encoding": "identity",
    }
    # Platform ChatMessage does not preserve provider-specific reasoning traces.
    # Disable thinking so subsequent tool turns do not need reasoning_content.
    payload["thinking"] = {"type": "disabled"}
    client = httpx.AsyncClient(timeout=httpx.Timeout(300, connect=15))
    try:
        upstream = await client.send(
            client.build_request(
                "POST", UPSTREAM + "/chat/completions", json=payload, headers=headers
            ),
            stream=True,
        )
    except httpx.HTTPError:
        await client.aclose()
        return JSONResponse(
            {"error": {"message": "OpenCode Go connection failed"}}, status_code=502
        )
    if not payload.get("stream") or upstream.status_code >= 400:
        try:
            body = await upstream.aread()
            return Response(
                body,
                status_code=upstream.status_code,
                media_type=upstream.headers.get("content-type", "application/json"),
            )
        finally:
            await upstream.aclose()
            await client.aclose()

    async def stream():
        try:
            async for chunk in upstream.aiter_bytes():
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    return StreamingResponse(
        stream(), status_code=upstream.status_code, media_type="text/event-stream"
    )


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8011, access_log=False)
