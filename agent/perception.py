"""Perception: the agent's orchestrator.

Runs every loop iteration. Looks at the user's original query, the memory
hits, and the run history so far, and emits the current Observation —
which goals exist, which are done, and whether the next unfinished goal
needs raw bytes from a specific artifact.

Perception never reads artifact bytes. It sees handles + descriptors only.
When a goal needs bytes, Perception sets `send_artifact: true` and points
`artifact_index` at one of the artifacts listed in MEMORY HITS. The outer
loop resolves the index back to the artifact id and attaches the bytes.

Session 7 note: memory hits are retrieved via FAISS vector similarity first
(falling back to keyword overlap). A hit marked `[vector]` was returned
because its embedding is semantically close to the query — it may be
relevant even if no keyword matches the query literally.
"""

from __future__ import annotations

import json

from pydantic import BaseModel, Field

from gateway import LLM, ensure_gateway
from schemas import Goal, MemoryItem, Observation, new_id


class _GoalDelta(BaseModel):
    """What the Perception LLM emits per goal. No `id` field — goals are
    identified by their position in the output list. The LLM cannot drift
    identity across iterations because there is no identity field to drift."""

    text: str = Field(max_length=240)
    done: bool = False
    send_artifact: bool = False
    artifact_index: int | None = None
    # For synthesis goals that need to read N fetched pages at once.
    # If non-empty, all listed indices are attached alongside artifact_index.
    artifact_indices: list[int] = Field(default_factory=list)


class _PerceptionOutput(BaseModel):
    goals: list[_GoalDelta] = Field(default_factory=list, max_length=10)


# ---------------------------------------------------------------------------
# System prompt — production-grade, 4-duty structure
# ---------------------------------------------------------------------------

