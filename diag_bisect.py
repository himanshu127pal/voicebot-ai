"""
diag_bisect.py — find EXACTLY what makes the API say "Schema is too complex."

Run on the server:  ./.venv/bin/python diag_bisect.py

Sends the agent's REAL tool schemas in proper Anthropic format (previous probe sent
OpenAI shape and failed on format, not complexity). Bisects tool count, then tests
variants to isolate the trigger: property `title` keys, description length, and the
system prompt.
"""
import os, sys, json, copy, asyncio
from dotenv import load_dotenv
load_dotenv()
MODEL = os.getenv("CLAUDE_MODEL", "claude-haiku-4-5")
sys.argv = [sys.argv[0]]
import agent_smarthealth_claude as A
from livekit.agents.llm import utils as lkutils

agent = A.SmartHealthAgent({"name":"Basant","phone":"0400000000","campaign":"d",
                            "call_id":"d","state":None,"postcode":None})

# OpenAI shape -> Anthropic shape (what the plugin ultimately posts)
def to_anthropic(t):
    f = t["function"] if "function" in t else t
    return {"name": f["name"], "description": f.get("description",""),
            "input_schema": f["parameters"]}

tools = [to_anthropic(lkutils.build_legacy_openai_schema(t)) for t in agent.tools]
print(f"{len(tools)} tools, {len(json.dumps(tools))} chars total\n")

def strip_titles(ts):
    ts = copy.deepcopy(ts)
    for t in ts:
        t["input_schema"].pop("title", None)
        for p in t["input_schema"].get("properties", {}).values():
            p.pop("title", None)
    return ts

def short_desc(ts, n=60):
    ts = copy.deepcopy(ts)
    for t in ts:
        t["description"] = t["description"][:n]
    return ts

async def send(tools_, system=None, label=""):
    from anthropic import AsyncClient
    c = AsyncClient()
    kw = dict(model=MODEL, max_tokens=16, messages=[{"role":"user","content":"hi"}])
    if tools_: kw["tools"] = tools_
    if system: kw["system"] = system
    try:
        await c.messages.create(**kw)
        print(f"  OK    {label}")
        return True
    except Exception as e:
        msg = str(e)
        short = "TOO COMPLEX" if "too complex" in msg else msg[:120]
        print(f"  FAIL  {label}  -> {short}")
        return False

async def main():
    print("--- 1. bisect tool count (real schema) ---")
    limit = None
    for n in range(1, len(tools)+1):
        ok = await send(tools[:n], label=f"first {n} tools ({len(json.dumps(tools[:n]))} chars)")
        if not ok:
            limit = n
            print(f"     ^ broke when adding: {tools[n-1]['name']}")
            break
    if limit is None:
        print("     all 9 tools accepted -> tools alone are NOT the trigger")

    print("\n--- 2. variants on the FULL 9-tool set ---")
    await send(tools,                 label="full (baseline)")
    await send(strip_titles(tools),   label="titles stripped")
    await send(short_desc(tools),     label="descriptions truncated to 60 chars")
    await send(strip_titles(short_desc(tools)), label="titles stripped + short desc")

    print("\n--- 3. does the system prompt matter? ---")
    sp = agent.instructions
    await send(tools, system=sp, label=f"full tools + real system prompt ({len(sp)} chars)")
    await send(None,  system=sp, label="system prompt, NO tools")

    print("\n--- 4. single worst tool alone ---")
    big = max(tools, key=lambda t: len(json.dumps(t)))
    await send([big], label=f"{big['name']} alone ({len(json.dumps(big))} chars)")
asyncio.run(main())
