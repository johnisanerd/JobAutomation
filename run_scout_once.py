"""
One-shot runner for Agent 1 — runs immediately (no scheduler).
Usage:
  python3 run_scout_once.py
"""
import asyncio, os, sys
sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv
load_dotenv()

from agents.agent1_scout import run_scout

if __name__ == "__main__":
    asyncio.run(run_scout())
