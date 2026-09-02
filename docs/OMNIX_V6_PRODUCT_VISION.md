# Omnix V6 — Product Vision

**Phase:** Architecture-Alignment Audit (post-Phase 0)
**Status:** Product-level north star
**Date:** 2026-08-29
**Audience:** Every contributor to Omnix V6.

---

## 1. The product in one sentence

**Omnix V6 is a general-purpose, natural-language, voice-capable AI desktop agent for Windows — a "Jarvis for the PC" that listens, understands, plans, acts on real applications, verifies what happened, recovers from failure, remembers what matters, and speaks back.**

It is not a chatbot with automation. It is not a command dispatcher. It is not a wrapper around PyAutoGUI. It is a coherent agent that lives on the user's computer and is reached primarily by talking to it.

---

## 2. What Omnix IS

Omnix V6 is, and only is, the following:

- A **persistent AI assistant** running on a Windows PC.
- A **voice-first** interaction surface ("Hey Omnix" + natural speech).
- A **text-capable** interaction surface (typed commands, terminal-style).
- A **general desktop agent** that operates real applications, real browsers, real windows, real files, real input.
- A **closed-loop agent** that observes → plans → acts → observes → verifies → recovers.
- A **natural-language understanding** system: the user does not need to speak in command templates.
- A **conversational partner** that handles questions, chit-chat, and requests for information without dragging automation into them.
- An **action executor** that handles single-step and multi-step real-world tasks.
- A **verifier** that distinguishes "I tried" from "it worked" and reports honestly.
- A **recoverer** that retries, switches strategy, re-observes, or asks the user when stuck.
- A **rememberer** with clear retention, relevance, and privacy policies.
- A **learner** of the user's preferences, patterns, and conventions.
- An **extensible platform** — additional capabilities, models, and perception strategies can be added without rewriting the agent.

---

## 3. What Omnix is NOT

Omnix V6 is **not**:

- ❌ A chatbot that happens to launch apps on demand.
- ❌ A collection of hard-coded command templates (`open_chrome`, `play_believer`).
- ❌ A voice assistant that interprets "Hey Omnix, open Chrome" as a fixed string match.
- ❌ An intent classifier that hands a label to a script.
- ❌ A wrapper around an LLM with PyAutoGUI bolted on.
- ❌ A monolithic engine that contains every piece of business logic.
- ❌ A V5 clone with V6 branding.
- ❌ A system that says "Task completed" when it only knows the action was attempted.
- ❌ A system that silently degrades automation success.
- ❌ A system that lets the LLM bypass safety.
- ❌ A system where every phase ships in isolation and integration happens "later."

---

## 4. The two interaction modes

A single, fundamental distinction drives the entire V6 architecture:

| Mode | Trigger | Output | Automation? |
|---|---|---|---|
| **Conversation** | Question, request for information, chit-chat, opinion, explanation | Natural-language answer (text + voice) | No |
| **Action / Task** | Imperative request, multi-step goal, command to do something | Plan, execution, observation, verification, natural-language report | Yes |

Examples:

- *"What is the difference between RAM and storage?"* → Conversation.
- *"Open Chrome and search for AI agents."* → Action.
- *"Can you help me prepare for my exam next week?"* → Action (multi-step, requires planning).
- *"Hey Omnix, I'm going to work on my assignment."* → Conversation (the user is announcing, not commanding).
- *"Open my browser, search for the best Python courses, and show me the results."* → Action (multi-step).
- *"Actually, go back and search for free Python courses instead."* → Action (modifies the current goal).

The system must decide which mode applies **per turn**, not per session. The same conversation can contain both.

---

## 5. Natural language is the interface

The user is **not** required to speak in command templates. The user can say:

- "Can you open Chrome for me?"
- "Hey Omnix, could you look up AI agents?"
- "Open Spotify and put on Believer."
- "I need to find that PDF I downloaded yesterday."
- "Can you close the window I'm currently using?"
- "What's on my screen?"
- "Open my browser, search for the best Python courses, and show me the results."
- "Actually, go back and search for free Python courses instead."

All of these must work. The system resolves intent, entities, and context. The capability system is deterministic and safe, but the **interface** to that capability system is natural language.

### What this means inside the engine

The user-facing interface is natural language only. Inside the engine, the natural-language input is **interpreted, not echoed**. The pipeline is:

```text
User language
    ↓
Semantic understanding (Brain / NLU)
    ↓
Goal
    ↓
Structured plan
    ↓
Capability calls
```

