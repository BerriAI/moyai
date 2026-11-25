from typing import Literal

SlackApp = Literal["dev", "dot"]


def saved_slack_app(app: str | None) -> SlackApp:
    if app is None or app == "dev":
        return "dev"
    if app == "dot":
        return "dot"
    raise ValueError("Unknown saved Slack app.")


def slack_provider(app: SlackApp) -> str:
    if app == "dev":
        return "slack"
    if app == "dot":
        return "slack_dot"
    raise ValueError("Unknown Slack app.")
