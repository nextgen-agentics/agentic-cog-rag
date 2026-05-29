"""Decision: one LLM call per turn.

Given the current goal, the relevant memory hits (descriptors + chunk previews),
the recent history, and optionally the raw bytes of an artifact Perception
attached to this goal, the model picks ONE of:

  (a) answer in plain text — the answer may itself be summarisation,
      extraction, comparison, translation, or any other semantic work the
      LLM does on the attached content or memory hits;
  (b) call exactly one MCP tool from the available tool list.

There is no taxonomy of "operation kinds". The model decides what it is
doing. Decision just routes the dispatch.

Session 7 note: memory hits are vector-retrieved first (FAISS) before falling
back to keyword search. Hits of kind `fact` whose descriptors start with
`[sandbox:` or `[art:` are indexed document chunks — the LLM should synthesise
from their inline `chunk:` previews or call `search_knowledge` for more,
rather than re-fetching the original source.
"""

from __future__ import annotations

import json

from gateway import LLM, ensure_gateway
from schemas import DecisionOutput, Goal, MemoryItem, ToolCall


# ---------------------------------------------------------------------------
# System prompt — production-grade, numbered decision tree
# ---------------------------------------------------------------------------

_SYSTEM = """\
You are the DECISION module of an autonomous AI agent.
You receive exactly ONE goal and must choose the single best next action: either directly answering the goal with a plain text response, or calling exactly one tool to gather or persist information.

## How to respond:
- If you have enough information to satisfy the goal, output your answer as a plain text response. Do NOT call any tools.
- If you need more information or need to perform a persistence action, invoke the single most appropriate tool. Do NOT output any plain text in this case.

## Decision tree — reason through these steps in order

### STEP 1 — Do I already have enough information?
⚠ RULE: If the GOAL is a pure action goal (starts with `index`, `query`, `search`, `fetch`, or `find`), you must ALWAYS perform the action tool call. Skip STEP 1 entirely and go directly to STEP 2. Do NOT produce a text answer for action goals.
Otherwise, check in this exact order:

a) MEMORY HITS
   If a memory hit directly answers the goal (a stored fact, preference, or tool outcome),
   answer from memory now. Do not call a tool when the answer already exists in memory.

   Sub-rule — indexed chunk hits (kind=fact, descriptor starts with [sandbox: or [art:):
     • If the `chunk:` previews shown inline under those hits are sufficient to fully answer
       the goal → answer directly from the previews. Do NOT call search_knowledge again.
     • If the chunk previews are too short or incomplete to answer fully
       → call search_knowledge to retrieve the full chunk text.
     • Never call fetch_url or read_file when indexed chunks already exist for the topic.

b) HISTORY
   If history contains an action or answer that fully satisfies the goal, answer from that.
   Do NOT repeat a tool call that already appears in history.
   Check ALREADY FETCHED URLs — never re-fetch a URL already listed there.

c) ATTACHED ARTIFACT
   If artifact content is provided, read ALL visible text before deciding.
   Distinguish between two artifact types:
     • Search/snippet artifact: contains only short URL excerpts (title + brief summary).
       This does NOT contain the full text of each source. If the goal requires reading
       or extracting detail from the full source, you must retrieve the full content.
     • Full-content artifact: contains the raw text of a fetched page or document.
       If such an artifact is attached and covers the goal → answer from it directly.
       Do NOT re-fetch a source whose full content is already in an attached artifact.
     • Knowledge Base chunks: contains retrieved vector chunks from search_knowledge.
       If these chunks conceptually answer the comparative or conceptual query (such as
       explaining backpropagation, reward shaping, or error correction, even if the literal
       words of the query like "credit assignment" are absent), you MUST synthesise the
       answer from these chunks directly. Perform semantic reasoning across the available
       thematic threads. Do NOT assume information is missing just because the literal
       words in the query are not present in the chunks.

   TRUNCATION RULE: If the artifact shows '[truncated; full size N bytes]', the first
   20 KB and last 10 KB of the document are displayed. This means the document is large,
   NOT that the content is missing. Key facts typically appear near the start of a
   document (summaries, lead sections, introductions) and are usually within the visible
   head. Attempt to answer from the visible text FIRST.
   Only proceed to STEP 2 if the visible text genuinely lacks the needed information.

If any check above passes → produce the answer now. Do NOT call a tool.
If none passes → go to STEP 2.

### STEP 2 — Which single tool will get me closest to done?
Pick ONE tool. Fill in all required arguments precisely. One call per iteration.

Tool selection hierarchy (prefer higher items over lower when applicable):
  1. Goal involves making content SEARCHABLE across turns/runs
     ("index", "ingest", "make searchable", "add to knowledge base"):
     → call index_document  (NOT read_file — read_file is one-shot and then discards)
  2. Content must be queried from an already-indexed knowledge base:
     → call search_knowledge  (see tool description for prerequisite requirements)
     ⚠ Only valid if RECENT HISTORY contains an index_document call.
     ⚠ If ATTACHED ARTIFACTS already contains the document text (even if truncated),
       answer from the artifact directly — do NOT call search_knowledge on content
       that was fetched but never indexed.
  3. Goal is to SAVE / CREATE / REMEMBER something durably (reminder, note, record):
     → call create_file or update_file in the sandbox
     Do NOT describe the action in text — actually persist the data.
     Make reasonable filename and content choices without asking the user.
  4. Goal involves current time, relative dates ("today", "this weekend", deadlines):
     → call get_time first — never guess or assume dates
  5. Goal requires reading the FULL content of a known URL:
     → call fetch_url  (check ALREADY FETCHED URLs first — skip if already retrieved)
  6. Goal requires finding URLs for a topic:
     → call web_search
  7. Goal requires reading or listing a real local file by name:
     → call read_file or list_dir (sandbox only — not for artifact handles)

Specialised tools always beat general ones (get_time > web_search for current time).
For a goal that requires N full sources, retrieve the NEXT source not yet in ALREADY FETCHED URLS.

## Hard constraints
- NO REPEAT OF REJECTED CALLS: If RECENT HISTORY shows a tool call that returned an
  ERROR (marked ⚠ ERROR in history), that exact call is BLOCKED. Do NOT repeat it.
  Read the error message, understand why it failed, and choose a completely different action.
  A second identical call to a blocked tool will produce the same ERROR — you will loop forever.
- ARTIFACT HANDLES ARE NOT PATHS OR URLS: Strings starting with `art:` are internal
  content-store handles — NEVER pass them to read_file, list_dir, fetch_url, or any tool.
  Artifact bytes arrive pre-loaded in the ATTACHED ARTIFACTS section — answer from that text.
  WRONG: passing an artifact ID (starting with "art:") to a file reading or URL fetching tool.
  RIGHT: read the bytes already shown in ATTACHED ARTIFACTS and answer directly.
- SANDBOX FILES ONLY: read_file and list_dir operate on the local sandbox/ directory.
  Only call them when the user has asked you to read or list a real sandbox file by name.
- NO DUPLICATE FETCHES: Do not call a tool for a URL that already appears in ALREADY FETCHED URLs.
- NO QUESTIONS: Never ask the user for additional information or clarification.
  Make sensible assumptions and act. An autonomous agent cannot wait for user input.
- SELF-CONTAINED ANSWERS: The user sees ONLY your answer text. Never reference artifact IDs,
  tool names, goal IDs, or any internal agent state in the answer.
- STRICT FORMATTING AND BREVITY: You must strictly respect all formatting, length, and brevity constraints specified in the query or goal (e.g. "short numbered list", "one sentence", "brief"). Avoid verbose introductions, explanations, or unsolicited analyses. Get straight to the requested answer.
- VALID ARGUMENTS: All tool arguments must be valid JSON values (strings, numbers, booleans).
  Never use placeholder values.
- TEMPORAL AWARENESS: If the goal involves current time, dates, deadlines, "this weekend",
  "today", or any relative time expression, call get_time before answering unless time is
  already present in memory or history.
- EXACTLY ONE OUTPUT: Provide either a plain text answer OR make a tool call — never both, never neither.
"""