The capability calls are **internal implementation objects** — typed dataclasses with validated parameters, never serialized to the user, never documented as a user-facing API. The user types "Open Chrome and search for AI agents"; the Brain emits a plan that calls `open_application(app="Chrome")` then `search_browser(query="AI agents")`. The user never sees those strings, never types those strings, and never has to learn them.

The capability registry is the **type system** the Brain emits into and the `CapabilityRouter` validates against. It is not a CLI the user invokes.

---

## 6. Conversation is a first-class feature

Voice is not "voice → command → automation." Voice is "voice → natural language → understanding."

The user can talk to Omnix normally:

> "Hey Omnix."
> "Yes?"
> "What can you do?"
> "I can help you use your PC, open and control applications, search the web, work with files, read what's on your screen, and handle multi-step tasks."
> "Hey Omnix, I'm going to work on my assignment."
> "Sure. What would you like me to help with?"

This means V6 must support a real **conversation state**: turns, entities, referents, prior commitments, mood, etc.

---

## 7. Context is essential

Omnix must understand the context of the interaction. Examples:

> "Open Chrome." → Done.
> "Search for AI agents." → Done.
> "Open the first result." → [understands what "the first result" refers to]

> "Open Spotify." → "Spotify is open."
> "Play Believer." → [understands Spotify is the relevant application]

> "Click the blue button." → [uses current screen context to determine which button]
> "Close that." → [resolves "that" from current context]

V6 needs a meaningful concept of:

- conversation context (turns, referents, prior commitments)
- task context (current goal, current step, remaining steps)
- application context (which app is foreground, which window is focused)
- screen context (what is visible right now)
- action context (what was just attempted, what was the observed result)
- entity context ("the first result", "that window", "the blue button")
- user preferences (relevant, but private and revisable)

These do not collapse into a single "context blob." The `ContextService` is a thin coordinator over **five well-typed state containers**, each with a clear owner:

```text
ContextService
    ├── ConversationContext   — what was said
    ├── TaskState             — current task / goal / subgoal / plan / step / replan count / status / cancellation
    ├── WorldState            — foreground app / window / open apps / current URL / visible elements / last observation / last action / known entities
    ├── EntityContext         — what things have been mentioned and resolved
    └── UserContext           — user identity, preferences, authorization
```

`TaskState` is the agent's understanding of **the work in progress**. `WorldState` is the agent's understanding of **the computer right now**. The two are distinct because a plan refers to the work; the screen refers to the world.

---

## 8. General desktop agency

The goal is **not** to implement a fixed list of automation commands. The goal is to build the **infrastructure** that lets Omnix perform many different desktop tasks, including tasks it has never seen as an exact predefined command.

Example task:

> "Open Chrome, search for the best free Python courses, open the first result, and find the section about decorators."

The system should reason:

```
Goal: Find the decorators section in a Python course.

Possible plan:
1. Open/focus Chrome.
2. Navigate/search for Python courses.
3. Observe search results.
4. Identify appropriate result.
5. Open result.
6. Observe page.
7. Find decorators section.
8. Scroll if necessary.
9. Verify target section exists.
10. Report result.
```

The exact steps are **not** hard-coded as one giant skill. The agent **composes** available capabilities dynamically.

### Capability domains (not a fixed command list)

- **Applications**: open, close, switch, focus, list, launch-as-admin
- **Mouse**: click, double-click, right-click, move, drag, scroll
- **Keyboard**: type, backspace, enter, tab, hotkeys, shortcuts
- **Windows**: find, focus, move, resize, minimize, maximize, close
- **Files**: find, open, create, rename, move, copy, delete (when authorized)
- **Browser**: open, navigate, search, click links, fill forms, read pages
- **Media**: open, search, play, pause, skip, volume
- **Screen understanding**: read text, identify buttons, identify inputs, identify icons, locate elements, understand basic structure
- **System**: volume, brightness (where supported), process info, window state, clipboard, safe Windows operations

The agent picks and sequences these per task.

---

## 9. Observe → act → observe is fundamental

The architecture is **not**:

```
plan all actions → blindly execute
```

It is a closed loop:

```
OBSERVE → UNDERSTAND → PLAN → ACT → OBSERVE → VERIFY → CONTINUE/REPLAN/RECOVER
```

For UI automation specifically:

```
current screen → find target → perform action → observe screen again → determine whether expected state occurred → continue
```

This is one of the most important properties of Omnix and is **non-negotiable** in V6.

---

## 10. Action success is not goal success

This is a hard architectural rule. Examples:

`pyautogui.click(x, y)` returning without an exception does **not** mean "the button was clicked."

"Spotify launch command succeeded" does **not** mean "Spotify is open and Believer is playing."

The architecture must distinguish:

