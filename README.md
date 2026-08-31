# nihongo

A lightweight CLI for learning Japanese: kana reading fluency first, then
GPT-guided conversational grammar and vocabulary. Runs locally, keeps its
state in a single SQLite file, and picks up exactly where you left off every
time you run it.

## Why it's split in two

Kana recognition is rote — teach, drill, read, quiz, repeat — and doesn't
need a language model in the loop. So that half runs as a fully **offline,
deterministic engine**: zero API calls, zero latency, zero cost. Once kana is
solid, conversation and grammar genuinely benefit from a real model, so that
half is a **GPT-backed tutor** that holds an actual conversation with you.

```
nihongo kana      → offline: hiragana, katakana, basic kanji reading
nihongo lesson     → GPT: grammar, vocab, conversation practice (after kana)
```

## Commands

| Command | What it does |
|---|---|
| `nihongo` | Status dashboard — current unit, streak, what's due |
| `nihongo kana` | Run a kana practice session (review → teach → drill → reading → quiz), looping across as many rounds as you want in one sitting |
| `nihongo curriculum` | Full course map: every batch's status and mastery stats, plus a checklist of exactly what's blocking the batch you're on |
| `nihongo lesson` | Interactive GPT conversation for the current unit (grammar/vocab/conversation, once past kana) |
| `nihongo review` | Self-graded spaced-repetition review of vocab/grammar learned in lessons |

## The kana engine

`nihongo kana` works through 47 batches — all of hiragana and katakana
(base characters, dakuten/handakuten, yōon combinations, small っ, long
vowels), common katakana loanwords, and ~15 foundational kanji — in a fixed
order, one batch unlocked at a time.

Each round is: **review** (weak/older characters resurface first, spaced by
priority) → **new material** (only shown once per batch; later rounds just
reinforce anything still shaky) → **drill** (20 prompts split 12 current /
6 weak-prior / 2 spaced-prior) → **reading** (real words, not isolated
characters) → **quiz** (10 prompts, no feedback until the end). Presentation
order is shuffled every round so you can't pattern-match a fixed sequence —
only the underlying selection logic (what to test, weak-character priority)
stays deterministic.

A batch only unlocks the next one once **all** of these hold:

- Attempts recorded across 2+ separate practice sessions (not just one round)
- At least 10 scored attempts in 2+ of those sessions
- Every character in the batch has 4+ lifetime attempts
- 90%+ accuracy over the most recent 20 attempts
- Every character individually got 3-of-last-4 attempts correct
- That session's 5-word reading check scores 4/5

`nihongo curriculum` shows this checklist live for whichever batch you're
currently on, so you always know exactly what's holding you back.

The curriculum content and mastery algorithm in `nihongo/data/kana_curriculum.json`
were generated externally (via ChatGPT, from a prompt describing the desired
teaching strategy) and are treated as data, not something the app improvises —
`kana_engine.py` just executes it exactly.

## Setup

```bash
git clone https://github.com/YarboJanks/nihongo.git
cd nihongo
python3 -m venv .venv && source .venv/bin/activate
pip install -e .

cp .env.example .env
# edit .env and set OPENAI_API_KEY (only needed for `nihongo lesson`/`nihongo review`;
# `nihongo kana` and `nihongo curriculum` never call the API)
```

Requires Python 3.10+. Progress lives in `~/.nihongo/nihongo.db` by default
(override with `NIHONGO_DB_PATH`).

## Design notes

- **No server, no daemon.** Every command opens the database, does its thing,
  and closes it — so it doesn't matter which machine or terminal you run it
  from, there's no session to hand off.
- **Alternate screen buffer.** Both `nihongo kana` and `nihongo lesson` run
  inside the terminal's alt-screen (the same mechanism `vim`/`htop` use), so
  nothing from a session lingers in your scrollback — only a short summary
  prints once you're done.
- **Tool-calling for structured state.** The GPT tutor uses OpenAI function
  calling (`record_vocab`, `complete_unit`) to persist what it teaches,
  rather than trying to parse its own conversational output.
