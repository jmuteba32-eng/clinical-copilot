"""Week 2 multi-agent graph: supervisor + intake-extractor + evidence-retriever.

The supervisor is NOT a black box. It owns every routing decision, and each
decision plus each worker handoff is appended to state["handoffs"] and emitted
as a structured log line carrying the correlation id. A full run is therefore
reconstructable after the fact: which worker ran, in what order, and why the
supervisor chose it.

Flow (workers always return to the supervisor -- it decides what is next):

    START -> supervisor -> refuse                     -> END   (out of scope)
                        -> intake_extractor  -> supervisor
                        -> evidence_retriever -> supervisor
                        -> answer                     -> END

Routing rules are explicit and inspectable rather than model-chosen: the
supervisor asks "what is still missing to answer safely?" and dispatches the
worker that fills that gap. Off-scope questions are refused BEFORE any worker
runs -- no extraction, no retrieval, no LLM call.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Annotated, Any, Literal, Optional, TypedDict

from langgraph.graph import END, START, StateGraph

logger = logging.getLogger("agent.graph")


def _append(left: list, right: list) -> list:
    return (left or []) + (right or [])


class CoPilotState(TypedDict, total=False):
    question: str
    correlation_id: str
    scope: str
    intake: dict
    intake_summary: str
    evidence: list
    citations: list
    answer_text: str
    refusal_reason: Optional[str]
    handoffs: Annotated[list, _append]


def _log_handoff(state: CoPilotState, frm: str, to: str, why: str) -> list:
    cid = state.get("correlation_id", "-")
    logger.info(
        "worker_handoff",
        extra={"event": "worker_handoff", "correlation_id": cid, "handoff_from": frm, "handoff_to": to, "reason": why},
    )
    return [{"from": frm, "to": to, "reason": why}]


def supervisor(state: CoPilotState) -> dict:
    """Owns routing. Decides the next worker from what state still lacks."""
    if "scope" not in state:
        from agent.pipeline.answer import classify_scope
        scope = classify_scope(state["question"])
        nxt = "refuse" if scope == "out_of_scope" else "intake_extractor"
        why = f"classified scope={scope}"
        return {"scope": scope, "handoffs": _log_handoff(state, "supervisor", nxt, why)}
    if state["scope"] == "out_of_scope":
        return {"handoffs": _log_handoff(state, "supervisor", "refuse", "scope=out_of_scope")}
    if "intake" not in state:
        return {"handoffs": _log_handoff(state, "supervisor", "intake_extractor", "patient facts not yet loaded")}
    if "evidence" not in state:
        return {"handoffs": _log_handoff(state, "supervisor", "evidence_retriever", "no guideline evidence yet")}
    return {"handoffs": _log_handoff(state, "supervisor", "answer", "facts + evidence present; ready to answer")}


def _route(state: CoPilotState) -> Literal["refuse", "intake_extractor", "evidence_retriever", "answer"]:
    if state.get("scope") == "out_of_scope":
        return "refuse"
    if "intake" not in state:
        return "intake_extractor"
    if "evidence" not in state:
        return "evidence_retriever"
    return "answer"


def intake_extractor(state: CoPilotState) -> dict:
    """WORKER: supplies the patient's extracted intake facts."""
    from agent.pipeline.respond import _intake_summary, _load_intake
    intake = _load_intake()
    return {
        "intake": intake,
        "intake_summary": _intake_summary(intake),
        "handoffs": _log_handoff(state, "intake_extractor", "supervisor", "intake facts loaded"),
    }


def evidence_retriever(state: CoPilotState) -> dict:
    """WORKER: supplies guideline evidence for the question."""
    from agent.pipeline.answer import Citation
    from agent.rag.retrieve import retrieve_evidence
    intake = state.get("intake", {})
    query = " ".join(
        p for p in (state["question"], intake.get("chief_concern"), intake.get("concern_symptoms")) if p
    )
    hits = retrieve_evidence(query, k=3)
    return {
        "evidence": [
            {"file": h.chunk.guideline_file, "page": h.chunk.page, "text": h.chunk.text} for h in hits
        ],
        "citations": [Citation(**h.citation()).model_dump() for h in hits],
        "handoffs": _log_handoff(state, "evidence_retriever", "supervisor", f"retrieved {len(hits)} chunks"),
    }


def refuse(state: CoPilotState) -> dict:
    return {"handoffs": _log_handoff(state, "refuse", "END", "returned out-of-scope refusal")}


def answer(state: CoPilotState) -> dict:
    """WORKER-adjacent node: composes the grounded answer from facts + evidence.

    Grounding is enforced in the prompt (answer ONLY from the supplied intake
    summary and evidence; say so plainly when evidence does not support an
    answer). The verified/citation invariant is applied after the graph returns,
    in answer_question() -- see agent/pipeline/answer.py.
    """
    from anthropic import Anthropic

    evidence = state.get("evidence", [])
    evidence_block = "\n\n".join(
        f"[{i+1}] {e['file']} p{e['page']}: {e['text']}" for i, e in enumerate(evidence)
    )
    prompt = (
        "You are a clinical co-pilot preparing a physician for a follow-up visit. "
        "Answer the question using ONLY the patient intake summary and the guideline "
        "evidence below. Do not invent facts. If the evidence does not support an "
        "answer, say so plainly. Cite guideline claims by their [n] marker.\n\n"
        f"PATIENT INTAKE SUMMARY:\n{state.get('intake_summary', '')}\n\n"
        f"GUIDELINE EVIDENCE:\n{evidence_block}\n\n"
        f"PHYSICIAN QUESTION: {state['question']}"
    )
    client = Anthropic()
    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=700,
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    return {
        "answer_text": text,
        "handoffs": _log_handoff(state, "answer", "END", "grounded answer composed"),
    }


def build_graph():
    g = StateGraph(CoPilotState)
    g.add_node("supervisor", supervisor)
    g.add_node("intake_extractor", intake_extractor)
    g.add_node("evidence_retriever", evidence_retriever)
    g.add_node("refuse", refuse)
    g.add_node("answer", answer)
    g.add_edge(START, "supervisor")
    g.add_conditional_edges("supervisor", _route, {
        "refuse": "refuse",
        "intake_extractor": "intake_extractor",
        "evidence_retriever": "evidence_retriever",
        "answer": "answer",
    })
    g.add_edge("intake_extractor", "supervisor")
    g.add_edge("evidence_retriever", "supervisor")
    g.add_edge("refuse", END)
    g.add_edge("answer", END)
    return g.compile()