```
ACTION ATTEMPTED
    ↓
ACTION EXECUTED (no exception)
    ↓
EXPECTED STATE OBSERVED (vision / DOM / process / window)
    ↓
STEP VERIFIED (per-step verdict)
    ↓
GOAL VERIFIED (overall goal verdict)
```

The user must never receive "Task completed" when Omnix only knows it attempted the action.

**Preserve** the verification invariants that the V5 test suite already exercises (`test_verification_recovery.py`).

---

## 11. Recovery is core, not optional

Real desktop automation fails. Examples:

- Application takes longer to open.
- Button moved.
- Window was not focused.
- Element not detected.
- Website changed.
- Click missed.
- Keyboard input went to the wrong window.
- Network request failed.
- Vision model was uncertain.

Omnix must be designed to recover:

```
PLAN
 → CLICK SEARCH BOX
 → VERIFY
 → FAILED
 → RECOVER
 → OBSERVE SCREEN
 → LOCATE SEARCH BOX AGAIN
 → TRY ALTERNATIVE STRATEGY
 → VERIFY
 → CONTINUE
```

Recovery strategies include:

- Wait.
- Re-observe.
- Refocus a window.
- Re-locate an element.
- Use a different locator.
- Use keyboard navigation.
- Use browser automation instead of coordinate clicking.
- Replan the remaining steps.
- Ask the user when stuck.

---

## 12. Vision is Omnix's eyes

Vision is **not** an isolated "YOLO feature." It is part of the agent's perception system.

Conceptually:

```
SCREEN
 → CAPTURE
 → OCR / TEXT
 → UI DETECTION
 → OBJECT DETECTION
 → UI HIERARCHY (UIA)
 → ELEMENT LOCATION
 → STRUCTURED SCREEN STATE
 → AGENT
```

The agent should be able to reason:

- "There is a Chrome window."
- "The search box is near the top."
- "There is a blue button labeled Save."
- "The Spotify play button is visible."
- "The page appears to have changed."

**Multiple perception strategies must coexist.** Not every action uses YOLO:

```
UI Automation (UIA)
Browser DOM
Accessibility tree
OCR
Vision
Window APIs
Keyboard navigation
Coordinate click
```

**No single ordering of these strategies is universally correct.** A `PerceptionRouter` selects the most reliable available strategy for the current application, target, action, context, and evidence. Sensible defaults exist (DOM-first for browsers, UIA-first for native Windows, OCR for text-heavy UIs, vision for unknown or custom UIs, coordinates as a last resort), but the router is adaptive, not hard-coded. The router records the choice and the reason, so a failure in the chosen strategy can fall back intelligently.

---

## 13. Windows automation is real

The final Omnix genuinely controls a Windows PC.

- "Open Chrome." → actually opens Chrome.
- "Open Notepad and type hello." → actually opens Notepad and types.
- "Open Spotify and play Believer." → actually attempts the whole task and verifies whether playback started.
- "Click Save." → locates the correct element and verifies the resulting state.

V6 must support real:

- process control
- application launching
- window management
- keyboard
- mouse
- clipboard
- filesystem
- browser interaction
- UI Automation
- screen capture
- OCR
- vision

---

## 14. Voice is first-class

```
HEY OMNIX
 → LISTEN
 → SPEECH-TO-TEXT
 → NATURAL LANGUAGE UNDERSTANDING
 → CONVERSATION OR TASK
 → RESPONSE
 → TEXT + SPEECH
```

The user does not manually switch between chat mode, automation mode, voice mode. The system decides.

Examples:

- "Hey Omnix, what is Python?" → conversation
- "Hey Omnix, open VS Code." → task
- "Hey Omnix, open VS Code and create a new Python file." → multi-step task
- "Hey Omnix, I can't find my assignment PDF." → reasoning + search goal
- "Hey Omnix, remember that my assignment files are usually in Downloads." → memory update

---

## 15. Memory supports the agent

Memory is not added because "AI assistants need memory." Memory has **useful purposes**:

- user preferences
- frequently used applications
- useful file locations
- previous task context
- relevant conversation context
- known UI patterns
- user-approved persistent facts

Memory improves future interactions. But memory has clear policies:

