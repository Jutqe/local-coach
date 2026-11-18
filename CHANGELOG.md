# Changelog

Versions are those of the private development line this repository is generated from. Dates are 2026.

## 5.11 (Nov 18)
- `/rule text`, `/rules`, `/rule delete N`: your own rules for the coach. Stored in `rules.md` in the working folder, added to the chat instructions (never above the safety rules), available in the terminal chat and the web chat. Nothing is trained: only the instructions change.
- The English prompt version is now `4.10-en`: the profile prompts ask for "the author"/"they" with an example, because the 9B model wrote "he" for an author of unknown gender.

## 5.10 (Nov 18)
- Language button in the web settings: pick Russian or English, the choice is saved in the working folder (`lang`), the service restarts itself under systemd and the page reloads in the new language. `LOCAL_COACH_LANG`, when set, still wins and locks the button. Memory that was built in the other language stays as it was until `reextract`.
- English profile headings and gender-neutral wording in the English profile prompts (found in a real run on an invented English vault: 42 facts, 40 verbatim quotes, dates such as "last week" resolved correctly).
- README: "Tuning the coach" (what can and cannot be changed, no fine-tuning of weights) and "Other models".

## 5.9 (Nov 18)
- **English.** The whole tool now works in English: the prompts for the model, the terminal commands and messages, the reports, the web page (served with substituted strings) and the typed answers. `LOCAL_COACH_LANG=en|ru` chooses the language at startup (English is the default in this repository, Russian in the personal line it is generated from). Typed input accepts both languages in either mode: answer letters (a/i/s/w/h/d/l/x/u/n for registry items, c/p/u/n/s/q in the calibration dialogs, y/n/e/q for suggestions), yes/no/stop words, memory commands ("remember that ..."), body-double phrases ("for 50 minutes", "stay with me"), English dates ("May 1st, 2015") and review intervals ("in 3 months").
- The translation (about 1000 interface strings, 295 web strings and the prompt set) was produced with a language model and checked by scripts, not by a native speaker. Known limits in English: the biography table parser and the "present tense" portrait fixer still expect Russian text, and answer quality with the English prompts has not been measured.
- One working folder, one language: the prompt version includes the language, so after switching run `reextract`.

## 5.8 (Nov 14 to 18)
- `LOCAL_COACH_HEAVY_MODEL` / `--heavy-model`: a separate, larger model for heavy work (fact extraction, registry naming and selection, hypotheses, people); chat and quick calls stay on the main model. Empty means everything runs on the main model as before.
- 5.8.1: in the web review window the area of an item is a dropdown on the card, so a wrong area (set by the model during extraction) can be fixed with one click and undone. Rebuilding the registry does not reset it.
- `tools/compare_naming.py` and `tools/compare_extract.py` compare two models on registry naming and on fact extraction (the second uses a synthetic diary, no personal data). Our own measurements: the larger model was not better on either task.

## 5.6 to 5.7 (Nov 14)
- Life Reset review in the web (`/triage` in chat or the button): one item per screen with five options, each explained (do it now, postpone, close, not a project, not sure). "Not sure" asks two button questions instead of a free text field; free text stays as an optional "tell me in your own words".
- Items with the same title are merged automatically when the window opens (with an Undo button); "Not a project" hides reference lists and notes from the review for good.

## 5.5 (Nov 14)
- Presence mode: start it on an active Life Reset item (`/focus LR-12 on 50 minutes draft`, or pick it in the web start dialog) and `reset` shows the focus time per active direction for the last 30 days.
- Optional time plan (25/50/90 minutes in the web, "на 50 минут" in chat). In the web a progress ring sits next to the timer, and when time is up the cat wakes, stretches and asks how it went; the first reply is saved with the session.

## 5.4 (Nov 14)
- Web presence mode shows the task you typed above the cat, with a running timer and a slow glow (none under "reduce motion").

## 5.3 (Nov 14)
- `triage --area <area>` reviews one area of life; `triage --quick` takes a whole list in one line ("1а 2к 3о", "4?" opens a card). Web: the input hint is configurable in the settings.

## 5.0 to 5.2 (Nov 14)
- Independent code review of Life Reset: ten findings, all fixed (lost sources, lost hypotheses, personal data in exports, extra model calls).
- Single mentions get no model naming and are reviewed in bulk with `sweep`; the model picks the most significant ones per area and the rest wait for your answer. `inventory reopen LR-N` brings a closed item back.

## 4.24 (Nov 14)
- **Life Reset**, a program for sorting out what has piled up in your notes: a registry of goals, projects, ideas and open loops (`inventory`), one decision per item with a reason (`triage`), an audit of twelve areas of life (`audit`), careful hypotheses about recurring patterns with quotes and alternative explanations (`hypotheses`; always "maybe", only you change their status), stages and conversation depth (`reset`, `/depth`). Results are exported to a `Life Reset/` folder in the vault; sensitive items appear there by number only. In chat: `/reset`, `/triage`, `/audit`, `/hypotheses`, `/idea`.
- The coach prompt now has a stable part (your active items, audit focus, program rules) before the changing blocks, so Ollama reuses the computed prefix.

## 4.23 (Oct 29)
- Web interface (`coach.py serve`) with the cat Basil: streamed answers, presence mode, PIN access, session summary saved automatically.
- Background sound mixer for focus and cat reactions to stroking (4.19 to 4.23).

## 4.18 (Oct 29)
- `bio` command (the old name `history` still works); the "age" column of a biography table is no longer treated as a fact and is used to label years with age.
- `/think` in chat toggles reasoning for one question or the whole session.

## 4.17 (Oct 27)
- Finds Ollama on another port of the same machine and remembers it.

## 4.14 to 4.16 (Oct 27)
- Timeline by year straight from the database, without the model; chat routes list requests to it.
- Chronology by source roles: biography rows parsed by code, only events from notes, no repeats.
- Numbers like "2000 rpm" are not years; multi-year events fold into one line.

## 4.13 (Oct 25)
- Chat works without the main machine: falls back to another Ollama.

## 4.9 to 4.12 (Oct 24)
- Ongoing things stay in the present tense in the timeline, profile and audit.
- Remembering in chat offers to retire similar old memory; rebuild-profile offer at the end of a session.
- Tables with year headers and merged cells; `tools/import_check.py` and `tools/date_stats.py` (counts only, no content).

## 4.2 to 4.8 (Oct 23 to 24)
- Body double mode (`/focus`), check-ins, people as entities, commitments with due dates, quality evaluation (`eval`).
- Refuses cloud models and non-local Ollama addresses; more secret patterns.
- End-to-end tests on a fake Ollama.

## 4.0 (Oct 23)
- New code base: fact extraction with verified quotes, profile with audit, hybrid retrieval, remembering in chat, file import.
