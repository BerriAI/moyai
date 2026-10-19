"""Slack Block Kit presentation and answer parsing for deferred questions."""

from __future__ import annotations

import html
from typing import Any, NoReturn

from fastapi import HTTPException
from pydantic_ai_harness.ask_user import AskUserRequest

CHOICE_ACTION = "moyai_native_choice"
SUBMIT_ACTION = "moyai_native_answer"
SLACK_OPTION_TEXT_LIMIT = 75


class SlackQuestions:
    @staticmethod
    def requests(pending) -> list[AskUserRequest]:
        if pending is None:
            return []
        return [
            AskUserRequest.from_tool_call(call)
            for call in pending.calls
            if call.tool_name == "ask_user_question"
        ]

    @classmethod
    def _numbered(cls, requests: list[AskUserRequest]):
        return enumerate(question for request in requests for question in request.questions)

    @classmethod
    def blocks(cls, requests: list[AskUserRequest]) -> list[dict[str, Any]]:
        return [
            block
            for index, question in cls._numbered(requests)
            for block in cls._question(index, question)
        ]

    @classmethod
    def _question(cls, index: int, question: Any) -> list[dict[str, Any]]:
        return [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*{html.escape(question.header)}*\n{html.escape(question.question)}",
                },
            },
            {
                "type": "actions",
                "block_id": cls._block_id(index),
                "elements": [{
                    "type": "checkboxes" if question.multi_select else "radio_buttons",
                    "action_id": CHOICE_ACTION,
                    "options": [cls._option(option_index, option)
                                for option_index, option in enumerate(question.options)],
                }],
            },
        ]

    @staticmethod
    def _option(index: int, option: Any) -> dict[str, Any]:
        value = {
            "text": {"type": "plain_text", "text": option.label},
            "value": str(index),
        }
        if option.description is not None:
            value["description"] = {
                "type": "plain_text",
                "text": SlackQuestions._clip(option.description),
            }
        return value

    @staticmethod
    def _clip(text: str, limit: int = SLACK_OPTION_TEXT_LIMIT) -> str:
        return text if len(text) <= limit else text[:limit - 1] + "…"

    @staticmethod
    def _block_id(index: int) -> str:
        return f"moyai_question:{index}"

    @staticmethod
    def submit(turn_id: str) -> dict[str, Any]:
        return {
            "type": "button",
            "action_id": SUBMIT_ACTION,
            "value": turn_id,
            "style": "primary",
            "text": {"type": "plain_text", "text": "Submit answer"},
        }

    @classmethod
    def answers(cls, requests: list[AskUserRequest], state: dict[str, Any]) -> dict[str, str | list[str]]:
        return {
            question.header: cls._answer(question, cls._selected(state, index, question))
            for index, question in cls._numbered(requests)
        }

    @classmethod
    def _selected(cls, state: dict[str, Any], index: int, question: Any) -> list[str]:
        values = state.get("values") if isinstance(state, dict) else None
        question_state = values.get(cls._block_id(index)) if isinstance(values, dict) else None
        selection = question_state.get(CHOICE_ACTION) if isinstance(question_state, dict) else None
        if question.multi_select:
            selected = selection.get("selected_options") if isinstance(selection, dict) else None
            raw_values = [
                option.get("value") if isinstance(option, dict) else None
                for option in selected
            ] if isinstance(selected, list) else []
        else:
            selected = selection.get("selected_option") if isinstance(selection, dict) else None
            raw_values = [selected.get("value")] if isinstance(selected, dict) else []
        if not raw_values:
            cls._missing(question)
        selected_values = []
        for value in raw_values:
            if not isinstance(value, str) or not value:
                cls._missing(question)
            selected_values.append(value)
        return selected_values

    @classmethod
    def _answer(cls, question: Any, values: list[str]) -> str | list[str]:
        if not values:
            cls._missing(question)
        labels = []
        for value in values:
            if not value.isascii() or not value.isdigit():
                cls._missing(question)
            index = int(value)
            if index >= len(question.options):
                cls._missing(question)
            labels.append(question.options[index].label)
        return labels if question.multi_select else labels[0]

    @staticmethod
    def _missing(question: Any) -> NoReturn:
        raise HTTPException(422, f"Choose an answer for {question.header}.")
