import ast
import pathlib
import unittest


APP_PATH = pathlib.Path(__file__).with_name("app.py")


def parsed_app():
    return ast.parse(APP_PATH.read_text(encoding="utf-8"))


def function_named(tree, name):
    return next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == name
    )


def called_function_names(function):
    return {
        node.func.id
        for node in ast.walk(function)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }


def html_template(tree):
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == "HTML"
            for target in node.targets
        ):
            continue
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            return node.value.value
    raise AssertionError("HTML template assignment not found")


def literal_assignment(tree, name):
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if any(
            isinstance(target, ast.Name) and target.id == name
            for target in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{name} assignment not found")


class DefaultSearchTests(unittest.TestCase):
    def test_default_index_uses_registration_not_call_lookup(self):
        index = function_named(parsed_app(), "index")
        calls = called_function_names(index)

        self.assertIn("reg_lookup", calls)
        self.assertIn("vlr_lookup", calls)
        self.assertIn("bearer_lookup", calls)
        self.assertNotIn("ua_lookup", calls)

    def test_template_uses_latest_registration_user_agent(self):
        template = html_template(parsed_app())

        self.assertIn("Latest Registration User-Agent", template)
        self.assertIn("device_row.user_agent", template)
        self.assertIn("Registration History", template)
        self.assertIn("Timestamp (Zurich)", template)
        self.assertIn("Observed at (Zurich)", template)
        self.assertNotIn("Recent Call User-Agents", template)

    def test_ucn_timestamps_are_converted_and_labelled_as_zurich(self):
        tree = parsed_app()
        vlr_calls = called_function_names(function_named(tree, "vlr_lookup"))
        bearer_calls = called_function_names(function_named(tree, "bearer_lookup"))
        template = html_template(tree)

        self.assertIn("format_utc_text_as_zurich", vlr_calls)
        self.assertIn("format_utc_text_as_zurich", bearer_calls)
        self.assertIn("Session Start (Zurich)", template)
        self.assertIn("Registered (Zurich)", template)
        self.assertIn("Last updated (Zurich)", template)
        self.assertIn("Expires (Zurich)", template)

    def test_rat_labels_include_network_generation(self):
        labels = literal_assignment(parsed_app(), "RAT_TYPE_MAP")

        self.assertEqual(labels["1"], "UTRAN (3G UMTS)")
        self.assertEqual(labels["2"], "GERAN (2G GSM/EDGE)")
        self.assertEqual(labels["3"], "WLAN (Wi-Fi)")
        self.assertEqual(labels["5"], "HSPA (3G HSPA+)")
        self.assertEqual(labels["6"], "EUTRAN (4G LTE)")
        self.assertEqual(labels["8"], "NB-IoT (4G LTE)")
        self.assertEqual(labels["9"], "NRTRAN (5G)")
        self.assertEqual(labels["10"], "NR (5G)")


if __name__ == "__main__":
    unittest.main()