_SYSTEM = """\
You are the PERCEPTION module of an autonomous AI agent. You run once per loop iteration.
Your sole output is an updated goal list (JSON). You do not execute tasks, call tools, or produce answers.

## Context you receive each iteration
- USER QUERY: the original request (never changes across iterations)
- PRIOR GOALS: the goal list from the previous iteration (empty on the first call)
- ARTIFACTS: a lookup table mapping artifact_index → which goal produced it
- MEMORY HITS: descriptors from long-term memory retrieved by vector similarity (marked [vector])
  or keyword overlap (marked [keyword]). Vector hits may be semantically relevant even when no
  keyword matches the query literally — treat them as high-confidence context.
- HISTORY: the last 10 agent actions and answers (chronological, newest last)

## Your four duties — execute in this exact order

### 1. DECOMPOSE  (only when PRIOR GOALS is empty)
Break the query into 1–5 atomic, independently-completable goals. Each goal must:
  - Start with an action verb: Fetch, Find, Extract, Convert, List, Answer, Query, Summarise, Remember, …
  - Be testable: you can verify from HISTORY or MEMORY HITS alone whether it is satisfied
  - Be written at the level of INTENT, not tool selection — describe WHAT, not WHICH tool

Decomposition rules:
  a) If the answer is already in MEMORY HITS (vector or keyword), use a SINGLE goal: "Answer <question>".
     Do NOT create sub-goals for things the memory already contains.
  b) If MEMORY HITS contain `fact` items whose descriptors start with `[sandbox:` or `[art:`
     (these mark previously-indexed chunks of source documents), the goal must be to
     QUERY THE EXISTING KNOWLEDGE BASE, not to re-fetch the original sources.
     Always pair a "Query knowledge base for …" goal with a final "Synthesise / Answer …" goal.
     Never emit a knowledge-base query as the only goal — the user needs an answer produced
     from the returned chunks.
  c) If the query asks to read/fetch/process N items ("top 3 results", "first 5 articles"),
     emit a SEPARATE fetch goal for each concrete item plus the final synthesis goal.
     Do NOT use a single umbrella goal.
  d) If the query asks to ingest N files so they can be searched later, emit one goal per file
     ("Make <file> searchable") plus a final report/confirmation goal.
  e) Whenever the query is a question (rather than a pure action like "save X" or "fetch Y"),
     the LAST goal in your decomposition MUST be a synthesis or answer goal using verbs like:
     answer, tell, summarise, compare, list, extract, identify, describe, explain, report.
  f) Do NOT split a simple factual lookup into multiple sub-goals.
     Bad: "Identify the capital of France" + "Inform the user of the capital"
     Good: "Answer what the capital of France is"
  g) If the query asks a high-level, conceptual, or comparative question across multiple documents or topics (e.g., "Across these papers, how do they...", "Compare how X and Y..."), do NOT split it into separate search/fetch goals for each individual document or topic. Emitting multiple narrow search goals will fragment the vector retrieval and degrade context. Instead, use a SINGLE search/query goal targeting the concepts across all topics (e.g., "Query the knowledge base for how these papers handle X"), followed by a final synthesis goal.

### 2. MARK DONE  (check every prior goal, every iteration)
For each goal in PRIOR GOALS, scan HISTORY for evidence of completion.
Set done=true when ANY of these is true:
  a) A HISTORY event with kind="answer" has a goal_id matching this goal → DONE
  b) A HISTORY event with kind="action" produced a result that fully satisfies
     a purely-fetch or purely-compute goal (e.g. list_dir completed for a "list directory" goal) → DONE

Hard constraints on marking done:
  - SYNTHESIS GOALS ARE NEVER DONE FROM A TOOL-CALL ALONE. A goal containing any of these words:
    answer, extract, summarise, summarize, compare, identify, explain, describe, list, report,
    evaluate, select, synthesise, synthesize, analyse, analyze, pick, choose, decide, recommend,
    find, determine, name, tell
    … is only done when a kind="answer" event exists in HISTORY with a matching goal_id.
    A kind="action" event (tool call) for a synthesis goal means the raw data was fetched,
    not that the synthesis is complete.
  - Once done=true, keep it true forever. Never flip back to false.
  - Apply this check BEFORE deciding on artifact attachments (step 3).
  - A search/listing goal is done when a search or list action event exists in HISTORY for it.
  - A full-content fetch goal is done when a full-content retrieval action event exists for it.
    Short snippet results (from a search) do NOT count as full-content retrievals.
  - A "fetch top N sources" goal is done ONLY when N separate retrieval events appear in HISTORY.

### 3. ATTACH ARTIFACT  (only for the FIRST goal still done=false)
HARD RULE: NEVER set send_artifact=true on a goal with done=true.
Only the first open goal (lowest-index slot with done=false) may receive an artifact.

Use the ARTIFACT INDEX TABLE in the user message to find which artifact belongs to which goal.
Each row shows: i=<integer>  artifact_id=<id>  produced_by=<goal_id>  (<goal_text>)
Only items with a numeric `i` value can be selected — entries with i=null are not artifacts.

Choose the right pattern for the open goal:

PATTERN A-SINGLE — One artifact (one-source goals)
  Use when the goal processes ONE piece of raw content:
    • An extraction or summarisation goal reading one fetched page or file
    • A reading goal that is the current step in a sequential read-then-answer chain
    • A fetch goal that retrieves a specific result from a prior search (needs the prior search artifact to find the URL)
  Action: set send_artifact=true, set artifact_index to the integer `i` of the ONE relevant artifact.

PATTERN A-MULTI — Multiple artifacts (synthesis goals that need N pages at once)
  Use when the goal is a SYNTHESIS or ANSWER goal that must read several fetched pages
  produced by earlier goals in the same run (e.g. "Synthesise the common advice from 3 fetched pages").
  Action: set send_artifact=true, set artifact_index to the integer `i` of the FIRST relevant artifact,
          AND set artifact_indices to the list of ALL `i` values needed (e.g. [0, 1, 2]).
  Only use this when ALL required source artifacts already exist in the ARTIFACT INDEX TABLE.

PATTERN B — No artifact (pure action goal)
  Use when the goal is purely: search / compute / open / time / query-knowledge-base.
  Action: leave send_artifact=false, artifact_index=null, artifact_indices=[].

Decision procedure:
  1. Is the open goal purely search/compute/open? → PATTERN B
  2. Is the open goal a synthesis/answer goal that needs content from N≥2 artifacts?
     a) Collect the `i` values for ALL prerequisite artifacts from the ARTIFACT INDEX TABLE.
     b) Confirm every artifact exists (i is not null).
     c) If ALL prerequisite goals already produced kind="answer" events in HISTORY, their
        summaries are already in HISTORY — do NOT re-attach the raw artifacts.
     d) Otherwise → PATTERN A-MULTI: set artifact_index=min(i values), artifact_indices=[all i values].
  3. Does the open goal need to process ONE artifact?
     a) Find the artifact produced by the prerequisite goal in the ARTIFACT INDEX TABLE.
     b) Confirm the artifact exists (i is not null).
     c) If the prerequisite goal already produced a kind="answer" event in HISTORY, that summary
        is already in HISTORY — do NOT re-attach the raw artifact.
     d) Otherwise → PATTERN A-SINGLE: set artifact_index to the integer `i` of that artifact.
  4. If no relevant artifact exists yet (the prerequisite hasn't run) → PATTERN B.

### 4. PRESERVE ORDER
Return goals in the SAME ORDER as PRIOR GOALS. Goal identity is determined by POSITION.
Rules:
  - Copy each prior goal's `text` verbatim into the same slot.
  - You MAY append new goals at the END when a discovery action (e.g., list_dir) reveals
    concrete items that were unknown at decomposition time. Keep all prior goals verbatim,
    append one new goal per concrete item, then re-append the original synthesis/report goal
    LAST so it stays the final step.
  - NEVER reorder, insert in the middle, or drop a prior goal.

## Precision constraint
All structural rules above are HARD CONSTRAINTS regardless of temperature or context complexity.
Violations — reordering goals, premature done=true on synthesis goals, wrong artifact_index,
setting send_artifact=true on a done goal — will break the agent loop and produce wrong answers.
When in doubt: be conservative. Mark done=false rather than guessing; leave send_artifact=false
rather than attaching the wrong artifact.
"""


