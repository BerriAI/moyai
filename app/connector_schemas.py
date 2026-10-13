"""Finite native app inputs. No caller-controlled URLs, methods or credentials."""
from datetime import date
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StrictBool, StrictFloat, StrictStr, model_serializer, model_validator


class Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PatchArgs(Args):
    @model_serializer(mode='wrap')
    def supplied_fields(self, handler):
        # The broker and adapter both validate/dump. Retain omission across both
        # boundaries: a default None must never become an explicit clear.
        return {key: value for key, value in handler(self).items() if key in self.model_fields_set}


UUID = Annotated[str, Field(pattern=r"^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$"), AfterValidator(str.lower)]
NotionID = Annotated[str, Field(pattern=r"^(?:[a-fA-F0-9]{32}|[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12})$")]
LinearIssueId = Annotated[str, Field(pattern=r"^(?:[A-Za-z][A-Za-z0-9]*-\d+|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})$")]
SlackChannelID = Annotated[str, Field(pattern=r"^[CDG][A-Z0-9]{7,30}$")]
SlackTS = Annotated[str, Field(pattern=r"^\d{10,16}\.\d{1,9}$")]


class CursorPage(Args):
    cursor: str = ''
    page_size: int = Field(default=50, ge=1, le=100, description="Provider page size, not a total result limit. Follow the returned continuation.")


class LinearUpdateIssue(PatchArgs):
    issue_id: LinearIssueId
    parent_id: LinearIssueId | None = Field(default=None, description="Omit to preserve; null removes parent.")
    title: str | None = Field(default=None, min_length=1, max_length=250, description="Omit to preserve; null is rejected.")
    description: str | None = Field(default=None, description="Omit to preserve; null clears Markdown description.")
    state_id: UUID | None = Field(default=None, description="Workflow status ID from linear_options(states). Null is rejected.")
    assignee_id: UUID | None = Field(default=None, description="User ID; null unassigns.")
    priority: int | None = Field(default=None, ge=0, le=4, strict=True, description="0 none, 1 urgent, 2 high, 3 medium, 4 low. Null is rejected.")
    label_ids: list[UUID] | None = Field(default=None, max_length=100, description="Replace the entire label set; [] clears. Null is rejected.")
    project_id: UUID | None = Field(default=None, description="Project ID; null removes the project.")
    cycle_id: UUID | None = Field(default=None, description="Team cycle ID; null removes the cycle.")
    due_date: str | None = Field(default=None, description="YYYY-MM-DD; null clears.")

    @model_validator(mode='after')
    def valid_patch(self):
        if not self.model_fields_set - {'issue_id'}:
            raise ValueError("Supply at least one update field.")
        for key in ('title', 'state_id', 'priority', 'label_ids'):
            if key in self.model_fields_set and getattr(self, key) is None:
                raise ValueError(f"{key} cannot be null.")
        if self.due_date is not None:
            if date.fromisoformat(self.due_date).isoformat() != self.due_date:
                raise ValueError("due_date must use YYYY-MM-DD.")
        return self


class LinearOptions(CursorPage):
    team_id: UUID
    resource: Literal['states', 'members', 'labels', 'projects', 'cycles']


class SlackMessage(Args):
    channel: SlackChannelID
    ts: SlackTS


class SlackReply(Args):
    channel: SlackChannelID
    thread_ts: SlackTS = Field(description="Exact root message timestamp in this conversation, not a reply timestamp.")
    text: str = Field(min_length=1)


class SlackEdit(SlackMessage):
    text: str = Field(min_length=1, max_length=39000, description="Replacement body, prefixed with the active requester's name. One message only.")


class SlackReaction(SlackMessage):
    name: str = Field(pattern=r'^[a-z0-9_+-]+(?:::skin-tone-[2-6])?$', max_length=100)
    action: Literal['add', 'remove']


class NotionObject(Args):
    object_id: NotionID
    kind: Literal['page', 'block', 'database', 'data_source']


class NotionDiscover(CursorPage):
    query: str = ''
    kind: Literal['page', 'data_source'] = 'data_source'


