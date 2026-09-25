#!/usr/bin/env python3
# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Checks that scheduled tasks run in the time zone that was asked for.

Both scheduling tools used to build their Cloud Scheduler job with
time_zone="Asia/Tokyo" written into the generator template, so every demo this
sample generated ran its schedules in Tokyo no matter what the user had asked
for. The cron expression itself was always correct, which is what makes the bug
expensive to notice: a request for "every day at 02:00 PST" registered
0 2 * * * and then fired at 02:00 in Tokyo, with nothing in the log to say the
hour had been reinterpreted.

Four facts are pinned here, with no Google Cloud project and no credentials:

  1. an explicit IANA zone reaches the Cloud Scheduler job AND the Firestore
     task definition, on the plain scheduling path and the autonomous one;
  2. a blank zone becomes UTC, the default Cloud Scheduler applies when timeZone
     is left unset, instead of any one particular region;
  3. a value that is not an IANA name is refused, and the refusal happens
     BEFORE the Firestore write, so a rejected call leaves behind no definition
     that the Data Viewer would go on to render as a live schedule;
  4. a runtime with no IANA database installed still schedules, because a
     missing database must not be able to break every demo at once.

Both runtime trees are checked. AGENTS.md section 14.1 keeps agent_template/
and skills/./templates/ as two front ends over the same files that are allowed
to diverge on purpose, so a zone fix landing in only one of them would leave
the other shipping the original bug.

    python3 test_scheduled_timezone.py
