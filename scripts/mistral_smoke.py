"""Smoke-test Mistral wiring: chat, structured decision, TTS, STT.

Run: uv run python scripts/mistral_smoke.py
Requires MISTRAL_API_KEY in the environment.
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.platform import llm


def main() -> None:
    print("1. chat ...")
    msg = llm.chat([{"role": "user", "content": "Reply with exactly: OK"}])
    print("   ->", repr(msg.content))

    print("2. structured decision ...")
    d = llm.decide([
        {"role": "system", "content": "You are a test agent. Return the decision JSON."},
        {"role": "user", "content": "No records yet; call the provider tomorrow."},
    ])
    print("   ->", d.model_dump())

    print("3. TTS ...")
    audio = llm.synthesize("Hello, this is a quick smoke test.")
    print(f"   -> {len(audio)} bytes, head={audio[:4]!r}")

    print("4. STT ...")
    text = llm.transcribe(audio)
    print("   ->", repr(text))

    print("\nSMOKE OK")


if __name__ == "__main__":
    main()
