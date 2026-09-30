"""What the meta-statement filter refuses, and what it must keep.

Capture stored an assistant's apology as a durable fact once, behind a prompt that already
forbade it. The filter is the second line, and it only knew first-person present tense: the
extractor is told to resolve pronouns, so a meta-statement usually arrives in the third person
("The assistant could not find…"), and past tense and apologies went straight through. Both
lists are the measure: widening the pattern must not start eating facts about the user.
"""

from __future__ import annotations

import pytest
from felix.memory.extraction import looks_like_assistant_meta

REFUSED = [
    "I'm sorry, I couldn't find that file.",
    "I couldn't find the deployment runbook.",
    "I was unable to access the calendar.",
    "I apologize for the confusion.",
    "Sorry for the confusion earlier.",
    "The assistant apologized for the confusion.",
    "The assistant could not find the deployment runbook.",
    "The assistant does not have access to the user's calendar.",
    "The assistant will remember this for next time.",
    "As a language model, it cannot browse the web.",
    "The AI is unable to open attachments.",
    "I'll keep that in mind.",
    "Let me know if you need anything else.",
]

KEPT = [
    "The user's timezone is CET.",
    "The user is unable to access the VPN from the office network.",
    "The user could not attend the Tuesday standup.",
    "The deploy runbook lives in the ops repository.",
    "The user prefers metric units.",
    "The user will be in Berlin from March 3 to March 7.",
    "The staging database was migrated to Postgres 17.",
    "Apologies are logged in the incident channel by the on-call engineer.",
]


@pytest.mark.parametrize("sentence", REFUSED)
def test_the_assistant_talking_about_itself_is_refused(sentence: str) -> None:
    assert looks_like_assistant_meta(sentence), sentence


@pytest.mark.parametrize("sentence", KEPT)
def test_facts_about_the_user_and_the_world_are_kept(sentence: str) -> None:
    assert not looks_like_assistant_meta(sentence), sentence