class NotionPropertyRead(CursorPage):
    page_id: NotionID
    property_id: str = Field(min_length=1, max_length=200, description="Property ID from notion_object(page); URL-encoded IDs are accepted.")


class NotionText(Args):
    text: str = Field(max_length=2000)
    bold: bool = False
    italic: bool = False
    strikethrough: bool = False
    code: bool = False
    link: str | None = Field(default=None, pattern=r'^https?://')


BlockType = Literal['paragraph', 'heading_1', 'heading_2', 'heading_3', 'bulleted_list_item',
                    'numbered_list_item', 'to_do', 'toggle', 'quote', 'code', 'divider']


class NotionBlock(Args):
    type: BlockType
    rich_text: list[NotionText] = Field(default_factory=list, max_length=100)
    checked: bool | None = None
    language: Literal['plain text', 'python', 'javascript', 'typescript', 'bash', 'json', 'sql', 'html', 'css', 'markdown'] | None = None
    children: list['NotionBlock'] = Field(default_factory=list, max_length=100)

    @model_validator(mode='after')
    def supported_block(self):
        if self.checked is not None and self.type != 'to_do':
            raise ValueError("checked is only supported for to_do.")
        if self.language is not None and self.type != 'code':
            raise ValueError("language is only supported for code.")
        if self.type == 'divider' and self.rich_text:
            raise ValueError("A divider has no rich text.")
        if self.children and self.type not in {'paragraph', 'bulleted_list_item', 'numbered_list_item', 'to_do', 'toggle', 'quote'}:
            raise ValueError("This block type cannot contain children.")
        if any(child.children for child in self.children):
            raise ValueError("Append at most two block levels; append deeper children separately.")
        return self


class PropertyBase(Args):
    property: str = Field(min_length=1, max_length=200, description="Exact property name or ID from the data-source schema.")


class TextProperty(PropertyBase):
    type: Literal['title', 'rich_text']
    text: str = Field(max_length=200000)


class NumberProperty(PropertyBase):
    type: Literal['number']
    value: float | None = Field(allow_inf_nan=False)


class CheckboxProperty(PropertyBase):
    type: Literal['checkbox']
    value: bool


class SelectProperty(PropertyBase):
    type: Literal['select', 'status']
    value: str | None = Field(max_length=100, description="Existing option name; null clears.")


class MultiSelectProperty(PropertyBase):
    type: Literal['multi_select']
    values: list[Annotated[str, Field(min_length=1, max_length=100)]] = Field(max_length=100)


class RelationProperty(PropertyBase):
    type: Literal['relation', 'people']
    ids: list[NotionID] = Field(max_length=100, description="Complete replacement list; [] clears.")


class DateValue(Args):
    start: str = Field(min_length=1, max_length=50)
    end: str | None = Field(default=None, max_length=50)
    time_zone: str | None = Field(default=None, max_length=100)


class DateProperty(PropertyBase):
    type: Literal['date']
    value: DateValue | None


class ContactProperty(PropertyBase):
    type: Literal['url', 'email', 'phone_number']
    value: str | None = Field(max_length=2000)


NotionProperty = Annotated[
    TextProperty | NumberProperty | CheckboxProperty | SelectProperty | MultiSelectProperty |
    RelationProperty | DateProperty | ContactProperty, Field(discriminator='type')]


class NotionCreatePage(Args):
    parent_id: NotionID
    parent_type: Literal['page_id', 'data_source_id']
    properties: list[NotionProperty] = Field(min_length=1, max_length=100)
    children: list[NotionBlock] = Field(default_factory=list, max_length=100)

    @model_validator(mode='after')
    def parent_properties(self):
        if len({p.property for p in self.properties}) != len(self.properties):
            raise ValueError("Duplicate properties are not allowed.")
        if self.parent_type == 'page_id' and (len(self.properties) != 1 or self.properties[0].type != 'title' or self.properties[0].property != 'title'):
            raise ValueError("A page parent accepts only the title property, named title.")
        return self


