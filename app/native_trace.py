"""Trace-only projection of native protocols; never used for billing or forwarding."""

LIMIT = 16_000
OMITTED = '[text omitted: trace size limit]'


class NativeModelContent:
    """Retain public text and tool names, then let AgentTracing sanitize them.

    Assemble deltas before redaction so chunk boundaries cannot split a secret.
    On overflow omit the whole text, including its potentially private prefix.
    """
    def __init__(self, payload: dict, route: str) -> None:
        self.route = route
        self.text: str | None = ''
        self.tools: list[str] = []
        self.messages: list[dict] = []
        items = payload.get('messages' if route == '/v1/messages' else 'input')
        if isinstance(items, str):
            items = [{'role': 'user', 'content': items}]
        for item in reversed(items if isinstance(items, list) else []):
            if not isinstance(item, dict) or item.get('role') != 'user':
                continue
            content = item.get('content')
            blocks = [{'type': 'text', 'text': content}] if isinstance(content, str) else content
            self.blocks(blocks, input_text=True)
            if self.text:
                self.messages.append({'role': 'user', 'content': self.text})
            if self.text is None or sum(len(m['content']) for m in self.messages) > LIMIT:
                # Bound the aggregate of all retained input, not each message.
                self.messages = [{'role': 'user', 'content': OMITTED}]
                break
            if len(self.messages) == 5:
                break
            self.text = ''
        self.messages.reverse()
        self.text = ''

    def append(self, text: object) -> None:
        if isinstance(text, str) and self.text is not None:
            self.text = self.text + text if len(self.text) + len(text) <= LIMIT else None

    def blocks(self, blocks: object, *, input_text: bool = False) -> None:
        for block in blocks if isinstance(blocks, list) else []:
            if not isinstance(block, dict):
                continue
            kind = block.get('type')
            if kind in (('text', 'input_text') if input_text else ('text', 'output_text')):
                self.append(block.get('text'))
            elif not input_text and kind in ('tool_use', 'function_call', 'custom_tool_call'):
                name = block.get('name')
                if isinstance(name, str) and name and len(self.tools) < 100:
                    self.tools.append(name if len(name) <= 120 else '[tool name omitted]')
            elif not input_text and kind == 'message' and block.get('role') == 'assistant':
                # Responses message content is a flat list; never traverse
                # arbitrary nested objects (reasoning, arguments or results).
                for part in block.get('content', []) if isinstance(block.get('content'), list) else []:
                    if isinstance(part, dict) and part.get('type') == 'output_text':
                        self.append(part.get('text'))

    def consume(self, value: dict) -> None:
        kind = value.get('type')
        if self.route == '/v1/messages':
            if kind == 'message_start':
                message = value.get('message')
                if isinstance(message, dict):
                    self.blocks(message.get('content'))
            elif kind == 'content_block_start':
                self.blocks([value.get('content_block')])
            elif kind == 'content_block_delta':
                delta = value.get('delta')
                if isinstance(delta, dict) and delta.get('type') == 'text_delta':
                    self.append(delta.get('text'))
            elif kind in (None, 'message'):
                self.blocks(value.get('content'))
        else:
            if kind == 'response.output_text.delta':
                self.append(value.get('delta'))
            elif kind == 'response.output_item.added':
                item = value.get('item')
                if isinstance(item, dict) and item.get('type') in ('function_call', 'custom_tool_call'):
                    self.blocks([item])
            else:
                response = value.get('response') if kind in (
                    'response.completed', 'response.failed', 'response.incomplete') else value
                if isinstance(response, dict) and isinstance(response.get('output'), list):
                    observed_text, observed_tools = self.text, self.tools
                    # Completed snapshots contain the full output; replacing
                    # deltas avoids duplicates. Unsuccessful snapshots can be
                    # empty or shorter than what reached the caller already.
                    self.text, self.tools = '', []
                    self.blocks(response['output'])
                    if kind in ('response.failed', 'response.incomplete') or response.get('status') in ('failed', 'incomplete'):
                        if observed_text != '':
                            self.text = observed_text
                        if observed_tools:
                            self.tools = observed_tools

    @property
    def choices(self) -> list[dict]:
        if self.text == '' and not self.tools:
            return []
        return [{'message': {'content': self.text if self.text is not None else OMITTED,
                             'tool_calls': [{'function': {'name': name}} for name in self.tools]}}]
