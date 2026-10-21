"""Behavioral checks for OCU's input adapter; actual GTK coverage lives in smoke.py."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

spec = importlib.util.spec_from_file_location("ocu_input_test", Path(__file__).parents[1] / "ocu_guest.py")
guest = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guest)


class Node:
    def __init__(self, text="", *, children=(), focused=False, editable=True, active=False):
        self.text, self.caret, self.selection = text, len(text), None
        self.focused, self.editable, self.active = focused, editable, active
        self.children, self.parent = list(children), None
        for child in self.children:
            child.parent = self

    def get_text_iface(self):
        return self

    get_editable_text_iface = get_text_iface
    get_component_iface = get_text_iface
    get_value_iface = get_text_iface

    def get_parent(self):
        return self.parent

    def get_index_in_parent(self):
        return self.parent.children.index(self)


def walk(node):
    yield node
    for child in node.children:
        yield from walk(child)


@pytest.fixture
def runtime():
    decoy = Node("DO NOT EDIT")
    target = Node("abcd", focused=True)
    window = Node(children=(decoy, target), active=True)

    def delete(node, start, end):
        node.text = node.text[:start] + node.text[end:]
        node.selection = None
        return True

    def insert(node, offset, text, length):
        assert length == len(text.encode("utf-8"))
        node.text = node.text[:offset] + text + node.text[offset:]
        return True

    def caret(node, offset):
        node.caret = offset
        return True

    api = NS(
        Text=NS(
            get_text=lambda node, _start, _end: node.text,
            get_caret_offset=lambda node: node.caret,
            get_n_selections=lambda node: int(node.selection is not None),
            get_selection=lambda node, _index: NS(start_offset=node.selection[0], end_offset=node.selection[1]),
            set_caret_offset=Mock(side_effect=caret),
        ),
        EditableText=NS(delete_text=Mock(side_effect=delete), insert_text=Mock(side_effect=insert)),
        Component=NS(grab_focus=Mock(return_value=True)),
        StateType=NS(FOCUSED="focused", EDITABLE="editable", ACTIVE="active", VERTICAL="vertical",
                     HORIZONTAL="horizontal"),
        ModifierType=NS(CONTROL=2, SHIFT=0),
        KeySynthType=NS(LOCKMODIFIERS="lock", UNLOCKMODIFIERS="unlock", SYM="symbol"),
        generate_keyboard_event=Mock(return_value=True),
    )
    r = NS(
        Atspi=api, iter_all=walk, state_contains=lambda node, state: getattr(node, state, False),
        supports_interface=lambda node, _interface: node.editable,
        perform_operation=Mock(return_value={"ok": True}),
        build_snapshot=Mock(return_value={"focusedSummary": "text Target"}),
        resolve_app=lambda _name: window, main_window=lambda _app: (0, window),
        find_element=lambda _app, record: record,
        KEY_ALIASES={"enter": "Return"}, MODIFIER_BITS={"ctrl": "CONTROL", "shift": "SHIFT"},
        keyval=lambda name: {"Return": 0xFF0D}.get(name, ord(name[0])),
        window=window, target=target, decoy=decoy,
    )
    return r


@pytest.mark.parametrize(("caret", "selection", "text", "expected"), [
    (2, None, "X", "abXcd"),
    (0, None, "é🙂", "é🙂abcd"),
    (4, None, "\n尾", "abcd\n尾"),
    (3, (1, 3), "Ω🙂", "aΩ🙂d"),
    (4, (0, 4), "new", "new"),
    (2, None, "", "abcd"),
])
def test_typing_targets_focus_and_preserves_editing_semantics(runtime, caret, selection, text, expected):
    runtime.target.caret, runtime.target.selection = caret, selection
    guest.LinuxInput(runtime).type_text(runtime.window, text)
    assert runtime.target.text == expected
    assert runtime.decoy.text == "DO NOT EDIT"
    assert runtime.target.caret == ((selection[0] if selection else caret) + len(text) if text else caret)
    runtime.Atspi.generate_keyboard_event.assert_not_called()


@pytest.mark.parametrize("case", ["no_focus", "ambiguous_focus", "read_only"])
def test_no_guess_or_global_typing_when_focus_is_not_editable(runtime, case):
    if case == "no_focus":
        runtime.target.focused = False
    elif case == "ambiguous_focus":
        runtime.decoy.focused = True
    else:
        runtime.target.editable = False
    with pytest.raises(RuntimeError):
        guest.LinuxInput(runtime).type_text(runtime.window, "wrong")
    assert runtime.target.text == "abcd" and runtime.decoy.text == "DO NOT EDIT"
    runtime.Atspi.EditableText.insert_text.assert_not_called()
    runtime.Atspi.generate_keyboard_event.assert_not_called()


@pytest.mark.parametrize("failure", ["rejected", "false_success", "partial"])
def test_unconfirmed_edits_never_fall_back_or_repeat(runtime, failure):
    if failure == "partial":
        runtime.target.selection = (1, 3)
    runtime.Atspi.EditableText.insert_text.side_effect = None
    runtime.Atspi.EditableText.insert_text.return_value = failure == "false_success"
    with pytest.raises(RuntimeError, match="did not"):
        guest.LinuxInput(runtime).type_text(runtime.window, "x")
    assert runtime.Atspi.EditableText.insert_text.call_count == 1
    assert runtime.target.text == ("ad" if failure == "partial" else "abcd")
    runtime.Atspi.generate_keyboard_event.assert_not_called()


def test_click_editable_focuses_without_activating_or_submitting(runtime):
    guest.LinuxInput(runtime).perform({
        "tool": "click", "app": "target", "element": runtime.target, "skip_read_back": True,
    })
    runtime.Atspi.Component.grab_focus.assert_called_once_with(runtime.target)
    runtime.perform_operation.assert_not_called()


def test_inactive_app_cannot_receive_global_keys(runtime):
    runtime.window.active = False
    with pytest.raises(RuntimeError, match="not active"):
        guest.LinuxInput(runtime).press_key(runtime.window, "ctrl+a")
    runtime.Atspi.generate_keyboard_event.assert_not_called()


def test_failed_key_still_releases_modifiers(runtime):
    runtime.Atspi.generate_keyboard_event.side_effect = [True, False, True]
    with pytest.raises(RuntimeError, match="did not confirm"):
        guest.LinuxInput(runtime).press_key(runtime.window, "ctrl+a")
    assert runtime.Atspi.generate_keyboard_event.call_args_list[-1].args == (4, None, "unlock")


def test_cursor_selection_and_field_identity_change_observed_state(runtime):
    adapter = guest.LinuxInput(runtime)
    # Use a new snapshot dictionary for every upstream read.
    runtime.build_snapshot.side_effect = lambda *_args, **_kwargs: {"focusedSummary": "text Target"}
    first = adapter.build_snapshot("target")
    runtime.target.caret = 2
    second = adapter.build_snapshot("target")
    runtime.target.selection = (1, 3)
    third = adapter.build_snapshot("target")
    runtime.target.focused, runtime.decoy.focused = False, True
    fourth = adapter.build_snapshot("target")
    assert len({x["focusedSummary"] for x in (first, second, third, fourth)}) == 4


def test_scroll_changes_requested_scrollbar_without_sending_keys(runtime):
    bar = Node()
    bar.vertical = True
    bar.position = 10
    area = Node(children=[bar])
    runtime.node_role = lambda node: "scroll bar" if node is bar else "panel"
    runtime.extents = lambda _node: {"height": 100, "width": 200}

    def set_value(node, value):
        node.position = value
        return True

    runtime.Atspi.Value = NS(
        get_current_value=lambda node: node.position, get_minimum_value=lambda _node: 0,
        get_maximum_value=lambda _node: 500, get_minimum_increment=lambda _node: 1,
        set_current_value=Mock(side_effect=set_value),
    )
    guest.LinuxInput(runtime).scroll(area, "down", 2)
    assert bar.position == 190
    assert runtime.target.text == "abcd"
    runtime.Atspi.generate_keyboard_event.assert_not_called()
