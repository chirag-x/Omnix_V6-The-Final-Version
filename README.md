# OMNIX V6

## The Goal

**Omnix is a voice-first, natural-language AI desktop agent designed to understand human goals, reason about them, operate a Windows computer, observe the result of its actions, verify success, recover from failures, remember relevant context, and communicate naturally with the user.**

Omnix is not intended to be a simple chatbot.

It is not intended to be a collection of hard-coded automation scripts.

It is not intended to require the user to learn special commands.

The ultimate goal is to create an AI system that allows a person to interact with their computer primarily by **telling Omnix what they want accomplished**.

---

# Vision

The long-term Omnix experience should feel like this:

```text
                    USER
                      │
              Natural Voice / Text
                      │
                      ▼
               ┌─────────────┐
               │    OMNIX    │
               │             │
               │ Understand  │
               │ Reason      │
               │ Plan        │
               │ Act         │
               │ Observe     │
               │ Verify      │
               │ Recover     │
               │ Remember    │
               └──────┬──────┘
                      │
          ┌───────────┼───────────┐
          ▼           ▼           ▼
       Windows      Browser      Files
       Apps         Web          System
          │           │           │
          └───────────┼───────────┘
                      ▼
                 Real Result
                      │
                      ▼
              Natural Response
                      │
                      ▼
                    Voice
                      │
                      ▼
                    USER
```

The user should be able to describe a **goal**, rather than manually describe every computer operation required to achieve it.

---

# The Ultimate User Experience

When Omnix starts, the user should not need to stare at a terminal.

After the required subsystems become ready, Omnix should naturally announce itself:

> **"Omnix is ready. How can I help you?"**

The user can then speak naturally.

For example:

> "Open Chrome and search for the best AI agent frameworks."

Omnix should understand the request, create an appropriate plan, perform the required computer actions, observe what happened, verify the result, and communicate progress.

For example:

> "I'm opening Chrome."

> "Chrome is open. I'm searching for AI agent frameworks."

> "I found the results. I'm opening the second one."

If something fails, Omnix should not blindly assume success.

It should observe the environment, determine what went wrong, attempt an appropriate recovery strategy when possible, and honestly report the result.

---

# Core Philosophy

## 1. Goal-oriented, not command-oriented

Omnix should understand requests such as:

```text
"Open Notepad and write Hello World."
```

rather than requiring:

```text
open_app notepad
type_text "Hello World"
```

The user describes the desired outcome.

Omnix determines the necessary actions.

---

## 2. One brain, one agent, one execution pipeline

Omnix should not become a collection of disconnected brains and automation pipelines.

The intended architecture is:

```text
                         USER
                           │
                           ▼
                    Voice / Text
                           │
                           ▼
                     System 2
              Understanding / Routing
                           │
                           ▼
                         Brain
                 Reasoning / Planning
                           │
                           ▼
                         Agent
                Execution / Coordination
                           │
                           ▼
                  Capability Router
                           │
          ┌────────────────┼────────────────┐
          ▼                ▼                ▼
       Windows          Browser           Files
       Capabilities     Capabilities      Capabilities
          │                │                │
          └────────────────┼────────────────┘
                           │
                           ▼
                      Perception
                    UIA / OCR / Vision
                           │
                           ▼
                      Verification
                           │
                    ┌──────┴──────┐
                    ▼             ▼
                 Success       Failure
                    │             │
                    │          Recovery
                    │             │
                    └──────┬──────┘
                           ▼
                     Final Response
                           │
                           ▼
                          TTS
                           │
                           ▼
                         USER
```

Every subsystem should contribute to this single lifecycle.

---

# 3. Natural language should be the interface

Users should not need to know Omnix's internal capabilities.

The system should eventually understand requests such as:

```text
"Open Chrome."

"Could you open Chrome for me?"

"Search for AI agents."

"Open Notepad, write this message, and save it on my desktop."

"Find the PDF I downloaded yesterday and open it."

"Open my Omnix project in VS Code."

"Go back and search for something else."

"What's on my screen?"

"Click the blue button."

"Stop what you're doing."
```

These are goals and natural interactions, not rigid API commands.

---

# 4. Omnix must actually operate the computer

The purpose of Omnix is not merely to explain how a user can perform an action.

Omnix should perform the action itself when it has the necessary capability and authorization.

For example:

```text
User:
"Open Notepad and type Hello Omnix."

Omnix:

Understand
    ↓
Resolve Notepad
    ↓
Launch Notepad
    ↓
Observe
    ↓
Verify Notepad is available
    ↓
Focus Notepad
    ↓
Type text
    ↓
Observe
    ↓
Verify text
    ↓
Report success
```

The same principle applies to browsers, applications, files, and other supported computer interactions.

---

# 5. Observe after acting

Omnix should not rely on blind automation.

Weak automation:

```text
click()
sleep(2)
assume_success()
```

Desired Omnix behavior:

