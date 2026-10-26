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
        self.assertNotIn("Recent Call User-Agents", template)


if __name__ == "__main__":
    unittest.main()