class NotionUpdatePage(PatchArgs):
    page_id: NotionID
    properties: list[NotionProperty] = Field(default_factory=list, max_length=100)
    in_trash: bool | None = Field(default=None, description="true archives, false restores. Omit to preserve; null is rejected.")

    @model_validator(mode='after')
    def valid_patch(self):
        if not self.properties and 'in_trash' not in self.model_fields_set:
            raise ValueError("Supply properties or in_trash.")
        if 'in_trash' in self.model_fields_set and self.in_trash is None:
            raise ValueError("in_trash cannot be null.")
        if len({p.property for p in self.properties}) != len(self.properties):
            raise ValueError("Duplicate properties are not allowed.")
        return self


class NotionAppendBlocks(Args):
    block_id: NotionID
    children: list[NotionBlock] = Field(min_length=1, max_length=100)
    after: NotionID | None = Field(default=None, description="Existing sibling block ID; omit to append at end.")


class NotionUpdateBlock(PatchArgs):
    block_id: NotionID
    block: NotionBlock | None = Field(default=None, description="Replace this block's content, preserving its type. Append children separately.")
    archived: bool | None = Field(default=None, description="true archives, false restores; omit to preserve.")

    @model_validator(mode='after')
    def valid_patch(self):
        if not self.model_fields_set - {'block_id'}:
            raise ValueError("Supply block or archived.")
        if any(getattr(self, key) is None for key in self.model_fields_set - {'block_id'}):
            raise ValueError("Omit unchanged fields; null is not supported.")
        if self.block and (self.block.children or self.block.type == 'divider'):
            raise ValueError("Update content without children; dividers support archive only.")
        return self


class NotionFilter(Args):
    property: str = Field(min_length=1, max_length=200)
    type: Literal['title', 'rich_text', 'number', 'checkbox', 'select', 'status', 'multi_select', 'date', 'people', 'relation']
    operator: Literal['equals', 'does_not_equal', 'contains', 'does_not_contain', 'starts_with', 'ends_with',
                      'greater_than', 'less_than', 'greater_than_or_equal_to', 'less_than_or_equal_to',
                      'before', 'after', 'on_or_before', 'on_or_after', 'is_empty', 'is_not_empty']
    value: StrictStr | StrictFloat | StrictBool

    @model_validator(mode='after')
    def operator_for_type(self):
        common = {'is_empty', 'is_not_empty'}
        text = {'equals', 'does_not_equal', 'contains', 'does_not_contain', 'starts_with', 'ends_with'}
        operators = {
            'title': text, 'rich_text': text, 'number': {'equals', 'does_not_equal', 'greater_than', 'less_than', 'greater_than_or_equal_to', 'less_than_or_equal_to'},
            'checkbox': {'equals', 'does_not_equal'}, 'select': {'equals', 'does_not_equal'}, 'status': {'equals', 'does_not_equal'},
            'multi_select': {'contains', 'does_not_contain'}, 'people': {'contains', 'does_not_contain'},
            'relation': {'contains', 'does_not_contain'}, 'date': {'equals', 'before', 'after', 'on_or_before', 'on_or_after'},
        }
        allowed = operators[self.type] | (common if self.type != 'checkbox' else set())
        if self.operator not in allowed:
            raise ValueError("Unsupported operator for this property type.")
        expected = bool if self.operator in common or self.type == 'checkbox' else (int, float) if self.type == 'number' else str
        if not isinstance(self.value, expected) or (self.type == 'number' and isinstance(self.value, bool)):
            raise ValueError("Incorrect filter value type.")
        return self


class NotionSort(Args):
    property: str = Field(min_length=1, max_length=200)
    direction: Literal['ascending', 'descending'] = 'ascending'


class NotionQuery(CursorPage):
    data_source_id: NotionID
    filters: list[NotionFilter] = Field(default_factory=list, max_length=20, description="All conditions must match (AND). Not workspace full-text search.")
    sorts: list[NotionSort] = Field(default_factory=list, max_length=20)