# How much attached content to send per turn. Most LARGE-tier workers handle
# 30 KB comfortably; truncate above that with a head-and-tail window.
_ATTACH_HEAD = 20_000
_ATTACH_TAIL = 10_000


# ---------------------------------------------------------------------------
# User message formatters
# ---------------------------------------------------------------------------

def _format_hits(hits: list[MemoryItem]) -> str:
    """Surface enough of each hit's `value` for Decision to anchor on it.

    Hits with source="user_query" are recordings of the user's request text,
    not factual data — they are excluded so the LLM does not mistake them for
    an answer. This is the source of the 'empty answer on fetch goal' bug where
    Decision sees the stored query, thinks memory already contains the answer,
    produces empty text, and Perception then marks the fetch goal done.

    For indexed-chunk facts (value.chunk), we render a short chunk preview
    so Decision can answer directly from memory hits when search_knowledge
    has already populated them — avoiding a redundant search_knowledge call.
    For classifier facts (value.raw), we render the raw stored content.
    For other tool outcomes, we render compact value fields.
    """
    if not hits:
        return "  (none)"
    out = []
    for h in hits[:10]:
        # Skip user_query source hits: they record the user's request text,
        # not factual answers. Showing them causes the LLM to conclude the
        # answer is already in memory when it is not.
        if getattr(h, "source", "") == "user_query":
            continue
        line = f"  - [{h.kind}] {h.descriptor}"
        val = h.value or {}
        if val:
            raw = val.get("raw")
            chunk = val.get("chunk")
            if isinstance(raw, str) and raw.strip():
                line += f"\n      raw: {raw[:200]}"
            elif isinstance(chunk, str) and chunk.strip():
                src = val.get("source") or ""
                preview = chunk[:600].replace("\n", " ")
                more = "…" if len(chunk) > 600 else ""
                line += f"\n      chunk ({src}): {preview}{more}"
            else:
                compact = {
                    k: v for k, v in val.items()
                    # Exclude chunk (already rendered above) and artifact_id.
                    # artifact_id is an internal handle that must never be
                    # passed to tools — omitting it prevents the LLM from
                    # picking it up out of memory hit values and calling
                    # read_file(path="art:xxx") or similar.
                    if k not in ("chunk", "artifact_id")
                    and not (isinstance(v, str) and len(v) > 200)
                }
                if compact:
                    line += f"\n      value: {json.dumps(compact)[:240]}"
        out.append(line)
    return "\n".join(out)