- **storage** (where, how)
- **retrieval** (when, by what signal)
- **relevance** (what counts as relevant)
- **retention** (how long, when to forget)
- **deduplication** (no duplicate facts)
- **privacy** (user can inspect, edit, delete)
- **deletion/update** (user can correct Omnix's memory)
- **confidence** (facts have confidence scores)

**Uncontrolled memory growth is forbidden.**

---

## 16. The AI Brain

The Brain is responsible for **reasoning** and **natural-language understanding**. It does not control the mouse or keyboard.

The conceptual separation:

```
BRAIN        — "What does the user want?"
PLANNER      — "How can we accomplish it?"
AGENT        — "What should I do next, given current state?"
CAPABILITY   — "Perform this specific operation."
SYSTEM       — "Actually interact with Windows."
VISION       — "What changed on the screen?"
VERIFICATION — "Did it work?"
```

The Brain is **replaceable**. OpenRouter, Groq, local models, fine-tuned models, future multimodal models — all are implementation/provider decisions. The product goal is independent of provider.

V6 must support **provider switching at runtime** without rewriting skills, planners, or capabilities.

**The Brain can only invoke registered capabilities.** The Brain may reason, plan, and select — but the set of operations the agent can perform is **closed**: it is the set of capabilities registered with the `CapabilityRouter`. The Brain may not invent arbitrary executable operations. A "code execution" capability that the LLM can drive without explicit registration is forbidden by default; if it is ever allowed, it must be registered, parameterized, gated by the safety policy, and fully logged. The capability registry is the boundary between "the agent that reasons" and "the agent that acts."

---

## 17. Skills are capabilities, not the brain

A skill is a small, reusable, deterministic **capability**. Examples:

- `OpenApplication`
- `ClickElement`
- `TypeText`
- `SearchBrowser`
- `ReadScreen`
- `FindFile`
- `PlayMedia`
- `FocusWindow`

**Bad architecture:**

```
skill = "open_chrome_and_search_and_click_and_verify"
```

**Good architecture:**

```
Capabilities:
  open_application
  focus_window
  navigate_browser
  type_text
  click_element
  read_screen
  verify_state
```

The agent **composes** these dynamically. Skills never contain the intelligence that determines the entire task.

---

## 18. Hybrid browser automation

Browser automation must **not** depend entirely on vision. A hierarchy:

```
Browser DOM / Selenium   (most reliable when available)
   ↓
Accessibility / UI Automation
   ↓
OCR / structured text
   ↓
Vision (YOLO + UI detection)
   ↓
Coordinate fallback
```

The exact strategy is chosen per application and per task.

---

## 19. Multi-modal future

V6's architecture must not block future capabilities:

- richer computer vision
- better OCR
- local multimodal models
- local LLMs
- better browser agents
- document understanding
- image understanding
- richer memory
- external tools
- additional Windows APIs

Do not over-engineer these now. But do not write architecture that makes them impossible.

---

## 20. Safety

Omnix controls the user's computer. Safety is non-negotiable.

**Low-risk actions** (allowed without confirmation):

```
open application
search web
read screen
type text
play media
move window
```

**Potentially destructive actions** (require safeguards / confirmation):

```
delete file
terminate process
change system configuration
shutdown / restart
modify important files
send / submit externally
install software
modify registry
```

The agent **never** bypasses safety because the LLM suggested the action. The safety policy is enforced **below** the agent, not above it.

---

## 21. V5 is reference, not specification

V5 is the **existing implementation**. V5 is **not** the final spec.

If V5 contains:

- architectural limitations
- incomplete automation
- weak verification
- hard-coded behavior
- placeholder systems
- poor abstractions
- unreliable execution
- missing capabilities

**do not blindly migrate them.** Instead:

```
V5 proven implementation
+ V6 architecture
+ Final product vision
= V6 implementation
```

- **Preserve** useful functionality.
- **Redesign** broken functionality.
- **Improve** weak functionality.
- **Remove** obsolete functionality.

**Migrate capabilities, not files.**

---

## 22. No giant monolith

V5's `core/omnix_engine.py` is 3,637 lines. V6 must **not** reproduce that. The engine is the orchestrator, not the location of every piece of business logic.

Clear boundaries:

```
Engine        coordinates
Brain         reasons
Planner       decomposes goals
Agent         decides next action
Context       gathers state
Memory        stores / retrieves
Vision        perceives
Capabilities  perform operations
Execution     drives the action
System        touches Windows
Voice         hears / speaks
Verification  judges outcomes
Recovery      recovers
Events        integrates
```

The engine **coordinates** them. It does not **implement** them.

---

## 23. Target architecture (conceptual)

```
                          USER
                            │
                ┌───────────┴───────────┐
                │                       │
              VOICE                    TEXT
                │                       │
                └───────────┬───────────┘
                            ↓
                  NATURAL LANGUAGE
                   UNDERSTANDING
                            ↓
                      OMNIX ENGINE
                            │
              ┌─────────────┼─────────────┐
              ↓             ↓             ↓
           MEMORY        CONTEXT         BRAIN
              │             │             │
              └─────────────┼─────────────┘
                            ↓
                    INTENT / GOAL
                    UNDERSTANDING
                            ↓
                         PLANNER
                            ↓
                          AGENT
                            ↓
                  CAPABILITY ROUTER
                            ↓
                    EXECUTION LAYER
                            ↓
           ┌────────────────┼────────────────┐
           ↓                ↓                ↓
        WINDOWS          BROWSER         FILESYSTEM
         INPUT           CONTROL          CONTROL
           │                │                │
           └────────────────┼────────────────┘
                            ↓
                       OBSERVATION
                            ↓
                          VISION
                            ↓
                       VERIFICATION
                            ↓
                  ┌─────────┴─────────┐
                  │                   │
                SUCCESS             FAILURE
                  │                   │
                  ↓                   ↓
               RESPONSE          RECOVERY
                                      │
                                      ↓
                                   REPLAN
                                      │
                                      └──────→ AGENT

                         RESPONSE
                            ↓
                     TEXT + VOICE
                            ↓
                           USER
```

This is conceptual, not a demand that every box become a separate Python package. But every box must be a **clear responsibility** in the code.

---

## 24. Sequential development ≠ sequential product

We can develop Omnix in phases. But the final architecture must remain **integrated**. Every phase must move toward:

```
NATURAL LANGUAGE
   ↓
UNDERSTAND
   ↓
PLAN
   ↓
ACT
   ↓
OBSERVE
   ↓
VERIFY
   ↓
RECOVER / REPLAN
   ↓
RESPOND
```

No phase ships an isolated feature that later becomes impossible to connect. The **whole loop** is the deliverable, not pieces of it.

---

## 25. Success criteria for the final Omnix

V6 is **not** complete when "all V5 files migrated." V6 is complete when Omnix can reliably demonstrate the scenarios in `docs/V6_ACCEPTANCE_TESTS.md` — including:

- Conversation (free-form chat)
- Application control (open / close / focus)
- Compound task ("open Chrome and search for AI agents")
- UI automation ("open Notepad, type hello world, save it")
- Media ("open Spotify and play Believer")
- Browser ("search, open first result, navigate")
- Screen understanding ("what is on my screen?")
- Context (multi-turn referring to prior context)
- Recovery (when an action fails, recover or ask)
- Voice (full voice loop on real Windows)

The acceptance tests are the contract. The implementation serves the contract.

---

## 26. Invariants (binding)

These are **non-negotiable** in V6:

1. **Conversation and action are distinguished per turn.** No single mode for the whole session.
2. **Natural language is the user-facing interface.** No command templates required from the user.
2a. **The internal interface is structured capability calls.** The Brain emits typed capability calls; the `CapabilityRouter` validates them. The capability set is the type system, not a CLI.
3. **Observe → act → observe is the only loop.** No "plan all, execute blind."
4. **Action success is not goal success.** The verifier can override the executor.
5. **Recovery is core.** Every step has a recovery path.
6. **Verification is mandatory.** No `success=True` without evidence.
7. **Safety is below the agent.** The LLM cannot bypass it.
8. **The brain is replaceable.** No code outside `ai/` may call an LLM provider.
9. **Memory has policy.** Retention, dedup, privacy, deletion.
10. **Voice is first-class.** The user does not switch modes for voice.
11. **Vision is one of several perception strategies.** Not the only one.
11a. **Perception strategy is selected by router, not by universal ordering.** `DOM > UIA > OCR > Vision > Coordinates` is one example for some apps; it is not a fixed rule.
12. **Browser automation is hybrid and adaptive.** Strategy is chosen per (app, target, action, context).
13. **Skills are small, composable capabilities.** Not whole-task scripts.
14. **The engine is the orchestrator.** Not the implementation site of all logic.
15. **The architecture enables future capabilities.** Multi-modal, local LLM, document understanding, etc.
16. **V5 is reference, not spec.** Migrate capabilities, not files.
17. **Every phase moves toward the integrated loop.** No isolated features.
18. **The Brain can only invoke registered capabilities.** It may reason, but the executable operation set is closed and validated by the `CapabilityRouter`.
19. **Context is owned, not dumped.** `ContextService` coordinates five typed containers (`ConversationContext`, `TaskState`, `WorldState`, `EntityContext`, `UserContext`), each with a clear owner; nothing bypasses the coordinator.
20. **The agent is honest about generalization.** The acceptance tests are the contract; the open-ended agent benchmarks are the generalization gate.

---

**OMNIX V6 PRODUCT VISION ALIGNED. NO SOURCE CODE MODIFIED. NO DEPENDENCIES INSTALLED. WAITING FOR USER APPROVAL BEFORE PHASE 0.5.**
