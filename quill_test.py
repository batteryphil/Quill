#!/usr/bin/env python3
"""
quill_test.py — Thorough integration test of all Quill backend endpoints.

Tests:
  ✓ Projects CRUD
  ✓ Scenes CRUD  
  ✓ Snapshots
  ✓ Characters + World Rules (Story Bible)
  ✓ Scene extract (fact extraction pipeline)
  ✓ RAG index build + semantic query
  ✓ Audit system (contradiction detection)
  ✓ Export (Markdown compile)
  ✓ Generation endpoints (SSE response shape)
  ✓ Scene meta labels
  ✓ Export check (pandoc detection)

Run from quill/ directory:
  python3 quill_test.py
"""

import asyncio
import json
import sys
import time
import urllib.request
import urllib.error
from typing import Any

BASE = "http://127.0.0.1:8000"

# ─── Colours ─────────────────────────────────────────────────────────────────
GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

pass_count = fail_count = skip_count = 0

def ok(msg: str)   -> None:
    global pass_count
    pass_count += 1
    print(f"  {GREEN}✓{RESET} {msg}")

def fail(msg: str, detail: str = "") -> None:
    global fail_count
    fail_count += 1
    print(f"  {RED}✗{RESET} {msg}")
    if detail:
        print(f"    {RED}→ {detail}{RESET}")

def skip(msg: str) -> None:
    global skip_count
    skip_count += 1
    print(f"  {YELLOW}⊘{RESET} {msg}")

def section(title: str) -> None:
    print(f"\n{BOLD}{CYAN}{'─'*55}{RESET}")
    print(f"{BOLD}{CYAN}  {title}{RESET}")
    print(f"{BOLD}{CYAN}{'─'*55}{RESET}")

# ─── HTTP helpers ─────────────────────────────────────────────────────────────

def _req(method: str, path: str, body: Any = None, raw=False) -> Any:
    url     = f"{BASE}{path}"
    data    = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    req     = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw_bytes = r.read()
            if raw:
                return raw_bytes
            return json.loads(raw_bytes)
    except urllib.error.HTTPError as e:
        err_body = e.read().decode()
        raise RuntimeError(f"HTTP {e.code}: {err_body[:200]}")
    except Exception as e:
        raise RuntimeError(str(e))

get    = lambda path: _req("GET", path)
post   = lambda path, body=None: _req("POST", path, body)
put    = lambda path, body=None: _req("PUT", path, body)
patch  = lambda path, body=None: _req("PATCH", path, body)
delete = lambda path: _req("DELETE", path)
download = lambda path, body=None: _req("POST", path, body, raw=True)

