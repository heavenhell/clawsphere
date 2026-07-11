from __future__ import annotations

import json
from pathlib import Path

from backend.agent.copilot import run_copilot
from eval.test_cases import TEST_CASES


def run_eval() -> dict:
    results = []
    for case in TEST_CASES:
        result = run_copilot(case["message"], ["readonly"], case.get("history", []))
        tools = [call["tool_name"] for call in result.get("tool_calls", [])]
        answer = result.get("answer") or ""
        ok = result.get("intent") == case["intent"]
        ok = ok and all(tool in tools for tool in case.get("must_tools", []))
        ok = ok and all(text in answer for text in case.get("must_contain", []))
        results.append({
            "name": case["name"],
            "passed": ok,
            "intent": result.get("intent"),
            "tools": tools,
            "answer": answer[:200],
        })
    passed = sum(1 for item in results if item["passed"])
    return {"passed": passed, "total": len(results), "pass_rate": passed / len(results), "results": results}


if __name__ == "__main__":
    report = run_eval()
    out = Path(__file__).resolve().parent / "report.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
