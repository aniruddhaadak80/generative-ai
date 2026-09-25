#!/usr/bin/env python3
# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Gates the time zone a scheduled task actually fires in (v12.25).

Every schedule this template creates is a Cloud Scheduler job, and a cron
expression carries no time zone of its own: the job's own `time_zone` decides
what "02:00" means. The template used to write one fixed zone into both creation
paths, so a demo fired in that zone's local time whichever zone the request
named, and `update_scheduled_task` could not move the zone at all because its
field mask covered only `schedule`. Both are invisible until a schedule is
supposed to run at a local time and does not.

Catching that needs a deployed demo and a live scheduler, so the three
scheduling tools are sliced out of the real templates and run here against a
stubbed Firestore and a stubbed scheduler client. Both trees that hold a copy
are checked: `agent_template/` and the skill's `templates/`, which section 14.1
of AGENTS.md lets diverge but not on a defect like this one.

    python3 test_scheduler_timezone.py
"""

import ast
import builtins
import logging
import datetime
import os
import sys
import textwrap
import types
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = [
    os.path.join(HERE, "agent_template", "adk_agent", "app", "tools.py"),
    os.path.join(HERE, "skills", "ge-demo-generator", "templates", "tools.py"),
]
DEMO_ID = "demo-stub"


def slice_function(src, name):
    """The exact source of one function, by name.

    The line numbers come from the parser, so the block ends where the function
    ends rather than at the next line that happens to sit at the same indent -
    a signature spans several lines, and a docstring carries brackets of its
    own. Where a name is defined twice, the first definition in the file wins:
    the skill's copy also carries an indent-0 fallback stub of the autonomous
    tool at the end of the file, behind the real one.
    """
    lines = src.split("\n")
    found = sorted(
        (node for node in ast.walk(ast.parse(src))
         if isinstance(node, ast.FunctionDef) and node.name == name),
        key=lambda node: node.lineno,
    )
    if not found:
        raise AssertionError("no function named %s" % name)
    return textwrap.dedent("\n".join(lines[found[0].lineno - 1:found[0].end_lineno]))


def slice_helper(src):
    """The default zone and the normalizer that reads it."""
    lines = src.split("\n")
    first = last = None
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "_DEFAULT_TASK_TIME_ZONE"
            for t in node.targets
        ):
            first = node.lineno
        if isinstance(node, ast.FunctionDef) and node.name == "_resolve_task_time_zone":
            last = node.end_lineno
    if first is None or last is None:
        raise AssertionError("no time zone helper to slice")
    return "\n".join(lines[first - 1:last])


class _Doc:
    exists = True

    def __init__(self):
        self.data = {}

    def set(self, payload):
        self.data = dict(payload)

    def get(self):
        return self

    def to_dict(self):
        return dict(self.data)

    def update(self, payload):
        self.data.update(payload)


class _Collection:
    def __init__(self, store, name):
        self.store = store
        self.name = name

    def document(self, doc_id):
        return self.store.setdefault((self.name, doc_id), _Doc())


class _Firestore:
    def __init__(self):
        self.docs = {}

    def collection(self, name):
        return _Collection(self.docs, name)

    def definition(self):
        for (name, _), doc in self.docs.items():
            if name.endswith("_task_definitions"):
                return doc
        raise AssertionError("no task definition was written")


class _Job:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.name = kwargs.get("name", "")


class _PubsubTarget:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class _Client:
    """Keeps whatever Job a create or update call was handed."""

    def __init__(self, sink):
        self.sink = sink

    def create_job(self, parent=None, job=None):
        self.sink["created"] = job
        return job

    def update_job(self, job=None, update_mask=None):
        self.sink["updated"] = job
        self.sink["mask"] = list(getattr(update_mask, "paths", []) or [])
        return job


def install_stubs(sink):
    """Puts a stand-in `google.cloud.scheduler_v1` in place.

    A deployed demo has the real SDK and Cloud Scheduler is what finally judges
    the zone. Neither is reachable here, and the point of this gate is the value
    the template hands over, not what the service does with it.
    """
    google = types.ModuleType("google")
    cloud = types.ModuleType("google.cloud")
    sched = types.ModuleType("google.cloud.scheduler_v1")
    sched.Job = _Job
    sched.PubsubTarget = _PubsubTarget
    sched.CloudSchedulerClient = lambda *a, **k: _Client(sink)
    cloud.scheduler_v1 = sched
    google.cloud = cloud
    sys.modules["google"] = google
    sys.modules["google.cloud"] = cloud
    sys.modules["google.cloud.scheduler_v1"] = sched
    try:
        from google.protobuf import field_mask_pb2  # noqa: F401
    except ImportError:
        protobuf = types.ModuleType("google.protobuf")
        field_mask = types.ModuleType("google.protobuf.field_mask_pb2")

        class FieldMask:
            def __init__(self, paths=None):
                self.paths = list(paths or [])

        field_mask.FieldMask = FieldMask
        protobuf.field_mask_pb2 = field_mask
        google.protobuf = protobuf
        sys.modules["google.protobuf"] = protobuf
        sys.modules["google.protobuf.field_mask_pb2"] = field_mask


def load_tools(path, sink):
    """Compiles the scheduling helpers out of one tools.py.

    Returns (namespace, source, error). A template that no longer carries the
    helper, or that no longer parses, comes back as an error string instead of
    an exception, so the caller can report it and still read the source.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            src = handle.read()
        namespace = {
            "os": os,
            "ToolContext": object,
            "_task_uuid": uuid,
            "_task_dt": datetime,
            "_MANAGED_AGENT_ID": "agents/stub",
        }
        blocks = [
            slice_helper(src),
            slice_function(src, "register_scheduled_task"),
            slice_function(src, "update_scheduled_task"),
            slice_function(src, "register_scheduled_autonomous_task"),
        ]
        for block in blocks:
            exec(compile(block, path, "exec"), namespace)  # noqa: S102
    except (AssertionError, SyntaxError, KeyError, ValueError) as exc:
        return None, src if "src" in dir() else "", "%s: %s" % (type(exc).__name__, exc)
    return namespace, src, None