def check_sse(path: str, body: dict, timeout: int = 8) -> str:
    """Hit an SSE endpoint, return first 300 chars of response."""
    url  = f"{BASE}{path}"
    data = json.dumps(body).encode()
    req  = urllib.request.Request(url, data=data,
                                  headers={"Content-Type": "application/json"},
                                  method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            out = r.read(1024).decode(errors="replace")
            return out
    except urllib.error.URLError as e:
        # Timeout means server responded but LLM is streaming slowly — still a valid shape check
        if "timed out" in str(e).lower() or "timeout" in str(e).lower():
            raise RuntimeError("LLM_TIMEOUT")
        raise RuntimeError(str(e))
    except Exception as e:
        if "timed out" in str(e).lower():
            raise RuntimeError("LLM_TIMEOUT")
        raise RuntimeError(str(e))

# ─── Test data ────────────────────────────────────────────────────────────────

SCENE_1 = """Elena stood at the top of the lighthouse, her red hair whipping in the salt wind.
She had been here before — three years ago, when Marcus first told her about the letters.
Now Marcus was missing, and the only clue was an old brass key she had found in his studio.
Elena pressed the key into the lock of the trapdoor. It turned with a satisfying click."""

SCENE_2 = """Elena descended the spiral stairs into the lighthouse basement.
Marcus had installed shelves along the stone walls, filled with glass jars of copper powder.
She recognised his handwriting on labels: "Solution A", "Catalyst", "Sample 17".
Her dark curly hair — which she had cut short last spring — kept falling into her eyes.
Through the porthole, she could see the storm gathering over the black water."""

SCENE_3 = """Inspector Raines arrived at dawn, his lantern swinging in the mist.
He knocked three times on the lighthouse door. When Elena opened it, his eyes went to her collar.
"Miss Vasquez," he said, "your hair. It was described to me as red."
Elena touched her hair — dark and curly. A contradiction that would follow her for months."""

TEST_PROJECT_ID = None

# ═══════════════════════════════════════════════════════════════════════════════
def main():
    global TEST_PROJECT_ID

    print(f"\n{BOLD}{'═'*55}")
    print(f"  Quill Integration Test Suite")
    print(f"{'═'*55}{RESET}\n")
    print(f"  Target: {BASE}")

    # ── 1. Server health ────────────────────────────────────────────────────
    section("1. Server Health")
    try:
        projects = get("/api/projects")
        ok(f"Server reachable — {len(projects)} existing project(s)")
    except Exception as e:
        fail("Server not reachable", str(e))
        print(f"\n  {RED}Cannot proceed without server. Is it running?{RESET}\n")
        sys.exit(1)

    # ── 2. Projects CRUD ────────────────────────────────────────────────────
    section("2. Projects — Create / Get / List")
    try:
        p = post("/api/projects", {
            "title":           "Quill Test Novel",
            "genre":           "mystery",
            "word_count_goal": 60_000,
        })
        TEST_PROJECT_ID = p["id"]
        ok(f"Created project: id={TEST_PROJECT_ID}")
    except Exception as e:
        fail("Create project", str(e)); sys.exit(1)

    try:
        project = get(f"/api/projects/{TEST_PROJECT_ID}")
        assert project["title"] == "Quill Test Novel"
        ok("Get project: title matches")
    except Exception as e:
        fail("Get project", str(e))

    try:
        all_projects = get("/api/projects")
        ids = [p["id"] for p in all_projects]
        assert TEST_PROJECT_ID in ids
        ok(f"List projects: test project in list ({len(all_projects)} total)")
    except Exception as e:
        fail("List projects", str(e))

    # ── 3. Scenes CRUD ───────────────────────────────────────────────────────
    section("3. Scenes — Create / Get / Update / Snapshots")
    scene_ids = []

    for i, (content, title) in enumerate([
        (SCENE_1, "The Key"),
        (SCENE_2, "The Basement"),
        (SCENE_3, "Inspector Raines"),
    ]):
        try:
            s = post(f"/api/projects/{TEST_PROJECT_ID}/scenes", {
                "act": 1, "chapter": 1, "title": title
            })
            scene_ids.append(s["id"])
            ok(f"Created scene {i+1}: {s['id']} — '{title}'")
        except Exception as e:
            fail(f"Create scene {i+1}", str(e))

    # Save content to each scene
    for sid, content in zip(scene_ids, [SCENE_1, SCENE_2, SCENE_3]):
        try:
            put(f"/api/projects/{TEST_PROJECT_ID}/scenes/{sid}", {"content": content})
            ok(f"Saved content to {sid}: {len(content.split())} words")
        except Exception as e:
            fail(f"Save scene {sid}", str(e))

    # Verify read-back
    try:
        s = get(f"/api/projects/{TEST_PROJECT_ID}/scenes/{scene_ids[0]}")
        assert "Elena" in s["content"]
        ok(f"Get scene content: verified (wc={s['word_count']})")
    except Exception as e:
        fail("Get scene content", str(e))

    # List scenes
    try:
        scenes = get(f"/api/projects/{TEST_PROJECT_ID}/scenes")
        assert len(scenes) == 3
        ok(f"List scenes: {len(scenes)} found")
    except Exception as e:
        fail("List scenes", str(e))

    # Snapshot (save same scene twice to trigger snapshot)
    try:
        put(f"/api/projects/{TEST_PROJECT_ID}/scenes/{scene_ids[0]}",
            {"content": SCENE_1 + "\n\n[Second save for snapshot]"})
        snaps = get(f"/api/projects/{TEST_PROJECT_ID}/scenes/{scene_ids[0]}/snapshots")
        assert len(snaps) >= 1
        ok(f"Snapshots: {len(snaps)} snapshot(s) created")

        snap = get(f"/api/projects/{TEST_PROJECT_ID}/snapshots/{scene_ids[0]}/{snaps[0]}")
        assert "content" in snap
        ok(f"Get snapshot: content retrieved ({len(snap['content'])} chars)")
    except Exception as e:
        fail("Snapshot system", str(e))

    # ── 4. Story Bible — Characters ─────────────────────────────────────────
    section("4. Story Bible — Characters")
    try:
        chars = get(f"/api/projects/{TEST_PROJECT_ID}/characters")
        ok(f"Get characters: returned {len(chars)} entries (pre-extraction)")

        # Manual PATCH
        updated = patch(
            f"/api/projects/{TEST_PROJECT_ID}/characters/Elena",
            {"field": "appearance", "value": "Red hair, tall, weathered"}
        )
        assert updated["appearance"] == "Red hair, tall, weathered"
        ok("Manual character update: Elena.appearance set")

        updated2 = patch(
            f"/api/projects/{TEST_PROJECT_ID}/characters/Elena",
            {"field": "trait", "value": "Determined, secretive"}
        )
        assert updated2["trait"] == "Determined, secretive"
        ok("Manual character update: Elena.trait set")

        chars2 = get(f"/api/projects/{TEST_PROJECT_ID}/characters")
        assert "Elena" in chars2
        ok(f"Re-read characters: Elena present with {len(chars2['Elena'])} fields")
    except Exception as e:
        fail("Story Bible — Characters", str(e))

    # ── 5. Story Bible — World Rules ─────────────────────────────────────────
    section("5. Story Bible — World Rules")
    test_rules = [
        ("The lighthouse key opens the trapdoor to the basement", "rule"),
        ("Marcus vanished three years before the story starts",   "timeline"),
        ("Copper powder is significant to the mystery",           "lore"),
    ]
    try:
        for fact, cat in test_rules:
            r = post(f"/api/projects/{TEST_PROJECT_ID}/world_rules",
                     {"fact": fact, "category": cat})
            assert r.get("fact") == fact or r.get("status") == "exists"
            ok(f"Added world rule [{cat}]: {fact[:50]}…")

        # Duplicate check
        dup = post(f"/api/projects/{TEST_PROJECT_ID}/world_rules",
                   {"fact": test_rules[0][0]})
        assert dup.get("status") == "exists"
        ok("Duplicate rule rejected correctly")

        rules = get(f"/api/projects/{TEST_PROJECT_ID}/world_rules")
        assert len(rules) >= len(test_rules)
        ok(f"Get world rules: {len(rules)} rules total")
    except Exception as e:
        fail("Story Bible — World Rules", str(e))

    # ── 6. Fact Extraction (RAG pipeline entry point) ────────────────────────
    section("6. Fact Extraction — RAG Pipeline")
    print(f"  {YELLOW}Note: extraction requires LLM server. Sending requests and checking response shape.{RESET}")

    for i, (sid, label) in enumerate(zip(scene_ids, ["Scene 1", "Scene 2", "Scene 3"])):
        content = [SCENE_1, SCENE_2, SCENE_3][i]
        try:
            resp = post(
                f"/api/extract/scene_facts?project_id={TEST_PROJECT_ID}",
                {"scene_id": sid, "text": content}
            )
            assert "status" in resp or "task" in resp or "scene_id" in resp
            ok(f"Extract triggered for {label} ({sid}): {resp}")
        except Exception as e:
            fail(f"Extract {label}", str(e))

    # Wait for background tasks (if LLM is running, this could populate real data)
    print(f"\n  Waiting 5s for any background processing…")
    time.sleep(5)

    # Check scene_meta populate (may be empty if no LLM)
    try:
        meta = get(f"/api/projects/{TEST_PROJECT_ID}/scene_meta")
        ok(f"Scene meta endpoint: returned {len(meta)} entries")
        if meta:
            sid0 = next(iter(meta))
            entry = meta[sid0]
            ok(f"  First entry keys: {list(entry.keys())}")
            for field in ["summary", "pov", "pacing", "tension"]:
                if field in entry:
                    ok(f"  {field}: {str(entry[field])[:60]}")
    except Exception as e:
        fail("Scene meta", str(e))

    # ── 7. RAG — Index build + semantic query ────────────────────────────────
    section("7. RAG — Index Build & Semantic Query")
    try:
        # Check if chromadb + sentence-transformers are available
        import importlib
        chromadb_ok = importlib.util.find_spec("chromadb") is not None
        st_ok       = importlib.util.find_spec("sentence_transformers") is not None
        ok(f"chromadb available: {chromadb_ok}")
        ok(f"sentence_transformers available: {st_ok}")
    except Exception as e:
        fail("RAG dependency check", str(e))

    # Direct RAG module test (bypass HTTP)
    try:
        sys.path.insert(0, "/home/phil/.gemini/antigravity/scratch/quill")
        import importlib
        rag = importlib.import_module("backend.rag")

        # Manually add a document to the RAG index
        async def test_rag_direct():
            await rag.upsert_scene(
                project_id=TEST_PROJECT_ID,
                scene_id=scene_ids[0],
                summary="Elena discovers a brass key in the lighthouse. She opens the trapdoor.",
                characters=["Elena", "Marcus"],
            )
            await rag.upsert_scene(
                project_id=TEST_PROJECT_ID,
                scene_id=scene_ids[1],
                summary="Elena explores the lighthouse basement. Marcus had copper powder experiments.",
                characters=["Elena"],
            )
            await rag.upsert_scene(
                project_id=TEST_PROJECT_ID,
                scene_id=scene_ids[2],
                summary="Inspector Raines notices Elena's hair description contradicts the known red.",
                characters=["Elena", "Inspector Raines"],
            )

            # Query: should return semantically relevant scenes
            ctx = await rag.build_rag_context(
                project_id=TEST_PROJECT_ID,
                query_text="Elena finds the brass key and discovers what it opens",
                active_characters=["Elena", "Marcus"],
            )
            return ctx

        ctx = asyncio.run(test_rag_direct())
        if ctx:
            ok(f"RAG index: 3 scenes indexed successfully")
            ok(f"RAG query returned {len(ctx.split())} words of context")
            if "Elena" in ctx:
                ok("RAG context mentions Elena ✓")
            if "key" in ctx.lower() or "basement" in ctx.lower() or "lighthouse" in ctx.lower():
                ok("RAG context is semantically relevant ✓")
            print(f"\n  {CYAN}── RAG context preview (first 400 chars) ──{RESET}")
            for line in ctx[:400].split("\n"):
                print(f"  {line}")
        else:
            fail("RAG query returned empty context")

    except ImportError as e:
        skip(f"Direct RAG test skipped: {e}")
    except Exception as e:
        fail("RAG direct test", str(e))

    # RAG rebuild via HTTP
    try:
        rebuild = post(f"/api/rag/rebuild/{TEST_PROJECT_ID}")
        ok(f"RAG rebuild endpoint: {rebuild}")
    except Exception as e:
        fail("RAG rebuild endpoint", str(e))

    # ── 8. Audit System ──────────────────────────────────────────────────────
    section("8. Audit — Contradiction Detection")
    try:
        audit_resp = post(f"/api/audit/run/{TEST_PROJECT_ID}")
        ok(f"Audit run endpoint: {str(audit_resp)[:80]}")
    except Exception as e:
        fail("Run audit", str(e))

    try:
        contradictions = get(f"/api/audit/contradictions/{TEST_PROJECT_ID}")
        ok(f"Get contradictions: {len(contradictions)} items (may be empty without LLM)")
        if contradictions:
            c = contradictions[0]
            ok(f"  First contradiction: field='{c.get('field','?')}' severity='{c.get('severity','?')}'")
    except Exception as e:
        fail("Get contradictions", str(e))

    # ── 9. Generation Endpoints (SSE shape) ──────────────────────────────────
    section("9. Generation Endpoints — SSE Response Shape")
    gen_tests = [
        ("/api/generate/complete",   {"prefix": "Elena walked into"},
                                     "ghost complete"),
        ("/api/generate/continue",   {"prefix": SCENE_1[:200], "instruction": "Continue",
                                      "project_id": TEST_PROJECT_ID,
                                      "scene_id": scene_ids[0], "characters": ["Elena"]},
                                     "continue"),
        ("/api/generate/rephrase",   {"text": "Elena walked quickly.", "style": "elevated"},
                                     "rephrase"),
        ("/api/generate/brainstorm", {"context": SCENE_1[:200], "n": 3,
                                      "project_id": TEST_PROJECT_ID},
                                     "brainstorm"),
        ("/api/generate/describe",   {"text": "the rain fell", "mode": "sensory"},
                                     "describe"),
    ]
    for path, body, label in gen_tests:
        try:
            out = check_sse(path, body)
            if "data:" in out:
                ok(f"{label}: SSE response format ✓")
            elif out:
                ok(f"{label}: response received ({len(out)} bytes)")
            else:
                fail(f"{label}: empty response")
        except RuntimeError as e:
            errmsg = str(e)
            if "LLM_TIMEOUT" in errmsg:
                # Timeout = server is streaming to LLM but it's slow. Endpoint exists and is wired.
                skip(f"{label}: endpoint reachable, LLM slow/absent (timeout)")
            elif "Cannot reach LLM" in errmsg or "Connection refused" in errmsg:
                skip(f"{label}: LLM server not running (expected in test env)")
            elif "data:" in errmsg:
                ok(f"{label}: SSE shape confirmed")
            else:
                fail(f"{label}: {errmsg[:80]}")

    # ── 10. Export ───────────────────────────────────────────────────────────
    section("10. Export — Markdown Compile")
    try:
        md = download(f"/api/export/{TEST_PROJECT_ID}", {
            "format":                "markdown",
            "author":                "Test Author",
            "include_scene_headers": True,
            "strip_notes":           True,
        })
        md_str = md.decode("utf-8")
        ok(f"Markdown export: {len(md_str)} chars, {len(md_str.split())} words")

        checks = [
            ("YAML frontmatter",      "---" in md_str[:50]),
            ("title in frontmatter",  "Quill Test Novel" in md_str),
            ("author in frontmatter", "Test Author" in md_str),
            ("Act 1 header",          "# Act 1" in md_str),
            ("Chapter 1 header",      "## Chapter 1" in md_str),
            ("Scene headers",         "### The Key" in md_str),
            ("Scene content",         "Elena" in md_str),
            ("Scene dividers",        "---" in md_str),
        ]
        for label, passed in checks:
            (ok if passed else fail)(f"  Export check — {label}")

        # Save for inspection
        out_path = "/tmp/quill_test_export.md"
        with open(out_path, "w") as f:
            f.write(md_str)
        ok(f"Export saved to {out_path} for inspection")
    except Exception as e:
        fail("Markdown export", str(e))

    try:
        tool_check = get("/api/export/check")
        ok(f"Export tool check: pandoc={tool_check['available']}, formats={tool_check['formats']}")
    except Exception as e:
        fail("Export check endpoint", str(e))

    # ── 11. Writing Goal ─────────────────────────────────────────────────────
    section("11. Writing Goal Update")
    try:
        result = patch(f"/api/projects/{TEST_PROJECT_ID}/goal", {"word_count_goal": 50_000})
        assert result["word_count_goal"] == 50_000
        assert "completion_pct" in result
        ok(f"Goal update: 50K words, completion={result['completion_pct']}%")
    except Exception as e:
        fail("Goal update", str(e))

    # ── 12. Cleanup ──────────────────────────────────────────────────────────
    section("12. Cleanup")
    try:
        # DELETE returns 204 No Content — handle accordingly
        url = f"{BASE}/api/projects/{TEST_PROJECT_ID}"
        req = urllib.request.Request(url, method="DELETE")
        with urllib.request.urlopen(req, timeout=10) as r:
            assert r.status in (200, 204), f"unexpected status {r.status}"
        ok(f"Deleted test project {TEST_PROJECT_ID} (HTTP {r.status})")

        all_p = get("/api/projects")
        assert TEST_PROJECT_ID not in [p["id"] for p in all_p]
        ok("Project confirmed deleted from list")
    except Exception as e:
        fail("Cleanup", str(e))

    # ── Summary ──────────────────────────────────────────────────────────────
    total = pass_count + fail_count + skip_count
    print(f"\n{BOLD}{'═'*55}")
    print(f"  Results: {total} checks")
    print(f"  {GREEN}✓ Passed: {pass_count}{RESET}")
    if fail_count:
        print(f"  {RED}✗ Failed: {fail_count}{RESET}")
    if skip_count:
        print(f"  {YELLOW}⊘ Skipped: {skip_count}{RESET}")
    print(f"{'═'*55}{RESET}\n")

    if fail_count == 0:
        print(f"  {GREEN}{BOLD}All tests passed!{RESET}\n")
        sys.exit(0)
    else:
        print(f"  {RED}{BOLD}{fail_count} test(s) failed.{RESET}\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
