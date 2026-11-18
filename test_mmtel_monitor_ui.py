import ast
import pathlib
import unittest
from html.parser import HTMLParser


APP_PATH = pathlib.Path(__file__).with_name("app.py")


def monitor_template():
    tree = ast.parse(APP_PATH.read_text(encoding="utf-8"))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == "MMTEL_MONITOR_HTML"
            for target in node.targets
        ):
            continue
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            return node.value.value
    raise AssertionError("MMTEL_MONITOR_HTML assignment not found")


class ElementCollector(HTMLParser):
    def __init__(self):
        super().__init__()
        self.elements = []
        self.text = []

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))

    def handle_data(self, data):
        value = data.strip()
        if value:
            self.text.append(value)


class MonitorTemplateTests(unittest.TestCase):
    def test_page_has_one_ems_csv_export_action(self):
        rendered = monitor_template()
        parsed = ElementCollector()
        parsed.feed(rendered)

        export_links = [
            attrs
            for tag, attrs in parsed.elements
            if tag == "a" and attrs.get("href") == "/mmtel-monitor/export.csv"
        ]
        self.assertEqual(len(export_links), 1)
        self.assertEqual(parsed.text.count("Export for EMS"), 1)
        self.assertEqual(parsed.text.count("Reload page"), 1)

    def test_page_uses_os_column_and_one_filter_for_both_problem_tables(self):
        rendered = monitor_template()
        parsed = ElementCollector()
        parsed.feed(rendered)

        filters = [
            attrs
            for tag, attrs in parsed.elements
            if tag == "input" and attrs.get("id") == "monitor-filter"
        ]
        self.assertEqual(len(filters), 1)
        self.assertEqual(
            filters[0].get("placeholder"),
            "Filter device, build, OS or User-Agent",
        )
        self.assertIn("OS version", parsed.text)
        self.assertNotIn("Android version", parsed.text)
        self.assertIn("Confirmed missing MMTEL", parsed.text)
        self.assertIn("Unable to verify", parsed.text)

        refresh = [
            attrs
            for tag, attrs in parsed.elements
            if tag == "meta" and attrs.get("http-equiv") == "refresh"
        ]
        self.assertEqual(refresh[0].get("content"), "300")

    def test_sort_controls_cover_the_four_operator_fields(self):
        rendered = monitor_template()
        parsed = ElementCollector()
        parsed.feed(rendered)

        keys = {
            attrs["data-sort-key"]
            for tag, attrs in parsed.elements
            if tag == "button" and "data-sort-key" in attrs
        }
        self.assertEqual(keys, {"device", "build", "os", "user_agent"})

        sortable_headers = [
            attrs
            for tag, attrs in parsed.elements
            if tag == "th" and "data-sort-key" in attrs
        ]
        self.assertEqual(len(sortable_headers), 4)
        self.assertTrue(
            all(header.get("aria-sort") == "none" for header in sortable_headers)
        )

    def test_row_metadata_is_explicitly_escaped_for_control_attributes(self):
        template = monitor_template()

        self.assertIn('data-device="{{ row.model|e }}"', template)
        self.assertIn('data-build="{{ row.build_number|e }}"', template)
        self.assertIn('data-os="{{ row.os_version|e }}"', template)
        self.assertIn('data-user-agent="{{ row.user_agent|e }}"', template)
        self.assertIn('data-original-index="{{ loop.index0 }}"', template)

    def test_verified_and_unverified_samsung_actions_are_guarded_and_escaped(self):
        template = monitor_template()

        self.assertIn('row.metadata_status == "verified"', template)
        self.assertIn('href="{{ row.source_url|e }}"', template)
        self.assertIn("Official Samsung record", template)
        self.assertIn('row.metadata_status == "unverified"', template)
        self.assertIn('href="{{ row.research_url|e }}"', template)
        self.assertIn("Find official firmware", template)
        self.assertIn("Unknown — not verified", template)
        self.assertEqual(template.count("{% macro device_table"), 1)
        self.assertEqual(template.count("{{ device_table("), 2)
        self.assertGreaterEqual(template.count('rel="noopener noreferrer"'), 2)

    def test_script_persists_shared_state_and_uses_stable_tie_breaker(self):
        template = monitor_template()

        self.assertIn("function loadMonitorState()", template)
        self.assertIn("function saveMonitorState(state)", template)
        self.assertIn("function applyMonitorControls()", template)
        self.assertIn("ua-mmtel-monitor-controls-v1", template)
        self.assertIn("sessionStorage.getItem", template)
        self.assertIn("sessionStorage.setItem", template)
        self.assertIn("catch", template)
        self.assertIn("originalIndex", template)
        self.assertIn('querySelectorAll("[data-monitor-table]")', template)


if __name__ == "__main__":
    unittest.main()
