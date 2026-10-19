"""Slack Block Kit presentation and answer parsing for deferred questions."""

from __future__ import annotations

import html
from typing import Any

from fastapi import HTTPException
from pydantic_ai_harness.ask_user import AskUserRequest

CHOICE_ACTION = "moyai_native_choice"
SUBMIT_ACTION = "moyai_native_answer"


class SlackQuestions:
    @staticmethod
    def blocks(requests: list[AskUserRequest]) -> list[dict[str, Any]]:
        blocks = []
        index = 0
        for request in requests:
            for question in request.questions:
                options = []
                for option_index, option in enumerate(question.options):
                    value = {
                        "text": {"type": "plain_text", "text": option.label},
                        "value": str(option_index),
                    }
                    if option.description is not None:
                        description = option.description
                        if len(description) > 75:
                            description = description[:74] + "…"
                        value["description"] = {"type": "plain_text", "text": description}
                    options.append(value)
                blocks.extend([
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"*{html.escape(question.header)}*\n{html.escape(question.question)}",
                        },
                    },
                    {
                        "type": "actions",
                        "block_id": f"moyai_question:{index}",
                        "elements": [{
                            "type": "checkboxes" if question.multi_select else "radio_buttons",
                            "action_id": CHOICE_ACTION,
                            "options": options,
                        }],
                    },
                ])
                index += 1
        return blocks

    @staticmethod
    def submit(turn_id: str) -> dict[str, Any]:
        return {
            "type": "button",
            "action_id": SUBMIT_ACTION,
            "value": turn_id,
            "style": "primary",
            "text": {"type": "plain_text", "text": "Submit answer"},
        }

    @staticmethod
    def answers(requests: list[AskUserRequest], state: dict[str, Any]) -> dict[str, str | list[str]]:
        values = state.get("values") if isinstance(state, dict) else None
        answers: dict[str, str | list[str]] = {}
        index = 0
        for request in requests:
            for question in request.questions:
                question_state = values.get(f"moyai_question:{index}") if isinstance(values, dict) else None
                selection = question_state.get(CHOICE_ACTION) if isinstance(question_state, dict) else None
                option_values = None
                if question.multi_select:
                    selected = selection.get("selected_options") if isinstance(selection, dict) else None
                    if isinstance(selected, list) and selected:
                        option_values = [option.get("value") if isinstance(option, dict) else None for option in selected]
                else:
                    selected = selection.get("selected_option") if isinstance(selection, dict) else None
                    if isinstance(selected, dict):
                        option_values = [selected.get("value")]
                if not option_values:
                    raise HTTPException(422, f"Choose an answer for {question.header}.")
                labels = []
                for value in option_values:
                    if not isinstance(value, str) or not value.isascii() or not value.isdigit():
                        raise HTTPException(422, f"Choose an answer for {question.header}.")
                    option_index = int(value)
                    if option_index >= len(question.options):
                        raise HTTPException(422, f"Choose an answer for {question.header}.")
                    labels.append(question.options[option_index].label)
                answers[question.header] = labels if question.multi_select else labels[0]
                index += 1
        return answers
