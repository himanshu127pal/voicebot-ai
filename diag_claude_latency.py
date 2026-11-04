"""
diag_claude_latency.py — measure Claude time-to-first-token with THIS agent's payload.

Run on the server, in the same venv, with no phone call involved:
    ./.venv/bin/python diag_claude_latency.py

Answers one question: is the silence caused by the Anthropic request being slow for our
payload, and if so, is it the system prompt or the tool schemas? It sends the real
system prompt from agent_smarthealth_claude.py and prints time-to-FIRST-TOKEN (the only
latency that matters on a call — TTS cannot start before it) for three payloads:
    1. tiny prompt, no tools   -> baseline network + model speed
    2. real system prompt, no tools
    3. real system prompt + tool schemas approximating the agent's 9 tools
Compare the three: a jump at step 2 means the prompt, a jump at step 3 means the tools.
"""
import os, sys, time, asyncio
from dotenv import load_dotenv
load_dotenv()

MODEL = os.getenv("CLAUDE_MODEL", "claude-haiku-4-5")

def real_system_prompt() -> str:
    """Pull the actual instructions the agent builds, so we measure the real payload."""
    sys.argv = [sys.argv[0]]                      # stop the livekit CLI parsing our args
    import agent_smarthealth_claude as A
    agent = A.SmartHealthAgent({"name": "Basant", "phone": "0400000000",
                                "campaign": "diag", "call_id": "diag",
                                "state": None, "postcode": None})
    return agent.instructions

def approx_tools():
    """Stand-ins for the agent's 9 tools — same shape (all params REQUIRED, no unions)."""
    def t(name, props, desc="tool"):
        return {"name": name, "description": desc,
                "input_schema": {"type": "object",
                                 "properties": {p: {"type": "string"} for p in props},
                                 "required": list(props)}}
    return [
        t("save_qualification", ["insurer","cover_type","premium_amount","premium_frequency",
                                 "hospital_extras","permanent_resident","main_interest",
                                 "full_name","email","postcode"]),
        t("record_consent", []), t("request_handoff", ["remarks"]),
        t("book_callback", ["day","time_of_day","remarks"]),
        t("disqualify", ["reason","remarks"]), t("register_dnc", ["remarks"]),
        t("set_disposition", ["outcome","remarks","decline_reason"]),
        t("voicemail_detected", []), t("end_call", []),
    ]

async def measure(label, system, tools):
    from anthropic import AsyncClient
    client = AsyncClient()
    t0 = time.perf_counter(); first = None; text = ""
    try:
        kwargs = dict(model=MODEL, max_tokens=256, system=system,
                      messages=[{"role": "user", "content": "Hello?"}])
        if tools:
            kwargs["tools"] = tools
        async with client.messages.stream(**kwargs) as stream:
            async for chunk in stream.text_stream:
                if first is None:
                    first = time.perf_counter() - t0
                text += chunk
        total = time.perf_counter() - t0
        print(f"  {label:34} TTFT={first if first else float('nan'):6.2f}s  "
              f"total={total:6.2f}s  reply={text[:60]!r}")
    except Exception as e:
        print(f"  {label:34} FAILED after {time.perf_counter()-t0:.2f}s: "
              f"{type(e).__name__}: {str(e)[:200]}")

async def main():
    if not os.getenv("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY not set"); return
    print(f"model={MODEL}\n")
    sp = real_system_prompt()
    print(f"real system prompt = {len(sp)} chars (~{len(sp)//4} tokens)\n")
    print("time-to-first-token (TTS cannot start before this):")
    await measure("1. tiny prompt, no tools", "You are a helpful assistant.", None)
    await measure("2. REAL system prompt, no tools", sp, None)
    await measure("3. REAL system prompt + 9 tools", sp, approx_tools())
    print("\nOn a call anything over ~1.5s reads as dead air; over ~3s callers hang up.")

asyncio.run(main())