def _format_fetched_urls(history: list[dict]) -> str:
    """Build a deduplicated list of URLs already fetched this run.

    Excludes web_search (which returns search snippets, not full content).
    Decision checks this list to avoid re-fetching a URL that was already
    retrieved in a prior iteration — without having to parse history entries.
    """
    seen: list[str] = []
    seen_set: set[str] = set()
    for ev in history:
        if ev.get("kind") != "action":
            continue
        if ev.get("tool", "") in ("web_search",):
            continue
        url = ev.get("arguments", {}).get("url", "")
        if url and url not in seen_set:
            seen.append(url)
            seen_set.add(url)
    return "\n".join(f"  {u}" for u in seen) or "  (none yet)"


def _format_history(history: list[dict]) -> str:
    """Render the last 8 history events as compact text lines.

    8 events (up from S7's 6) to handle multi-file indexing and KB-query
    chains without losing earlier context. Artifact IDs are annotated as
    internal handles — not file paths or URLs — to prevent the LLM from
    passing them as tool arguments.
    """
    if not history:
        return "  (empty)"
    lines = []
    for h in history[-8:]:
        kind = h.get("kind", "?")
        it = h.get("iter", "?")
        if kind == "answer":
            text = (h.get("text") or "")[:140]
            lines.append(f"  iter={it} ANSWER goal={h.get('goal_id')}: {text!r}")
        elif kind == "action":
            tool = h.get("tool", "?")
            args = json.dumps(h.get("arguments") or {})[:80]
            desc = (h.get("result_descriptor") or "")[:300]
            # Mark error results prominently so the LLM knows not to repeat them.
            is_error = desc.startswith("ERROR")
            error_prefix = "⚠ ERROR: " if is_error else ""
            # Annotate artifact references so the LLM never confuses art: handles
            # with file paths or URLs.
            art_note = (
                f" → stored as internal artifact handle {h['artifact_id']!r} "
                f"(NOT a file path or URL)"
                if h.get("artifact_id") else ""
            )
            lines.append(f"  iter={it} {error_prefix}TOOL {tool}({args}){art_note} → {desc}")
        else:
            lines.append(f"  iter={it} {kind} {h}")
    return "\n".join(lines)


