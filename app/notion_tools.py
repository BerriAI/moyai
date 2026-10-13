"""Notion 2025-09-03 operations, routed only to fixed Notion resources."""
import json
from urllib.parse import quote, unquote

from .connector_errors import ConnectorError
from .connector_schemas import (
    NotionAppendBlocks, NotionCreatePage, NotionDiscover, NotionObject,
    NotionPropertyRead, NotionQuery, NotionUpdateBlock, NotionUpdatePage,
)

TOOLS = {
    'notion_discover': ('notion', False, NotionDiscover, "Discover shared page or data-source titles, optionally by title query. Follow next_arguments. This is not workspace-wide full-text search."),
    'notion_object': ('notion', False, NotionObject, "Retrieve page properties, one block, database data-source IDs, or a data-source property schema. Use kind=database to discover its data_sources; query/create rows with data_source_id, not database_id. Use notion_property for paginated property values."),
    'notion_property': ('notion', False, NotionPropertyRead, "Read one page property by its returned ID, including paginated relations, people, title and rich text. Follow next_arguments for complete values."),
    'notion_query': ('notion', False, NotionQuery, "Query rows of one shared data source with typed AND filters and property sorts. Discover IDs/schema with notion_discover and notion_object first. Follow next_arguments. Formula/rollup and arbitrary filters are not supported."),
    'notion_create_page': ('notion', True, NotionCreatePage, "Create a page under a shared page or data source. A page parent accepts only a title property named title. Data-source properties must match its schema. Supports typed properties and structured children. Verify an uncertain creation by discovery/query before any retry."),
    'notion_update_page': ('notion', True, NotionUpdatePage, "Update only supplied page properties, or archive/restore with in_trash. Omitted properties are unchanged; property values replace existing values. Use [] to clear lists and null only for nullable property types. Read notion_object(page) and notion_property to reconcile uncertain writes."),
    'notion_append_blocks': ('notion', True, NotionAppendBlocks, "Append up to 100 structured blocks (two levels per call) to a page/block; optionally insert after an existing sibling ID. For deeper nesting append separately. Read notion_page at the parent to reconcile before retrying any uncertain append."),
    'notion_update_block': ('notion', True, NotionUpdateBlock, "Replace one block's content without changing its type, or archive/restore it. Omit unchanged fields. Children are appended separately. Read notion_object(block) to reconcile an uncertain write."),
}
READS = frozenset(name for name, spec in TOOLS.items() if not spec[1])


def text_values(text):
    return [{'type': 'text', 'text': {'content': text[i:i + 2000]}} for i in range(0, len(text), 2000)]


def block_value(block):
    kind = block['type']
    value = {}
    if kind != 'divider':
        value['rich_text'] = [
            {'type': 'text', 'text': {'content': item['text'], **({'link': {'url': item['link']}} if item.get('link') else {})},
             'annotations': {k: item[k] for k in ('bold', 'italic', 'strikethrough', 'code')}}
            for item in block['rich_text']]
    if kind == 'to_do':
        value['checked'] = block.get('checked') or False
    if kind == 'code':
        value['language'] = block.get('language') or 'plain text'
    if block.get('children'):
        value['children'] = [block_value(child) for child in block['children']]
    return {'object': 'block', 'type': kind, kind: value}


def property_values(properties):
    values = {}
    for prop in properties:
        kind = prop['type']
        if kind in {'title', 'rich_text'}:
            value = text_values(prop['text'])
        elif kind in {'select', 'status'}:
            value = {'name': prop['value']} if prop['value'] is not None else None
        elif kind == 'multi_select':
            value = [{'name': name} for name in prop['values']]
        elif kind in {'people', 'relation'}:
            value = [{'id': identity} for identity in prop['ids']]
        else:
            value = prop['value']
        values[prop['property']] = {kind: value}
    return values