CASES_REGISTER = [
    # name, kwargs, expected zone
    ("the requested zone reaches the job", {"time_zone": "America/Los_Angeles"},
     "America/Los_Angeles"),
    ("no zone asked for means UTC", {}, "UTC"),
    ("an empty zone means UTC", {"time_zone": ""}, "UTC"),
    ("a padded zone is trimmed", {"time_zone": "  Europe/Berlin  "},
     "Europe/Berlin"),
    ("the zone is kept on the definition", {"time_zone": "Asia/Kolkata"},
     "Asia/Kolkata"),
]

CASES_AUTONOMOUS = [
    ("the requested zone reaches the job", {"time_zone": "America/Los_Angeles"},
     "America/Los_Angeles"),
    ("no zone asked for means UTC", {}, "UTC"),
    ("a zone named by the user is honoured, not overridden",
     {"time_zone": "Australia/Sydney"}, "Australia/Sydney"),
    ("the zone is kept on the definition", {"time_zone": "Asia/Kolkata"},
     "Asia/Kolkata"),
]

CASES_UPDATE = [
    # name, stored zone, kwargs, expected zone on the job
    ("moving the clock keeps the zone", "America/New_York", {}, "America/New_York"),
    ("an explicit zone replaces the stored one", "America/New_York",
     {"time_zone": "Europe/Paris"}, "Europe/Paris"),
    ("a definition with no zone on it lands on UTC", None, {}, "UTC"),
    ("an empty zone keeps the stored one", "America/New_York", {"time_zone": ""},
     "America/New_York"),
]


def run_tree(path):
    """Runs every case against one tools.py.

    Returns a list of (name, got, expected, ok) so the caller decides how to
    count, rather than a counter this function could fail to carry back.
    """
    sink = {}
    install_stubs(sink)
    firestore = _Firestore()
    previous = getattr(builtins, "_firestore_client", None)
    builtins._firestore_client = firestore
    os.environ["DEMO_ID"] = DEMO_ID
    os.environ["GOOGLE_CLOUD_PROJECT"] = "stub-project"
    os.environ["GOOGLE_CLOUD_LOCATION"] = "us-central1"
    results = []
    try:
        namespace, src, error = load_tools(path, sink)

        def record(name, got, expected):
            results.append((name, got, expected, got == expected))

        # A zone written as a literal anywhere in the template is the defect
        # this gate exists for, in a call site or in the prose around one.
        record("no zone literal is left in the template",
               src.count("Asia/Tokyo"), 0)
        record("the scheduling tools load and run", error or "loaded", "loaded")
        if error:
            return results

        register = namespace["register_scheduled_task"]
        update = namespace["update_scheduled_task"]
        autonomous = namespace["register_scheduled_autonomous_task"]

        for name, kwargs, expected in CASES_REGISTER:
            firestore.docs.clear()
            result = register("task", "desc", "prompt", "0 2 * * *", **kwargs)
            record(name,
                   (sink["created"].kwargs.get("time_zone"),
                    firestore.definition().data.get("time_zone"),
                    result.get("time_zone")),
                   (expected, expected, expected))

        for name, kwargs, expected in CASES_AUTONOMOUS:
            firestore.docs.clear()
            result = autonomous("task", "desc", "0 2 * * *", **kwargs)
            record(name,
                   (sink["created"].kwargs.get("time_zone"),
                    firestore.definition().data.get("time_zone"),
                    result.get("time_zone")),
                   (expected, expected, expected))

        for name, stored, kwargs, expected in CASES_UPDATE:
            firestore.docs.clear()
            task_id = "t1"
            doc = firestore.collection(DEMO_ID + "_task_definitions").document(task_id)
            doc.data = {"task_id": task_id, "task_type": "scheduled",
                        "schedule_cron": "0 2 * * *"}
            if stored:
                doc.data["time_zone"] = stored
            result = update(task_id, "0 3 * * *", **kwargs)
            record(name,
                   (sink["updated"].kwargs.get("time_zone"),
                    "time_zone" in sink["mask"],
                    doc.data.get("time_zone"),
                    result.get("time_zone")),
                   (expected, True, expected, expected))
    finally:
        if previous is None:
            del builtins._firestore_client
        else:
            builtins._firestore_client = previous
    return results


def main():
    logging.disable(logging.CRITICAL)
    total = failures = 0
    for path in TOOLS:
        print(os.path.relpath(path, HERE))
        for name, got, expected, ok in run_tree(path):
            total += 1
            failures += 0 if ok else 1
            print("  %-4s %-52s got=%s" % ("ok" if ok else "FAIL", name, got))
    if failures:
        print("\n%d of %d case(s) FAILED" % (failures, total))
        return 1
    print("\nAll %d case(s) passed." % total)
    return 0


if __name__ == "__main__":
    sys.exit(main())