# ---------------------------------------------------------------------------
# Helper: render memory hits with vector/keyword search method marker
# ---------------------------------------------------------------------------

def _snapshot_hits(hits: list[MemoryItem], current_run_id: str) -> list[dict]:
    """Render memory hits for the LLM. Artifacts are indexed (i) so
    Perception can point at them by integer; non-artifact hits show i=null.

    IMPORTANT: only hits from the current run receive a numeric `i`.
    Hits from previous runs that happen to carry an artifact_id are shown
    with i=null and a `stale_run` marker so the LLM understands they are
    historical context, not attachable artifacts. This prevents cross-run
    contamination when the FAISS index is small (few items, high top_k)
    and returns items from unrelated past queries at low similarity scores.

    Items retrieved via vector similarity are marked 'vector'; items from
    the keyword fallback (no embedding on the item) are marked 'keyword'."""
    art_pos = 0
    out = []
    for h in hits[:12]:
        # Only current-run artifact hits get a selectable index.
        # Cross-run artifact hits are surfaced as descriptive context (i=null).
        from_current_run = h.run_id == current_run_id
        i = None
        if h.artifact_id and from_current_run:
            i = art_pos
            art_pos += 1
        # Infer search method: if the item has an embedding it was indexed for
        # vector search, and the memory service prefers vector results.
        search_method = "vector" if h.embedding is not None else "keyword"
        entry: dict = {
            "i": i,
            "search_method": search_method,
            "kind": h.kind,
            "descriptor": h.descriptor,
            "keywords": h.keywords,
            "artifact_id": h.artifact_id,
        }
        if h.artifact_id and not from_current_run:
            entry["stale_run"] = True   # artifact from a past run — do not attach
        out.append(entry)
    return out