def checked_payload(payload):
    if len(json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode()) > 500000:
        raise ConnectorError('Notion payload exceeds 500 KB; split it into smaller operations.',
                             code='payload_too_large', dispatched=False)
    return payload


async def call(connectors, name, args, headers, run):
    from .connectors import paged

    revision = connectors.connection_revision('notion')
    write = TOOLS[name][1]

    async def request(method, path, **kwargs):
        connectors.guard(name, run, revision)
        data = await connectors.request(method, f'https://api.notion.com/v1/{path}', headers=headers, **kwargs)
        if not write:
            connectors.guard(name, run, revision)
        return data

    pagination = {'page_size': args.get('page_size', 50)}
    if args.get('cursor'):
        pagination['start_cursor'] = args['cursor']
    if name == 'notion_object':
        plural = {'page': 'pages', 'block': 'blocks', 'database': 'databases', 'data_source': 'data_sources'}[args['kind']]
        return await request('GET', f"{plural}/{args['object_id']}")
    if name == 'notion_discover':
        data = await request('POST', 'search', json={**pagination, 'query': args['query'],
            'filter': {'property': 'object', 'value': args['kind']}})
    elif name == 'notion_property':
        # IDs returned by Notion may already be encoded; never allow path/query injection.
        identity = quote(unquote(args['property_id']), safe='')
        data = await request('GET', f"pages/{args['page_id']}/properties/{identity}", params=pagination)
        if data.get('object') != 'list':
            return {**data, 'next_arguments': None}
    elif name == 'notion_query':
        payload = {**pagination, 'sorts': args['sorts']}
        if args['filters']:
            payload['filter'] = {'and': [
                {'property': item['property'], item['type']: {item['operator']: item['value']}}
                for item in args['filters']]}
        data = await request('POST', f"data_sources/{args['data_source_id']}/query", json=payload)
    else:
        if name == 'notion_create_page':
            method, path = 'POST', 'pages'
            payload = {'parent': {args['parent_type']: args['parent_id']},
                       'properties': property_values(args['properties']),
                       'children': [block_value(block) for block in args['children']]}
        elif name == 'notion_update_page':
            method, path = 'PATCH', f"pages/{args['page_id']}"
            payload = {}
            if 'properties' in args:
                payload['properties'] = property_values(args['properties'])
            if 'in_trash' in args:
                payload['in_trash'] = args['in_trash']
        elif name == 'notion_append_blocks':
            method, path = 'PATCH', f"blocks/{args['block_id']}/children"
            payload = {'children': [block_value(block) for block in args['children']]}
            if args['after']:
                payload['after'] = args['after']
        else:
            method, path = 'PATCH', f"blocks/{args['block_id']}"
            payload = {}
            if args.get('block'):
                try:
                    current = await request('GET', path)
                except ConnectorError as exc:
                    raise ConnectorError('Could not read the block before editing. No update was sent.',
                                         code=exc.code, dispatched=False) from exc
                kind = args['block']['type']
                if current.get('type') != kind:
                    raise ConnectorError('Read the block and preserve its existing type.', code='block_type_mismatch', dispatched=False)
                payload[kind] = block_value(args['block'])[kind]
            if 'archived' in args:
                payload['archived'] = args['archived']
        data = await request(method, path, json=checked_payload(payload))
        expected = 'list' if name == 'notion_append_blocks' else 'block' if name == 'notion_update_block' else 'page'
        target = args.get('page_id') or args.get('block_id')
        if (data.get('object') != expected
                or (expected != 'list' and not data.get('id'))
                or (expected != 'list' and target and data['id'].replace('-', '').lower() != target.replace('-', '').lower())
                or (expected == 'list' and len(data.get('results', [])) != len(args['children']))):
            raise ConnectorError('Notion did not return the expected write receipt. Read the destination before retrying.',
                                 code='missing_receipt', dispatched=True)
        return data
    return paged(data, args, scope=name.removeprefix('notion_'),
                 has_more=data.get('has_more'), cursor=data.get('next_cursor'))