def _format_attached(attached: list[tuple[str, bytes]]) -> str:
    """Render attached artifact bytes for the user message.

    Very large artifacts are truncated with a head-and-tail window so the
    model stays within the LARGE-tier context window.
    """
    if not attached:
        return ""
    parts = ["\n\nATTACHED ARTIFACTS (read this content — do NOT re-fetch these sources):"]
    for art_id, data in attached:
        text = data.decode("utf-8", errors="replace")
        if len(text) > _ATTACH_HEAD + _ATTACH_TAIL + 50:
            text = (
                text[:_ATTACH_HEAD]
                + f"\n\n...[truncated; full size {len(data)} bytes]...\n\n"
                + text[-_ATTACH_TAIL:]
            )
        parts.append(f"--- {art_id} ---\n{text}")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def next_step(
    goal: Goal,
    hits: list[MemoryItem],
    attached: list[tuple[str, bytes]],
    history: list[dict],
    mcp_tools: list[dict],
) -> DecisionOutput:
    """Select the next action for one bounded goal.

    Parameters
    ----------
    goal:       The single goal to work on.
    hits:       Relevant memory items from Memory.read().
    attached:   List of (artifact_id, raw_bytes) fetched by the loop.
    history:    Full run history accumulated so far.
    mcp_tools:  MCP tool definitions — passed as native tools= to the gateway.
    """
    ensure_gateway()

    prompt = (
        f"GOAL:\n  {goal.text}\n\n"
        f"MEMORY HITS (vector-retrieved unless noted; chunk: previews included):\n"
        f"{_format_hits(hits)}\n\n"
        f"ALREADY FETCHED URLs this run (do not re-fetch these):\n"
        f"{_format_fetched_urls(history)}\n\n"
        f"RECENT HISTORY (newest last):\n"
        f"{_format_history(history)}"
        f"{_format_attached(attached)}"
    )

    # Estimate prompt size. 1 token ≈ 4 characters.
    # Gateway router triggers HUGE (> 8,000 estimated tokens) when prompt + system exceeds ~28,000 chars.
    # Set provider="g" explicitly and auto_route=None when the prompt is large to bypass the router and go direct to Gemini.
    use_auto: str | None = "decision"
    prov: str | None = None
    if len(prompt) + len(_SYSTEM) > 26_000:
        use_auto = None
        prov = "g"
        print(f"[decision]      Prompt size is large ({len(prompt) + len(_SYSTEM)} chars) — bypassing auto-router to direct Gemini (provider='g')")

    reply = LLM().chat(
        prompt=prompt,
        system=_SYSTEM,
        cache_system=True,          # system prompt is constant per run — cache it
        tools=mcp_tools,
        tool_choice="auto",
        auto_route=use_auto,
        provider=prov,
        temperature=0,
        max_tokens=2048,
    )

    tcs = reply.get("tool_calls") or []
    
    # Python-level enforcement of action goals:
    # If the goal starts with action verbs (case-insensitive), it must call a tool.
    goal_text_lc = goal.text.lower()
    is_action_goal = any(
        goal_text_lc.startswith(verb)
        for verb in ("index", "query", "search", "fetch", "find")
    )
    
    if is_action_goal and not tcs:
        print(f"[decision] WARNING: action goal {goal.id!r} ({goal.text[:40]}...) produced text instead of a tool call — retrying with forced tool call constraint")
        retry_system = _SYSTEM + "\n\nCRITICAL WARNING: The active goal is a pure ACTION goal. You MUST invoke one of the available tools. You are STRICTLY FORBIDDEN from producing any plain text answers. Choose the single most appropriate tool call."
        retry_reply = LLM().chat(
            prompt=prompt,
            system=retry_system,
            tools=mcp_tools,
            tool_choice="auto",
            auto_route=use_auto,
            provider=prov,
            temperature=0,
            max_tokens=2048,
        )
        retry_tcs = retry_reply.get("tool_calls") or []
        if retry_tcs:
            tcs = retry_tcs
        else:
            print(f"[decision] WARNING: retry failed to produce tool call for action goal {goal.id!r}")

    if tcs:
        tc = tcs[0]
        return DecisionOutput(
            tool_call=ToolCall(
                name=tc["name"],
                arguments=tc.get("arguments") or {},
            )
        )

    answer_text = (reply.get("text") or "").strip()
    if answer_text:
        return DecisionOutput(answer=answer_text)

    # Neither tool call nor text — Gemini occasionally returns empty output in AUTO
    # function-calling mode when the correct action is to synthesise from attached
    # content. Retry once with tool_choice="none" (text-only mode) so the model
    # is forced to produce an answer rather than deferring to a tool call.
    print(f"[decision] WARNING: empty response for goal {goal.id!r} — retrying with tool_choice=none")
    retry_reply = LLM().chat(
        prompt=prompt,
        system=_SYSTEM,
        tools=mcp_tools,
        tool_choice="none",      # no function calls allowed — must produce text
        auto_route="decision",
        temperature=0,
        max_tokens=2048,
    )
    retry_tcs = retry_reply.get("tool_calls") or []
    if retry_tcs:
        tc = retry_tcs[0]
        return DecisionOutput(tool_call=ToolCall(name=tc["name"], arguments=tc.get("arguments") or {}))
    retry_text = (retry_reply.get("text") or "").strip()
    if retry_text:
        return DecisionOutput(answer=retry_text)

    print(f"[decision] WARNING: retry also empty for goal {goal.id!r} — returning empty sentinel")
    return DecisionOutput(answer="")
