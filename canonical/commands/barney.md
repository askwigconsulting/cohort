---
name: barney
kind: command
scope: global
description: Explain something Barney style — so simple, so step-by-step, that nobody can get it wrong
targets:
- claude
invocation: barney
args:
- name: topic
  required: false
  description: What to explain (defaults to the thing currently under discussion)
dry_run: true
---
Explain **Barney style**: so simple and so step-by-step that nobody can do it wrong.

The phrase comes from *Barney & Friends*, where ideas are taught to small children. The
military borrowed it for briefings that must survive stress, noise, and a tired reader at
3am. That is the bar: not "explained well", but **impossible to get wrong.**

Barney style is **not** condescending, and it is not baby talk. Never say "it's simple" or
"just" — if it were simple the request would not exist, and "just" is where people give up.
You are lowering the *reading* difficulty, never the *truth*.

## 1. Find the one idea

Before writing, answer for yourself: **what is the single thing they must understand?**
Everything else is detail that hangs off it. If you cannot name that one thing in a sentence,
you do not understand it well enough to explain it yet — go read the actual code, doc, or
system until you can.

Then decide what to leave out. **What you omit is the whole skill.** A Barney explanation
that mentions every exception is just the original complexity with shorter words.

## 2. Anchor to something they already know

Start from a thing the reader has touched before — a lock on a door, a queue at a counter,
a filing cabinet — and move one step at a time toward the real thing. An analogy is a bridge
you walk across and then leave behind; it is not the explanation itself.

**Say where the analogy breaks.** Every analogy is wrong somewhere, and the place it breaks
is usually where the bugs live. "It's like a lock on a door — except this door can be opened
by anyone who copies the key, and copying is free."

## 3. Build the ladder

Order the steps so that **each one only uses things already explained**. This is the rule
that makes an explanation impossible to get wrong: a reader who follows in order never meets
a word they have not been given.

- Short sentences. One idea each.
- Concrete over abstract: real names, real numbers, real commands — not "a value" or "a resource".
- Name things the way the reader will see them on screen, so they can match your words to
  what is in front of them.
- Cut every word that survives deletion without loss of meaning.

## 4. Show the thing happening

An explanation without an example is a definition. Give the smallest complete example that
actually runs or actually happened — a real command with its real output, a real sequence of
events. If there is a "before" and an "after", show both.

## 5. Say what happens when it goes wrong

The reader will meet the failure before they meet the theory. So name the two or three ways
this normally breaks, what each one looks like, and what to do about it. **A step someone can
perform but cannot verify is not finished** — tell them how to check they got it right.

## 6. Check it

Reread it as someone who has never seen this system, and ask:

- Is there a word here I have not defined?
- Could someone follow this exactly and still end up somewhere wrong?
- Did I say what to do when it fails?
- Did I quietly make something *sound* simpler than it is?

That last one is the real failure. **Never trade accuracy for simplicity.** If something is
genuinely dangerous, irreversible, or uncertain, that fact survives the simplification —
say it plainly in short words rather than dropping it. Simplifying a risk away is the one
Barney-style mistake that hurts people; "you cannot undo this" is already Barney style.

## 7. Close

End with the one idea again, in one sentence, plus the single next action if there is one.
If any real complexity was set aside to keep the explanation clean, say so in a line — where
it lives and when they will need it — so the reader knows the map is simplified, and knows
where the edge of it is.
