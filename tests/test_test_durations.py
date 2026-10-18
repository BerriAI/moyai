from xml.etree import ElementTree

import pytest

from scripts.update_test_durations import durations_from_reports


def report(tmp_path, cases):
    root = ElementTree.Element('testsuites')
    suite = ElementTree.SubElement(root, 'testsuite')
    for attributes in cases:
        ElementTree.SubElement(suite, 'testcase', attributes)
    path = tmp_path / 'report.xml'
    ElementTree.ElementTree(root).write(path)
    return path


def test_junit_preserves_class_and_parameterized_node_ids(tmp_path):
    module = tmp_path / 'tests/test_example.py'
    module.parent.mkdir()
    module.touch()
    path = report(tmp_path, [
        {'classname': 'tests.test_example', 'name': 'test_example[a.b-x]', 'time': '2.5'},
        {'classname': 'tests.test_example.TestGroup', 'name': 'test_method', 'time': '0.1'},
    ])
    assert durations_from_reports([path], tmp_path) == {
        'tests/test_example.py::test_example[a.b-x]': 2.5,
        'tests/test_example.py::TestGroup::test_method': 0.1,
    }


@pytest.mark.parametrize('seconds', ['nan', 'inf', '-1'])
def test_invalid_timings_are_rejected(tmp_path, seconds):
    (tmp_path / 'test_example.py').touch()
    path = report(tmp_path, [
        {'classname': 'test_example', 'name': 'test_example', 'time': seconds},
    ])
    with pytest.raises(ValueError):
        durations_from_reports([path], tmp_path)


def test_duplicate_shards_are_rejected(tmp_path):
    (tmp_path / 'test_example.py').touch()
    path = report(tmp_path, [
        {'classname': 'test_example', 'name': 'test_example', 'time': '1'},
    ])
    with pytest.raises(ValueError, match='duplicate'):
        durations_from_reports([path, path], tmp_path)
