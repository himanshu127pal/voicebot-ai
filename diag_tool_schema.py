"""
diag_tool_schema.py — dump the EXACT tool schema LiveKit sends to Anthropic.

Run on the server, same venv, no phone call:
    ./.venv/bin/python diag_tool_schema.py

Why: a hand-written 9-tool schema with all-required string params is accepted by the
API in 0.75s, but the real agent gets 400 "Schema is too complex." So LiveKit's
generated schema differs from the naive one. This prints it, sizes it, and then
bisects — sending 1 tool, then 2, ... — to find exactly where the API tips over.
"""
import os, sys, json, asyncio
from dotenv import load_dotenv
load_dotenv()

MODEL = os.getenv("CLAUDE_MODEL", "claude-haiku-4-5")
sys.argv = [sys.argv[0]]
import agent_smarthealth_claude as A

# ── 0. Is the deployed file actually the updated one? ────────────────────────
import inspect
sig = inspect.signature(A.SmartHealthAgent.save_qualification)
opt = [p.name for p in sig.parameters.values()
       if p.default is not inspect.Parameter.empty]
print(f"save_qualification optional params on THIS file: {len(opt)} {opt}")
print("  -> expected 0 if the latest file is deployed\n")

agent = A.SmartHealthAgent({"name": "Basant", "phone": "0400000000",
                            "campaign": "diag", "call_id": "diag",
                            "state": None, "postcode": None})

# ── 1. Get the tool list, then convert with the anthropic plugin's own helper ──
tools = list(agent.tools)
print(f"agent exposes {len(tools)} tools\n")

converted = None
from livekit.plugins import anthropic as ap
for modname in ("utils", "llm"):
    mod = getattr(ap, modname, None)
    if mod is None:
        continue
    for fname in ("to_fnc_ctx", "_to_fnc_ctx", "to_anthropic_tools", "build_tools"):
        fn = getattr(mod, fname, None)
        if callable(fn):
            try:
                converted = fn(tools)
                print(f"converted via livekit.plugins.anthropic.{modname}.{fname}()\n")
                break
            except Exception as e:
                print(f"  {modname}.{fname} -> {type(e).__name__}: {e}")
    if converted:
        break

if converted is None:
    # Fall back to the framework-neutral schema so we can still see the shape.
    from livekit.agents.llm import utils as lkutils
    for fname in ("build_legacy_openai_schema", "build_strict_openai_schema"):
        fn = getattr(lkutils, fname, None)
        if callable(fn):
            converted = [fn(t) for t in tools]
            print(f"converted via livekit.agents.llm.utils.{fname}() (OpenAI shape)\n")
            break

if converted is None:
    print("could not convert; available names:")
    print("  anthropic.utils:", [n for n in dir(getattr(ap,'utils',object)) if 'fnc' in n or 'tool' in n.lower()])
    print("  anthropic.llm  :", [n for n in dir(getattr(ap,'llm',object))   if 'fnc' in n or 'tool' in n.lower()])
    sys.exit(1)

blob = json.dumps(converted)
print(f"TOTAL serialised tool schema = {len(blob)} chars")
print(f"anyOf occurrences = {blob.count('anyOf')}   $ref = {blob.count('$ref')}   "
      f"$defs = {blob.count('$defs')}   null = {blob.count('null')}\n")
for t in converted:
    s = json.dumps(t)
    name = t.get("name") or t.get("function", {}).get("name", "?")
    print(f"  {name:22} {len(s):6} chars  anyOf={s.count('anyOf')}  $ref={s.count('$ref')}")

print("\n--- FULL SCHEMA OF THE BIGGEST TOOL ---")
biggest = max(converted, key=lambda t: len(json.dumps(t)))
print(json.dumps(biggest, indent=2)[:4000])

# ── 2. Bisect: how many of these tools does the API accept? ──────────────────
async def probe():
    from anthropic import AsyncClient
    c = AsyncClient()
    print("\n--- how many tools does the API accept? ---")
    for n in range(1, len(converted) + 1):
        subset = converted[:n]
        try:
            await c.messages.create(model=MODEL, max_tokens=16,
                                    messages=[{"role":"user","content":"hi"}],
                                    tools=subset)
            print(f"  first {n} tools -> OK")
        except Exception as e:
            msg = str(e)[:160]
            names = [t.get('name') for t in subset]
            print(f"  first {n} tools -> FAIL  {msg}")
            print(f"     tools were: {names}")
            break
asyncio.run(probe())