```text
OBSERVE
   ↓
DECIDE
   ↓
ACT
   ↓
OBSERVE AGAIN
   ↓
VERIFY
   ↓
SUCCESS?
 ┌─┴─┐
YES  NO
 │    │
 ▼    ▼
Next RECOVER
```

Every important action should have an appropriate verification strategy.

---

# 6. Recovery is part of intelligence

Real computers are unpredictable.

Windows applications can:

* open slowly
* change their UI
* lose focus
* display dialogs
* fail to respond
* expose incomplete UI information
* move controls
* produce unexpected states

Browsers can:

* fail to load
* navigate somewhere unexpected
* change page structure
* display consent dialogs
* lose sessions
* change layouts

Omnix should therefore be capable of:

```text
Action fails
     ↓
Observe current state
     ↓
Determine likely cause
     ↓
Choose recovery strategy
     ↓
Retry / re-plan / re-ground
     ↓
Verify
```

Omnix should prefer recovery over immediately giving up when recovery is safe and reasonable.

---

# 7. Perception should be layered

Omnix should use the strongest appropriate perception method available.

The intended hierarchy is:

```text
                 Perception
                     │
          ┌──────────┼──────────┐
          ▼          ▼          ▼
         UIA        OCR       Vision
          │          │          │
          └──────────┼──────────┘
                     ▼
              Grounded Target
                     │
                     ▼
                   Action
```

### UI Automation

Use structured accessibility/UI information when available.

### OCR

Use text recognition when visible text is useful for grounding.

### Vision

Use visual understanding for interfaces that cannot be reliably understood through structured UI information alone.

### Coordinate interaction

Coordinates may be used when necessary, but they should not become the primary intelligence mechanism.

---

# 8. Omnix should support unfamiliar applications

The long-term goal is not to create an endless list of application-specific scripts.

Instead of:

```text
if app == Chrome:
    ...
elif app == Notepad:
    ...
elif app == VSCode:
    ...
```

Omnix should increasingly understand applications through:

* application discovery
* application metadata
* Windows UI Automation
* accessibility information
* OCR
* visual perception
* process/window state
* browser DOM/accessibility information
* general computer-use strategies

The goal is **generalized computer use**, not an ever-growing collection of hard-coded macros.

---

# 9. Multi-step tasks are first-class

Omnix should eventually handle requests that require many dependent actions.

Example:

```text
"Open Chrome, search for AI agent frameworks,
compare the first three results, and tell me
which one looks best."
```

The desired lifecycle is:

```text
Understand goal
      ↓
Decompose task
      ↓
Create plan
      ↓
Execute step
      ↓
Observe
      ↓
Verify
      ↓
Execute next step
      ↓
Observe
      ↓
Reason over results
      ↓
Continue / recover
      ↓
Final response
```

A task should not be considered complete merely because the planned commands were issued.

It should be considered complete when the desired outcome has been sufficiently verified.

---

# 10. Conversation and computer control must coexist

Omnix should know when a user is simply talking and when they are asking it to perform a task.

For example:

```text
"What are AI agents?"
```

is primarily a conversational request.

Whereas:

```text
"Open Chrome and search for AI agents."
```

is a computer-use task.

And:

```text
"Actually, search for autonomous AI agents instead."
```

may modify an existing task or conversation context.

The system should preserve appropriate context so that follow-up instructions make sense.

---

# 11. Context and memory

Omnix should eventually maintain useful context across interactions.

For example:

```text
User:
"Open Chrome."

User:
"Search for AI agents."

User:
"Open the second result."
```

The final request should be understood in the context of the active interaction.

Omnix should combine:

```text
Current conversation
+
Current task state
+
Relevant memory
+
Environment state
+
Current computer observation
```

to make decisions.

Memory should be purposeful rather than indiscriminate.

---

# 12. Honest uncertainty

Omnix should never pretend an action succeeded when it cannot verify that it succeeded.

If multiple applications match:

> "I found two applications matching that name. Which one do you mean?"

If a save operation cannot be verified:

> "I couldn't verify that the file was saved."

If the computer state is ambiguous:

> "I'm not sure which window you mean. Can you clarify?"

Reliable uncertainty is preferable to fabricated success.

---

# 13. Safety

Omnix should never become:

```text
LLM → unrestricted shell access
```

The intended architecture is:

```text
User Goal
    ↓
Reasoning
    ↓
Plan
    ↓
Safety / Policy
    ↓
Capability Router
    ↓
Controlled Capability
    ↓
Execution
    ↓
Verification
```

The reasoning system decides what needs to happen.

Controlled capabilities determine how that action is safely executed.

Sensitive, destructive, irreversible, or ambiguous actions should have appropriate confirmation or safety policies.

---

# Voice-First Architecture

Voice is not intended to be a cosmetic layer added to the end of Omnix.

It should become a primary interaction channel.

The desired loop is:

