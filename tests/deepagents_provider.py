"""Responses wire fixtures consumed by the installed SDK, never a fake SDK."""
import copy
import json


def event(kind, **fields):
    return ('event: ' + kind + '\ndata: ' + json.dumps({'type': kind, **fields}) + '\n\n').encode()


def response_stream(response):
    yield event('response.created', response={**response, 'output': [], 'status': 'in_progress'})
    for index, item in enumerate(response['output']):
        initial = copy.deepcopy(item)
        if item['type'] == 'message':
            initial.update(content=[], status='in_progress')
        else:
            initial['arguments'] = ''
        yield event('response.output_item.added', output_index=index, item=initial)
        if item['type'] == 'message':
            for part_index, part in enumerate(item['content']):
                yield event('response.content_part.added', output_index=index, content_index=part_index,
                            item_id=item['id'], part={**part, 'text': ''})
                yield event('response.output_text.delta', output_index=index, content_index=part_index,
                            item_id=item['id'], delta=part['text'])
                yield event('response.output_text.done', output_index=index, content_index=part_index,
                            item_id=item['id'], text=part['text'])
                yield event('response.content_part.done', output_index=index, content_index=part_index,
                            item_id=item['id'], part=part)
        elif item['type'] == 'function_call':
            yield event('response.function_call_arguments.delta', output_index=index,
                        item_id=item['id'], delta=item['arguments'])
            yield event('response.function_call_arguments.done', output_index=index,
                        item_id=item['id'], arguments=item['arguments'])
        yield event('response.output_item.done', output_index=index, item=item)
    yield event('response.completed', response=response)


def answer(text='Completed response.', identity='fixture'):
    return {'id': 'resp_' + identity, 'object': 'response', 'status': 'completed', 'model': 'test-model',
        'created_at': 1770000000, 'output': [{'id': 'msg_' + identity, 'type': 'message', 'role': 'assistant',
            'status': 'completed', 'content': [{'type': 'output_text', 'text': text, 'annotations': []}]}],
        'usage': {'input_tokens': 10, 'output_tokens': 4, 'total_tokens': 14}}