"""

import builtins
import datetime
import logging
import os
import sys
import textwrap
import types
import uuid

SAMPLE = os.path.dirname(os.path.abspath(__file__))

# The two runtime trees that carry the scheduling tools.
TOOLS_FILES = [
    os.path.join(SAMPLE, "agent_template", "adk_agent", "app", "tools.py"),
    os.path.join(SAMPLE, "skills", "ge-demo-generator", "templates", "tools.py"),
]

# The constant that caused the report, and the docstring that taught the model
# to expect it.
HARD_CODED = 'time_zone="Asia/Tokyo"'
DOC_CLAIMED = "Asia/Tokyo timezone"

PLAIN_BASE = {
    "task_name": "nightly_report",
    "task_description": "Build the report",
    "task_prompt": "Build the report",
    "schedule_cron": "0 2 * * *",
    "tool_context": None,
}

AUTONOMOUS_BASE = {
    "task_name": "nightly_report",
    "task_description": "Build the report",
    "schedule_cron": "0 8 * * 1-5",
    "tool_context": None,
}

# name, argument, expected result
RESOLVE_CASES = [
    ("blank resolves to the UTC default", "", "UTC"),
    ("whitespace only is still blank", "   ", "UTC"),
    ("None is still blank", None, "UTC"),
    ("US Pacific passes through", "America/Los_Angeles", "America/Los_Angeles"),
    ("Tokyo is still a valid answer", "Asia/Tokyo", "Asia/Tokyo"),
    ("surrounding space is trimmed", " Europe/Berlin ", "Europe/Berlin"),
]

# name, argument
REFUSED_CASES = [
    ("a raw abbreviation is refused", "PST"),
    ("a zone nobody has heard of is refused", "Mars/Phobos"),
    ("an absolute path is refused", "/etc/localtime"),
    ("a directory traversal is refused", "../../etc/passwd"),
]

# name, time_zone argument, expected job zone, expected stored zone, status
PLAIN_CASES = [
    ("an explicit zone reaches the job",
     "America/Los_Angeles", "America/Los_Angeles", "America/Los_Angeles", "scheduled"),
    ("a blank zone becomes UTC", "", "UTC", "UTC", "scheduled"),
    ("a refused zone registers nothing", "PST", None, None, "error"),
]

AUTONOMOUS_CASES = [
    ("an explicit zone reaches the job",
     "Europe/Berlin", "Europe/Berlin", "Europe/Berlin", "scheduled"),
    ("a blank zone becomes UTC", "", "UTC", "UTC", "scheduled"),
    ("a refused zone registers nothing", "PST", None, None, "error"),
]


def slice_def(text, start_marker):
    """Returns one def, nested or not, together with its body.

    A plain indentation comparison cannot do this. The closing paren of a
    multi-line signature sits in the same column as the def itself, so a rule
    that stops at the next line no deeper than this one truncates every
    parameterised function at its own parameter list. Bracket depth is what
    actually says the header is finished.

    Args:
        text: the whole module source.
        start_marker: what the def begins with, indentation included.

    Returns:
        The function source, ready to exec.
    """
    start = text.index(start_marker)
    line_start = text.rfind("\n", 0, start) + 1
    indent = len(text[line_start:start])
    lines = []
    depth = 0
    for line in text[start:].split("\n"):
        too_shallow = lines and depth <= 0 and line.strip() and (
            len(line) - len(line.lstrip()) <= indent)
        if too_shallow:
            break
        lines.append(line)
        depth += (
            line.count("(") + line.count("[") + line.count("{")
            - line.count(")") - line.count("]") - line.count("}")
        )
    return "\n".join(lines)


class FakeDoc:
    """One Firestore document, held in a dict."""

    def __init__(self, doc_id):
        self.doc_id = doc_id
        self.data = None

    def set(self, data):
        self.data = dict(data)

    def get(self):
        return self

    def to_dict(self):
        return self.data

    @property
    def exists(self):
        return self.data is not None


class FakeCollection:
    """One Firestore collection, holding FakeDoc objects by id."""

    def __init__(self, store):
        self._store = store

    def document(self, doc_id):
        if doc_id not in self._store:
            self._store[doc_id] = FakeDoc(doc_id)
        return self._store[doc_id]


class FakeFirestore:
    """Just enough Firestore for the scheduling tools, all in memory."""

    def __init__(self):
        self.stores = {}

    def collection(self, name):
        return FakeCollection(self.stores.setdefault(name, {}))


def install_stubs():
    """Puts a fake google.cloud.scheduler_v1 in sys.modules.

    The scheduling tools import the scheduler lazily inside the function, so a
    module registered here is what they pick up. `from google.cloud import
    scheduler_v1` resolves the parent packages on the way, which is why they
    have to exist even though nothing in them is used.

    Returns:
        The list the fake create_job appends each built job to.
    """
    created = []

    class Job:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.name = kwargs.get("name", "")

    class PubsubTarget:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class Client:
        def create_job(self, parent=None, job=None):
            created.append(job)
            return Job(name=job.name)

    module = types.ModuleType("google.cloud.scheduler_v1")
    module.Job = Job
    module.PubsubTarget = PubsubTarget
    module.CloudSchedulerClient = Client
    cloud = types.ModuleType("google.cloud")
    cloud.scheduler_v1 = module
    google = types.ModuleType("google")
    google.cloud = cloud
    sys.modules["google"] = google
    sys.modules["google.cloud"] = cloud
    sys.modules["google.cloud.scheduler_v1"] = module
    return created


def load_tools(path):
    """Compiles the module and lifts out just the scheduling code.

    Importing tools.py for real would need ADK, dotenv and a live Firestore
    client, none of which exist off a deployed demo. The scheduling tools touch
    only four module globals - os, _task_uuid, _task_dt and ToolContext - plus
    the lazily imported scheduler, so supplying those is enough to run the real
    code rather than a transcription of it.

    Args:
        path: path to one tools.py.

    Returns:
        Tuple of the module source and a namespace holding the callables.
    """
    with open(path, encoding="utf-8") as handle:
        src = handle.read()
    compile(src, os.path.basename(path), "exec")
    space = {
        "os": os,
        "_task_uuid": uuid,
        "_task_dt": datetime,
        "ToolContext": object,
    }
    # The UTC default is a module level constant, so it is lifted out next to
    # the function that reads it.
    at = src.index("_SCHED_DEFAULT_TIME_ZONE = ")
    exec(src[at:src.index("\n", at)], space)
    exec(slice_def(src, "def _resolve_schedule_time_zone("), space)
    exec(slice_def(src, "def register_scheduled_task("), space)
    exec(
        textwrap.dedent(slice_def(src, "    def register_scheduled_autonomous_task(")),
        space,
    )
    space["_MANAGED_AGENT_ID"] = "projects/stub/locations/global/agents/1"
    return src, space


def stored_zone(fake, demo_id="demo-tz"):
    """The time_zone the tool persisted on the task definition, if any."""
    docs = fake.stores.get(demo_id + "_task_definitions", {})
    if not docs:
        return None
    return next(iter(docs.values())).data.get("time_zone")


def run_case(fn, base, kwargs, created):
    """Runs one scheduling call against the stubs.

    Args:
        fn: the scheduling tool under test.
        base: the keyword arguments every case shares.
        kwargs: the arguments specific to this case.
        created: the list the fake scheduler appends each job to.

    Returns:
        Tuple of the job's time zone, the stored time zone and the tool status.
        The first two are None when the tool refused the call before creating
        anything at all.
    """
    created.clear()
    fake = FakeFirestore()
    previous = getattr(builtins, "_firestore_client", None)
    builtins._firestore_client = fake
    os.environ.update(
        DEMO_ID="demo-tz",
        GOOGLE_CLOUD_PROJECT="stub-project",
        GOOGLE_CLOUD_LOCATION="us-central1",
    )
    try:
        result = fn(**base, **kwargs)
    finally:
        builtins._firestore_client = previous
    job_zone = created[0].kwargs.get("time_zone") if created else None
    return job_zone, stored_zone(fake), str(result.get("status", ""))


def resolve_without_database(resolve):
    """Calls the resolver as if no IANA database were installed.

    A slim container image can ship without one, and then every name fails to
    resolve, valid zones included. The resolver has to notice that and pass the
    caller's value through, so a demo still schedules and Cloud Scheduler stays
    the authority on what it accepts.

    Args:
        resolve: the _resolve_schedule_time_zone function.

    Returns:
        Whatever the resolver returns with no database present.
    """
    real = sys.modules.get("zoneinfo")

    class ZoneInfoNotFoundError(Exception):
        pass

    class ZoneInfo:
        def __init__(self, key):
            raise ZoneInfoNotFoundError(key)

    stub = types.ModuleType("zoneinfo")
    stub.ZoneInfo = ZoneInfo
    stub.ZoneInfoNotFoundError = ZoneInfoNotFoundError
    sys.modules["zoneinfo"] = stub
    try:
        return resolve("America/Los_Angeles")
    finally:
        if real is None:
            del sys.modules["zoneinfo"]
        else:
            sys.modules["zoneinfo"] = real


def check(label, ok, got):
    """Prints one result line.

    Returns:
        0 when the case passed, 1 when it failed, for the running tally.
    """
    print("  %-4s %-42s %s" % ("ok" if ok else "FAIL", label, got))
    return 0 if ok else 1


def run_tools_cases(space, created):
    """Runs the scheduling cases for both tools in one tree."""
    failures = 0
    for tool, base, cases in (
        ("register_scheduled_task", PLAIN_BASE, PLAIN_CASES),
        ("register_scheduled_autonomous_task", AUTONOMOUS_BASE, AUTONOMOUS_CASES),
    ):
        fn = space[tool]
        for name, arg, want_job, want_stored, want_status in cases:
            job, stored, status = run_case(fn, base, {"time_zone": arg}, created)
            ok = job == want_job and stored == want_stored and status == want_status
            got = "job=%s stored=%s status=%s" % (job, stored, status)
            failures += check(name, ok, got)
    return failures


def run_tree(path):
    """Runs every case against one runtime tree.

    Returns:
        How many cases failed.
    """
    print(os.path.relpath(path, SAMPLE))
    src, space = load_tools(path)
    resolve = space["_resolve_schedule_time_zone"]
    failures = 0
    for name, arg, want in RESOLVE_CASES:
        got = resolve(arg)
        failures += check(name, got == want, repr(got))
    for name, arg in REFUSED_CASES:
        try:
            resolve(arg)
            got = "accepted"
        except ValueError as err:
            got = "ValueError: " + str(err)[:36]
        failures += check(name, got.startswith("ValueError"), got)
    got = resolve_without_database(resolve)
    ok = got == "America/Los_Angeles"
    failures += check("a runtime with no IANA database still schedules", ok, repr(got))
    failures += run_tools_cases(space, install_stubs())
    for name, needle in (
        ("no job is built with a constant zone", HARD_CODED),
        ("the docstring no longer promises Tokyo", DOC_CLAIMED),
    ):
        present = needle in src
        failures += check(name, not present, "present" if present else "absent")
    print()
    return failures


def main():
    """Runs every case against every tree and reports.

    Returns:
        Process exit status, 0 when everything passed.
    """
    logging.disable(logging.CRITICAL)
    failures = 0
    for path in TOOLS_FILES:
        failures += run_tree(path)
    per_tree = len(RESOLVE_CASES) + len(REFUSED_CASES) + 1 + 6 + 2
    total = per_tree * len(TOOLS_FILES)
    if failures:
        print("%d of %d case(s) FAILED" % (failures, total))
        return 1
    print("All %d case(s) passed." % total)
    return 0


if __name__ == "__main__":
    sys.exit(main())