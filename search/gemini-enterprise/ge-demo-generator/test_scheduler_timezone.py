import ast
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_FILES = (
    os.path.join(ROOT, "agent_template", "adk_agent", "app", "tools.py"),
    os.path.join(ROOT, "skills", "ge-demo-generator", "templates", "tools.py"),
)
REGISTRATION_FUNCTIONS = (
    "register_scheduled_task",
    "update_scheduled_task",
    "register_scheduled_autonomous_task",
)


def find_function(tree, name):
    matches = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == name
    ]
    if not matches:
        raise AssertionError(f"missing function: {name}")
    preferred = [
        node
        for node in matches
        if any(arg.arg == "time_zone" for arg in node.args.args)
    ]
    return (preferred or matches)[0]


def argument_default(function, name):
    arguments = function.args.args
    defaults = function.args.defaults
    offset = len(arguments) - len(defaults)
    for index, default in enumerate(defaults):
        if arguments[offset + index].arg == name:
            return ast.literal_eval(default)
    raise AssertionError(f"missing default: {function.name}.{name}")


def has_time_zone_job_argument(function):
    for node in ast.walk(function):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Attribute) or node.func.attr != "Job":
            continue
        for keyword in node.keywords:
            if (
                keyword.arg == "time_zone"
                and isinstance(keyword.value, ast.Name)
                and keyword.value.id == "time_zone"
            ):
                return True
    return False


def has_persisted_time_zone(function):
    for node in ast.walk(function):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values):
            if (
                isinstance(key, ast.Constant)
                and key.value == "time_zone"
                and isinstance(value, ast.Name)
                and value.id == "time_zone"
            ):
                return True
    return False


def has_time_zone_update_mask(function):
    for node in ast.walk(function):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Attribute) or node.func.attr != "FieldMask":
            continue
        for keyword in node.keywords:
            if keyword.arg != "paths" or not isinstance(
                keyword.value, (ast.List, ast.Tuple)
            ):
                continue
            values = {
                element.value
                for element in keyword.value.elts
                if isinstance(element, ast.Constant)
            }
            if {"schedule", "time_zone"}.issubset(values):
                return True
    return False


def load_normalizer(tree, path):
    function = find_function(tree, "_normalize_scheduler_time_zone")
    module = ast.Module(body=[function], type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {}
    exec(compile(module, path, "exec"), namespace)
    return namespace[function.name]


def check_template(path):
    source = open(path, encoding="utf-8").read()
    tree = ast.parse(source, filename=path)
    normalizer = load_normalizer(tree, path)
    assert normalizer(None) == "UTC"
    assert normalizer("") == "UTC"
    assert normalizer("  America/Los_Angeles  ") == "America/Los_Angeles"
    assert "Asia/Tokyo" not in source

    functions = {name: find_function(tree, name) for name in REGISTRATION_FUNCTIONS}
    for name in ("register_scheduled_task", "register_scheduled_autonomous_task"):
        assert argument_default(functions[name], "time_zone") == "UTC"
    assert argument_default(functions["update_scheduled_task"], "time_zone") == ""
    for name, function in functions.items():
        assert has_time_zone_job_argument(function), name
    assert has_persisted_time_zone(functions["register_scheduled_task"])
    assert has_persisted_time_zone(functions["register_scheduled_autonomous_task"])
    assert has_persisted_time_zone(functions["update_scheduled_task"])
    assert has_time_zone_update_mask(functions["update_scheduled_task"])


def main():
    for path in TEMPLATE_FILES:
        check_template(path)
    code = open(os.path.join(ROOT, "app", "Code.gs"), encoding="utf-8").read()
    skill = open(
        os.path.join(ROOT, "skills", "ge-demo-generator", "SKILL.md"),
        encoding="utf-8",
    ).read()
    verifier = open(
        os.path.join(
            ROOT,
            "skills",
            "ge-demo-generator",
            "templates",
            "scripts",
            "verify_and_heal.py",
        ),
        encoding="utf-8",
    ).read()
    assert "v12.24-public" in code
    assert "2.26.0" in skill
    assert "v2.26.0" in verifier
    print("Scheduler timezone checks passed for both runtime templates.")


if __name__ == "__main__":
    sys.exit(main())