# ---------------------------------------------------------------------------
# Helper: render history as compact human-readable lines (hybrid format)
# ---------------------------------------------------------------------------

def _snapshot_history(history: list[dict]) -> str:
    """Render the last 10 history events as compact text lines.

    Uses structured key=value format rather than JSON to minimise token
    consumption. Values are clipped to keep the section scannable.
    """
    lines = []
    for ev in history[-10:]:
        kind = ev.get("kind", "?")
        it = ev.get("iter", "?")
        gid = ev.get("goal_id", "?")
        if kind == "answer":
            text_preview = (ev.get("text") or "")[:160].replace("\n", " ")
            lines.append(
                f"  iter={it} kind=answer goal_id={gid} text={text_preview!r}"
            )
        elif kind == "action":
            art = f" artifact_id={ev['artifact_id']}" if ev.get("artifact_id") else ""
            result = (ev.get("result_descriptor") or "")[:100].replace("\n", " ")
            lines.append(
                f"  iter={it} kind=action goal_id={gid}"
                f" tool={ev.get('tool', '?')}{art}"
                f" result={result!r}"
            )
        else:
            lines.append(f"  iter={it} kind={kind} goal_id={gid}")
    return "\n".join(lines) or "  (none — first iteration)"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def observe(
    query: str,
    hits: list[MemoryItem],
    history: list[dict],
    prior_goals: list[Goal],
    run_id: str,
) -> Observation:
    """Run one Perception iteration. Returns an updated Observation.

    Builds a structured, clearly-labelled user message so each section can
    be parsed independently. The artifact→goal lookup table is the critical
    addition that allows step 3 (attach) to work reliably without the LLM
    having to parse full history JSON.
    """
    ensure_gateway()

    # ── Ordered list of artifact IDs for index→id resolution ────────────────
    # Only include artifacts from the CURRENT run. Memory is persistent across
    # runs; a previous run's artifact appearing in FAISS hits must not become
    # an attachable candidate for the current query's goals.
    art_ids_in_order = [
        h.artifact_id for h in hits[:12]
        if h.artifact_id and h.run_id == run_id
    ]

    # ── PRIOR GOALS section — compact text ──────────────────────────────────
    prior_snapshot = [g.model_dump() for g in prior_goals] if prior_goals else []
    if prior_goals:
        first_open_idx = next(
            (i for i, g in enumerate(prior_goals) if not g.done), None
        )
        prior_lines = []
        for i, g in enumerate(prior_goals):
            status = "DONE" if g.done else "OPEN"
            marker = " ← next unfinished" if i == first_open_idx else ""
            prior_lines.append(f"  {g.id}: {g.text} [{status}]{marker}")
        prior_section = "\n".join(prior_lines)
    else:
        prior_section = "  (empty — decompose the query now)"

    # ── ARTIFACT INDEX TABLE — artifact_index → producing goal ──────────────
    # Build from HISTORY so it covers all artifacts ever produced, not just
    # those in the current memory hits window.
    goal_id_to_text = {g.id: g.text for g in prior_goals}
    artifact_table_lines = []
    seen_art: set[str] = set()
    art_idx = 0
    history_art_ids = []
    for ev in history:
        if ev.get("kind") == "action" and ev.get("artifact_id"):
            aid = ev["artifact_id"]
            if aid not in seen_art:
                gid = ev.get("goal_id", "?")
                gtext = goal_id_to_text.get(gid, "")[:60]
                artifact_table_lines.append(
                    f"  i={art_idx}  artifact_id={aid}  produced_by={gid}  ({gtext})"
                )
                seen_art.add(aid)
                history_art_ids.append(aid)
                art_idx += 1
    artifact_table = "\n".join(artifact_table_lines) or "  (none yet)"

    # ── MEMORY HITS section — JSON (machine-parseable, includes i index) ─────
    hits_snapshot = _snapshot_hits(hits, run_id)
    memory_section = json.dumps(hits_snapshot, indent=2) if hits_snapshot else "  []"

    # ── HISTORY section — compact text (human-readable, fewer tokens) ────────
    history_section = _snapshot_history(history)

    # ── Assemble user message ────────────────────────────────────────────────
    prompt = (
        f"USER QUERY: {query}\n\n"
        f"PRIOR GOALS:\n{prior_section}\n\n"
        f"ARTIFACT INDEX TABLE (use `i` as artifact_index when send_artifact=true):\n"
        f"{artifact_table}\n\n"
        f"MEMORY HITS (i=null entries are facts/outcomes, not selectable artifacts;\n"
        f"[vector] = semantically retrieved, [keyword] = keyword fallback):\n"
        f"{memory_section}\n\n"
        f"HISTORY (newest last):\n{history_section}\n\n"
        f"Return the current goal list as JSON matching the schema."
    )

    schema = _PerceptionOutput.model_json_schema()
    reply = LLM().chat(
        prompt=prompt,
        system=_SYSTEM,
        auto_route="perception",
        provider="g",
        response_format={
            "type": "json_schema",
            "schema": schema,
            "name": "PerceptionOutput",
            "strict": True,
        },
        temperature=1.0,
    )

    parsed = reply.get("parsed")
    if not parsed or not parsed.get("goals"):
        return Observation(goals=[Goal(id=new_id("g"), text=query)])

    # ── Post-processing: synthesis-goal done guard ───────────────────────────
    # Defence-in-depth: even if the LLM marks a synthesis goal done from a
    # tool-call alone (violating the hard constraint in the prompt), we catch
    # it here and flip it back to open.
    SYNTHESIS_KW = (
        "evaluate", "select", "synthes", "compare", "decide", "recommend",
        "tell me which", "most appropriate", "analy", "pick", "choose",
        "summarise", "summarize", "answer", "identify", "find", "determine",
        "extract", "list", "report", "tell", "explain", "describe", "name",
    )

    # ── Post-processing: goal-count invariant ────────────────────────────────
    # Never contract the goal list; prior goals keep their slot and id.
    # New goals may be appended (discovery pattern) but duplicates are dropped.
    raw_goals = parsed["goals"]
    if prior_goals:
        prior_texts = {g.text.strip().lower() for g in prior_goals}
        deduped = list(raw_goals[:len(prior_goals)])
        for extra in raw_goals[len(prior_goals):]:
            t = (extra.get("text") or "").strip().lower()
            if not t or t in prior_texts:
                continue
            prior_texts.add(t)
            deduped.append(extra)
        raw_goals = deduped

    out_goals: list[Goal] = []
    for i, d in enumerate(raw_goals):
        delta = _GoalDelta.model_validate(d)

        # Resolve artifact_index / artifact_indices → artifact_id list
        attach_ids: list[str] = []
        if delta.send_artifact:
            # Collect all requested indices, deduplicating while preserving order.
            requested: list[int] = []
            # artifact_indices takes precedence when provided (multi-attach)
            if delta.artifact_indices:
                requested = delta.artifact_indices
                # Also include artifact_index if it's not already in the list
                if delta.artifact_index is not None and delta.artifact_index not in requested:
                    requested = [delta.artifact_index] + requested
            elif delta.artifact_index is not None:
                requested = [delta.artifact_index]

            seen_ids: set[str] = set()
            for idx in requested:
                if 0 <= idx < len(history_art_ids):
                    aid = history_art_ids[idx]
                    if aid not in seen_ids:
                        attach_ids.append(aid)
                        seen_ids.add(aid)

        gid = prior_goals[i].id if i < len(prior_goals) else new_id("g")
        was_done = prior_goals[i].done if i < len(prior_goals) else False

        proposed_done = was_done or delta.done

        # Universal guard: an answer event with empty or trivially short text
        # (< 15 chars) must NEVER mark any goal done — it means Decision failed
        # to produce content (most commonly: it was confused by a user_query
        # memory hit). This guard fires before the synthesis-specific check.
        if proposed_done and not was_done:
            answer_events = [
                h for h in history
                if h.get("kind") == "answer" and h.get("goal_id") == gid
            ]
            action_events = [
                h for h in history
                if h.get("kind") == "action" and h.get("goal_id") == gid
            ]
            # If there are answer events but no action events (no tool was called),
            # and all answers are empty/trivial, keep the goal open.
            if answer_events and not action_events:
                all_trivial = all(
                    len((h.get("text") or "").strip()) < 15
                    for h in answer_events
                )
                if all_trivial:
                    proposed_done = False

        # Synthesis guard: if the LLM marked a synthesis goal done but there
        # is no qualifying answer event in history, flip it back to open.
        # Note: pure action goals (indexing, querying, searching, fetching) are completed by
        # tool calls alone and must be excluded from this guard to prevent infinite loops.
        if proposed_done and not was_done:
            gtext_lc = delta.text.lower()
            is_action_goal = any(
                gtext_lc.startswith(x) for x in ("index", "query", "search", "fetch", "find")
            )
            if not is_action_goal and any(kw in gtext_lc for kw in SYNTHESIS_KW):
                has_answer = any(
                    h.get("kind") == "answer"
                    and h.get("goal_id") == gid
                    and len((h.get("text") or "")) >= 15
                    for h in history
                )
                if not has_answer:
                    proposed_done = False

        # Fetch guard: if the LLM marked a web-fetch goal done but there is no
        # fetch_url action in history for it, flip it back to open.
        if proposed_done and not was_done:
            gtext_lc = delta.text.lower()
            if (gtext_lc.startswith("fetch ") or "fetch the " in gtext_lc) and any(x in gtext_lc for x in ("url", "result", "http", "page", "web", "site")):
                has_fetch = any(
                    h.get("kind") == "action"
                    and h.get("goal_id") == gid
                    and h.get("tool") == "fetch_url"
                    for h in history
                )
                if not has_fetch:
                    proposed_done = False

        out_goals.append(Goal(
            id=gid,
            text=delta.text,
            done=proposed_done,
            attach_artifact_ids=attach_ids,
        ))

    # ── Safety net: force-attach for forgotten synthesis/fetch goals ─────────
    # At temperature=1.0, the LLM occasionally forgets to set send_artifact=true.
    # We apply robust fallback attachments to guarantee the agent loop never stalls.
    for g in out_goals:
        if g.done:
            continue
        if g.attach_artifact_ids:
            break  # already attached — nothing to do
        if not history_art_ids:
            break  # no artifacts available yet
        
        gtext_lc = g.text.lower()
        # Case A: Synthesis goal gets ALL artifacts produced so far
        if any(kw in gtext_lc for kw in SYNTHESIS_KW):
            g.attach_artifact_ids = list(history_art_ids)
        
        # Case B: Fetch/extraction goal gets the search results artifact to find the URLs
        elif (gtext_lc.startswith("fetch ") or "fetch the " in gtext_lc) and any(x in gtext_lc for x in ("url", "result", "http", "page", "web", "site")):
            search_art_ids = [
                h.get("artifact_id") for h in history
                if h.get("kind") == "action" and h.get("tool") == "web_search" and h.get("artifact_id")
            ]
            if search_art_ids:
                g.attach_artifact_ids = [search_art_ids[-1]]
        
        break  # only act on the FIRST unfinished goal

    return Observation(goals=out_goals)