```text
Wake
 ↓
Listen
 ↓
Speech-to-text
 ↓
Understand
 ↓
Plan
 ↓
Execute
 ↓
Observe
 ↓
Verify
 ↓
Respond
 ↓
Text-to-speech
 ↓
Listen again
```

Omnix should also support:

* wake-word activation
* speech interruption / barge-in
* task progress narration
* concise spoken updates
* sleep/wake behavior
* cancellation
* natural spoken responses

The user should not have to monitor the console while Omnix works.

---

# Example: The Desired Omnix Experience

A mature Omnix session should look approximately like this:

```text
OMNIX:
"Omnix is ready. How can I help you?"

USER:
"Open Chrome and search for the best AI agent frameworks."

OMNIX:
"I'm opening Chrome."

[Chrome opens]

OMNIX:
"Chrome is open. I'm searching for AI agent frameworks."

[Search performed]

OMNIX:
"I found the results. I'm checking the top options."

[Results observed and analyzed]

OMNIX:
"The first result looks like the strongest match because
it focuses on autonomous agent workflows."

USER:
"Open it."

OMNIX:
"Opening it now."

[Page opens]

OMNIX:
"Done."
```

The exact wording can vary.

The important property is the **closed loop between understanding, action, observation, verification, and communication**.

---

# Current V6 Direction

Omnix V6 already contains many of the architectural foundations required for this vision:

* OmnixEngine
* centralized service architecture
* capability routing
* planning
* Agent execution
* multi-step execution
* application discovery
* Windows interaction
* browser automation
* UI Automation
* OCR
* visual perception infrastructure
* verification
* recovery
* memory/context infrastructure
* voice services
* TTS
* speech queue
* cancellation
* readiness reporting
* safety boundaries

The current priority is therefore **integration and reliability**, not endless addition of isolated subsystems.

---

# Current Development Priority

The next major objective should be:

## V6 Integration & End-to-End Hardening

The priority order is:

```text
1. Integrate System 2 into the real production pipeline
                         ↓
2. Make OmnixEngine the authoritative entry point
                         ↓
3. Fix local-first → engine integration
                         ↓
4. Establish reliable end-to-end computer use
                         ↓
5. Make Notepad a reliable golden-path test
                         ↓
6. Make Chrome/browser automation reliable
                         ↓
7. Fix real voice-runtime issues
                         ↓
8. Harden perception and verification
                         ↓
9. Harden recovery
                         ↓
10. Establish real Windows E2E acceptance tests
```

Only after this foundation is reliable should Omnix aggressively expand toward generalized autonomous computer use.

---

# Golden-Path Acceptance Tests

The ultimate system should eventually pass real end-to-end tests such as:

```text
"Open Notepad."
```

Expected:

```text
Notepad opens
+
Omnix verifies it
```

```text
"Open Notepad and type Hello Omnix."
```

Expected:

```text
Notepad opens
+
text is entered
+
text is verified
```

```text
"Open Notepad, type Hello Omnix,
and save it to my desktop."
```

Expected:

```text
Application opens
+
text entered
+
save operation performed
+
file existence/content verified
```

```text
"Open Chrome and search for AI agents."
```

Expected:

```text
Chrome opens
+
search performed
+
results observed
+
success verified
```

```text
"Open Chrome, search for AI agents,
and open the second result."
```

Expected:

```text
Chrome opens
+
search
+
observe results
+
ground second result
+
click
+
verify destination
```

```text
"What's on my screen?"
```

Expected:

```text
Current screen observed
+
relevant content understood
+
natural response
```

```text
"Stop."
```

Expected:

```text
Current task cancelled safely
+
speech interruption when appropriate
+
state finalized
```

These tests should run through the **real Omnix runtime**, not merely isolated unit tests.

---

# Definition of Success

Omnix should eventually reach the point where a user does not need to ask:

> "Which command does Omnix support?"

Instead, they should be able to think:

> **"I'll just tell Omnix what I need."**

The ultimate success criterion is:

```text
Natural Goal
     ↓
Omnix Understands
     ↓
Omnix Reasons
     ↓
Omnix Plans
     ↓
Omnix Acts
     ↓
Omnix Observes
     ↓
Omnix Verifies
     ↓
Omnix Recovers if necessary
     ↓
Omnix Remembers relevant context
     ↓
Omnix Responds naturally
     ↓
User gets the desired result
```

---

# North Star

> **Omnix is a voice-first AI computer-use agent whose job is to turn natural human goals into verified real-world computer outcomes.**

Every future Omnix feature should be evaluated against this principle.

If a feature makes Omnix more capable of:

* understanding goals,
* reasoning,
* planning,
* operating computers,
* perceiving environments,
* verifying outcomes,
* recovering from failures,
* remembering useful context,
* communicating naturally,
* or operating safely,

then it moves Omnix toward the goal.

If it creates another disconnected execution path, another hard-coded automation system, or another subsystem that does not integrate into the canonical pipeline, it should be reconsidered.

**The goal is not to build more pieces.**

**The goal is to make all the pieces behave like one intelligent agent.**
