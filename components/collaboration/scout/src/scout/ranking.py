"""Deterministic triage, not a trust score or an instruction-following model."""

from datetime import datetime
import re


RULES = (
    ("help_request", 4, r"\b(?:help wanted|help needed|need help|needs help|looking for help|can (?:anyone|someone) help|request for help|seeking help)\b|助けて|支援募集|協力者募集|手伝って"),
    ("collaboration", 4, r"\b(?:looking for (?:collaborators|partners)|seeking (?:collaborators|partners)|collaborat(?:e|ion|ors)|pair programming|co-build)\b|共同開発|コラボ|協業"),
    ("contribution", 3, r"\b(?:good first issue|contributors? welcome|review (?:wanted|needed|requested)|needs? (?:a )?review|testers? wanted|bug report|pull request)\b|レビュー募集|レビュー依頼|テスター募集|バグ報告"),
    ("project_activity", 1, r"\b(?:released?|changelog|benchmark|security advisory|breaking change|migration guide)\b|リリース|変更履歴|脆弱性報告|ベンチマーク"),
)
AGENT = re.compile(r"\b(?:agent|bot)\b|エージェント", re.I)
CAPABILITY = re.compile(r"\b(?:built|released?|supports?|provides?|can (?:detect|monitor|review|test|search)|open.source|sdk|tool)\b|開発しました|公開しました|監視|検出|提供", re.I)
INSTRUCTION_HINT = re.compile(
    r"ignore.{0,40}(?:instructions|rules)|system prompt|\bsudo\b|\bcurl\b|\bexecute\b|private key|seed phrase|秘密鍵|命令を無視|実行して", re.I)


def classify(text):
    reasons = []
    for category, weight, pattern in RULES:
        match = re.search(pattern, text, re.I)
        if match:
            reasons.append({"category": category, "weight": weight, "matched_text": match.group()})
    agent, capability = AGENT.search(text), CAPABILITY.search(text)
    if agent and capability:
        reasons.append({"category": "useful_agent", "weight": 2,
                        "matched_text": agent.group(), "capability_signal": capability.group()})
    return reasons


def rank(observations, keywords=(), top=20):
    grouped = {}
    scanned = 0
    for observation in observations:
        for index, message in enumerate(observation.data["messages"]):
            scanned += 1
            text = message["text"]
            if keywords and not any(word.casefold() in text.casefold() for word in keywords):
                continue
            reasons = classify(text)
            if not reasons:
                continue
            # Duplicate announcements do not gain rank through repetition.
            key = (message["from"], " ".join(text.casefold().split()))
            evidence = {
                "room": observation.room, "seq": message["seq"],
                "generation": observation.data.get("generation"),
                "message_ts": message["ts"], "fetched_at": observation.fetched_at,
                "source_file": f"source-{observation.room}.json",
                "json_pointer": f"/messages/{index}",
            }
            if key in grouped:
                grouped[key]["evidence"].append(evidence)
                continue
            grouped[key] = {
                "author_claim": message["from"], "identity_verified": False,
                "text": text, "assessment": "INFERENCE / HUMAN_REVIEW_REQUIRED",
                "score": sum(reason["weight"] for reason in reasons),
                "reasons": reasons,
                "instruction_like_text": bool(INSTRUCTION_HINT.search(text)),
                "evidence": [evidence],
            }
    candidates = list(grouped.values())
    candidates.sort(key=lambda item: (
        -item["score"],
        -max(datetime.fromisoformat(e["message_ts"].replace("Z", "+00:00")).timestamp()
             for e in item["evidence"]),
        item["author_claim"], item["text"],
    ))
    return {"messages_scanned": scanned, "candidates_total": len(candidates),
            "candidates_shown": min(len(candidates), top), "candidates": candidates[:top]}
