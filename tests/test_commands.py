import copy
import unittest
from unittest import mock

from tests import fake_gh

from tests.fake_gh import FakeGh, field_id, option, run

P = "/users/acme/projectsV2/2"


def patch(gh, item, field, value):
    return ("PATCH", f"{P}/items/{item}", {"fields": [{"id": field_id(gh, field), "value": value}]})


def multi(gh, item, pairs):
    return ("PATCH", f"{P}/items/{item}", {"fields": [{"id": field_id(gh, f), "value": v} for f, v in pairs]})


class SizeTest(unittest.TestCase):
    def test_size_sets_field_and_swaps_effort_label(self):
        gh = FakeGh()
        code, out, _ = run(["size", "web#157", "L"], gh)
        self.assertEqual((code, out), (0, "web#157 → Size L\n"))
        self.assertEqual(gh.writes(), [
            patch(gh, 5000, "Size", option(gh, "Size", "L")),
            ("DELETE", "/repos/acme/web/issues/157/labels/effort%3AM", None),
            ("POST", "/repos/acme/web/issues/157/labels", {"labels": ["effort:L"]}),
        ])

    def test_bad_size_is_usage(self):
        gh = FakeGh()
        self.assertEqual(run(["size", "web#157", "XXL"], gh)[0], 2)
        self.assertEqual(gh.calls, [])


class DeployTest(unittest.TestCase):
    def test_deploy_with_release(self):
        gh = FakeGh()
        code, out, _ = run(["deploy", "mobile#12", "staging", "--release", "build 41"], gh)
        self.assertEqual((code, out), (0, "mobile#12 → Deploy Staging (release build 41)\n"))
        self.assertEqual(gh.writes(), [
            multi(gh, 5001, [("Release", "build 41"), ("Deploy", option(gh, "Deploy", "Staging"))]),
            ("DELETE", "/repos/acme/mobile/issues/12/labels/deploy%3Apending", None),
            ("POST", "/repos/acme/mobile/issues/12/labels", {"labels": ["deploy:staging"]}),
        ])

    def test_deploy_fails_when_not_configured(self):
        gh = FakeGh()
        code, _, err = run(["deploy", "web#157", "na"], gh, deploy=False)
        self.assertEqual(code, 1)
        self.assertIn("needs [fields].deploy", err)
        self.assertEqual(gh.calls, [])


class MergedTest(unittest.TestCase):
    ARGS = ["merged", "web#157", "--model", "opus", "--findings", "3", "--rounds", "1", "--ktokens", "820"]

    def test_merged_writes_fields_then_done_then_deploy(self):
        gh = FakeGh()
        code, out, _ = run(self.ARGS + ["--turns", "97"], gh)
        self.assertEqual(code, 0)
        self.assertEqual(out, "web#157: opus, findings 3, rounds 1, kTokens 820, turns 97 (web#40) → Done, "
                              "Deploy Pending, closed\n")
        self.assertEqual(gh.writes(), [
            multi(gh, 5000, [
                ("Model", option(gh, "Model", "opus")), ("Review Findings", 3), ("Fix Rounds", 1),
                ("Agent kTokens", 820), ("Turns", 97), ("Status", option(gh, "Status", "Done")),
                ("Deploy", option(gh, "Deploy", "Pending"))]),
            ("POST", "/repos/acme/web/issues/157/labels", {"labels": ["deploy:pending"]}),
            ("DELETE", "/repos/acme/web/issues/157/labels/backlog", None),
            ("PATCH", "/repos/acme/web/issues/157", {"state": "closed", "state_reason": "completed"}),
        ])

    def test_merged_writes_all_fields_in_one_patch(self):
        gh = FakeGh()
        code, _, _ = run(self.ARGS + ["--turns", "97"], gh)
        self.assertEqual(code, 0)
        patches = [c for c in gh.writes() if c[1] == f"{P}/items/5000"]
        self.assertEqual(len(patches), 1)
        self.assertEqual([f["id"] for f in patches[0][2]["fields"]],
                         [field_id(gh, n) for n in ("Model", "Review Findings", "Fix Rounds", "Agent kTokens",
                                                    "Turns", "Status", "Deploy")])

    def test_merged_refuses_without_a_merged_pr_and_writes_nothing(self):
        gh = FakeGh()
        gh.timeline = [{"event": "cross-referenced", "source": {"issue": {
            "number": 41, "pull_request": {"merged_at": None}}}}, {"event": "labeled"}]
        code, _, err = run(self.ARGS, gh)
        self.assertEqual((code, err), (1, "wf: no merged PR references web#157; merge it first, or name it with --pr\n"))
        self.assertEqual(gh.writes(), [])

    def test_merged_pr_flag_checks_that_pr(self):
        gh = FakeGh()
        gh.extra[("GET", "/repos/acme/web/pulls/41")] = (200, {"merged_at": None})
        code, _, err = run(self.ARGS + ["--pr", "web#41"], gh)
        self.assertEqual((code, err), (1, "wf: web#41 is not merged\n"))
        self.assertEqual(gh.writes(), [])
        gh = FakeGh()
        gh.timeline = []
        gh.extra[("GET", "/repos/acme/mobile/pulls/7")] = (200, {"merged_at": "2026-10-28T10:00:00Z"})
        code, out, _ = run(self.ARGS + ["--pr", "mobile#7"], gh)
        self.assertEqual(code, 0)
        self.assertIn("(mobile#7) → Done", out)

    def test_merged_prefers_the_pr_that_closes_the_issue_over_one_that_mentions_it(self):
        def pr(number, body, repo="acme/web"):
            return {"event": "cross-referenced", "source": {"issue": {
                "number": number, "body": body, "repository": {"full_name": repo},
                "pull_request": {"merged_at": "2026-10-28T10:00:00Z"}}}}
        gh = FakeGh()
        gh.timeline = [pr(40, "Next: #157 is Ready."), pr(9, "Closes #157", repo="acme/mobile"),
                       pr(41, "Fixes: #157")]
        code, out, _ = run(self.ARGS, gh)
        self.assertEqual(code, 0)
        self.assertIn("(web#41) → Done", out)
        gh = FakeGh()  # GitHub's casing of the repo, a cross-repo reference and the issue URL all count
        for body, repo in (("Closes #157", "acme/web"),
                           ("Fixes acme/web#157", "acme/mobile"),
                           ("Resolves https://github.com/acme/web/issues/157", "acme/mobile")):
            gh.timeline = [pr(40, "Next: #157 is Ready."), pr(43, body, repo=repo)]
            code, out, _ = run(self.ARGS, gh)
            self.assertEqual(code, 0)
            self.assertIn("#43) → Done", out)
        gh = FakeGh()
        gh.timeline = [pr(40, "Next: #157 is Ready."), pr(42, "See #157.")]
        code, out, _ = run(self.ARGS, gh)
        self.assertEqual(code, 0)
        self.assertIn("(web#40) → Done", out)

    def test_merged_leaves_a_closed_issue_alone(self):
        gh = FakeGh()
        code, out, _ = run(["merged", "core#3", "--model", "opus", "--findings", "0", "--rounds", "0",
                            "--ktokens", "1"], gh)
        self.assertEqual(code, 0)
        self.assertFalse(out.rstrip().endswith("closed"), out)
        self.assertFalse(any(c[1] == "/repos/acme/core/issues/3" for c in gh.writes()))

    def test_merged_deploy_na_and_no_deploy_field(self):
        gh = FakeGh()
        code, out, _ = run(self.ARGS + ["--deploy", "na"], gh)
        self.assertTrue(out.rstrip().endswith("→ Done, Deploy n/a, closed"), out)
        gh = FakeGh()
        code, out, _ = run(self.ARGS, gh, deploy=False)
        self.assertEqual(out, "web#157: opus, findings 3, rounds 1, kTokens 820 (web#40) → Done, closed\n")
        self.assertFalse(any("deploy" in str(c).lower() for c in gh.writes()))

    def test_merged_rejects_staging_and_negative_counts(self):
        for extra in (["--deploy", "staging"], ["--findings", "-1"]):
            gh = FakeGh()
            self.assertEqual(run(self.ARGS + extra, gh)[0], 2, extra)
            self.assertEqual(gh.calls, [])


class ReadCommandsTest(unittest.TestCase):
    def test_board_drift_both_ways_and_check(self):
        gh = FakeGh()
        content = lambda n: next(i for i in gh.items if i["content"]["number"] == n)
        core3 = content(3)
        core3["content"]["state"] = "open"  # Done on the board, open on GitHub
        mobile12 = content(12)
        mobile12["content"]["state"] = "closed"  # closed, still In review
        code, out, _ = run(["board"], gh)
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines()[0], "drift: 1 Done but open: core#3; 1 closed but not Done: mobile#12")
        self.assertEqual(run(["board", "--check"], gh)[0], 1)
        self.assertEqual(run(["board", "--check"], fake_gh.spend_fields(FakeGh()), fields=fake_gh.SPEND_FIELDS)[0], 0)

    def test_board_says_which_spend_fields_are_missing_without_failing_check(self):
        code, out, _ = run(["board", "--check"], FakeGh())
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines()[1], "fields: cache_ktokens, review_ktokens not configured; wf merged records "
                                              "no Cache kTokens or Review kTokens. Name them under [fields] in "
                                              ".workflow.toml (e.g. cache_ktokens = \"Cache kTokens\", "
                                              "review_ktokens = \"Review kTokens\"), then wf fields --create")
        code, out, _ = run(["board"], fake_gh.spend_fields(FakeGh()), fields=fake_gh.SPEND_FIELDS)
        self.assertEqual(code, 0)
        self.assertNotIn("fields:", out)

    def test_board_warns_on_a_fix_stream_past_its_cap(self):
        gh = FakeGh()
        base = gh.items[1]
        gh.items = []
        for n in range(1, 13):
            it = copy.deepcopy(base)
            it["id"] = 6000 + n
            it["content"]["number"] = n
            it["content"]["created_at"] = "2026-10-01T00:00:00Z"
            it["content"]["labels"] = [{"name": "fix-4b"}]
            gh.items.append(it)
        with mock.patch.object(fake_gh, "CONFIG", fake_gh.CONFIG.replace(
                'deploy_prefix = "deploy:"', 'deploy_prefix = "deploy:"\nfix_prefix = "fix-"')):
            code, out, _ = run(["board", "--check"], gh)
        self.assertEqual(code, 1)
        self.assertRegex(out.splitlines()[1], r"^fix stream fix-4b: 12 issues over \d+ days, past the cap")
        for it in gh.items:
            it["content"]["state"] = "closed"
            it["fields"] = []
        code, out, _ = run(["board"], gh)  # a stream with nothing open is history, not a warning
        self.assertNotIn("fix stream", out)

    def test_board_does_not_list_done_na_items_as_not_live(self):
        code, out, _ = run(["board"], fake_gh.spend_fields(FakeGh()), fields=fake_gh.SPEND_FIELDS)
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), [
            "drift: none",
            "web#157 M Backlog Delete activity blobs server-side when an activity doc is d…",
            "mobile#12 S In review Show the lap table on the activity screen",
            "mobile#20 - - Placeholder for the onboarding flow",
        ])
        self.assertEqual(len(out.splitlines()[1].split(" ", 3)[3]), 60)

    def test_board_still_lists_done_items_not_live(self):
        gh = fake_gh.spend_fields(FakeGh())
        core3 = next(i for i in gh.items if i["content"]["number"] == 3)
        next(f for f in core3["fields"] if f["name"] == "Deploy")["value"]["name"]["raw"] = "Staging"
        out = run(["board"], gh, fields=fake_gh.SPEND_FIELDS)[1]
        self.assertIn("core#3 L Done", out)

    def test_board_check_help_matches_behaviour(self):
        from wf import cli
        sub = next(a for a in cli.parser()._actions if a.dest == "command").choices["board"]
        text = " ".join(next(a for a in sub._actions if "--check" in a.option_strings).help.split())
        self.assertEqual(text, "exit 1 on drift or a fix stream past its cap")

    def test_board_caps_at_40_lines(self):
        gh = fake_gh.spend_fields(FakeGh(page_size=7))
        base = gh.items[1]
        gh.items = []
        for n in range(1, 46):
            it = copy.deepcopy(base)
            it["id"] = 6000 + n
            it["content"]["number"] = n
            gh.items.append(it)
        code, out, _ = run(["board"], gh, fields=fake_gh.SPEND_FIELDS)
        lines = out.splitlines()
        self.assertEqual(len(lines), 42)
        self.assertEqual(lines[-1], "… and 5 more")
        self.assertEqual(sum(1 for c in gh.calls if "/items?" in c[1]), 7)  # 45 items, 7 per page

    def test_report_table(self):
        code, out, _ = run(["report", "sprint-3"], FakeGh())
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), [
            "| Model | Slices | Size mix | Review findings | Fix rounds | Agent kTokens | kTokens / slice | Turns |",
            "|---|---|---|---|---|---|---|---|",
            "| - | 1 | M | 0 | 0 | 0 | 0 | 0 |",
            "| opus | 1 | L | 3 | 2 | 1250 | 1250 | 140 |",
            "| sonnet | 1 | S | 2 | 1 | 410 | 410 | 55 |",
        ])

    def test_deploy_report(self):
        gh = FakeGh()
        gh.items[3]["fields"][3]["value"] = next(
            o for f in gh.fields if f["name"] == "Deploy" for o in f["options"] if o["name"]["raw"] == "Staging")
        code, out, _ = run(["deploy-report"], gh)
        self.assertEqual((code, out), (0, "web#150\tStaging\t2026-10-01\tRename the stage Worker route\n"))

    def _many_not_live(self, n=45):
        gh = FakeGh()
        base = gh.items[3]  # Done, Deploy set
        gh.items = []
        for i in range(1, n + 1):
            it = copy.deepcopy(base)
            it["id"] = 6000 + i
            it["content"]["number"] = i
            next(f for f in it["fields"] if f["name"] == "Deploy")["value"] = next(
                o for f in gh.fields if f["name"] == "Deploy" for o in f["options"] if o["name"]["raw"] == "Staging")
            gh.items.append(it)
        return gh

    def test_deploy_report_caps_at_max_lines(self):
        code, out, _ = run(["deploy-report"], self._many_not_live())
        lines = out.splitlines()
        self.assertEqual((code, len(lines), lines[-1]), (0, 41, "… and 5 more"))

    def test_deploy_report_all_prints_everything(self):
        code, out, _ = run(["deploy-report", "--all"], self._many_not_live())
        self.assertEqual((code, len(out.splitlines())), (0, 45))
        self.assertEqual(len(run(["deploy-report", "--json"], self._many_not_live())[1].splitlines()), 45)

    def test_board_json_drift_is_capped(self):
        gh = FakeGh()
        base = gh.items[1]
        gh.items = []
        for n in range(1, 13):
            it = copy.deepcopy(base)
            it["id"] = 6000 + n
            it["content"]["number"] = n
            it["content"]["state"] = "closed"
            gh.items.append(it)
        import json
        rec = json.loads(run(["board", "--json"], gh)[1].splitlines()[0])["drift"]
        self.assertEqual((len(rec["closed_not_done"]), rec["total"]["closed_not_done"]), (10, 12))


if __name__ == "__main__":
    unittest.main()


class FieldsTest(unittest.TestCase):
    def test_reports_and_creates_only_missing_fields(self):
        gh = FakeGh()
        code, out, _ = run(["fields"], gh)
        self.assertEqual((code, out), (0, "fields: all 9 configured fields exist\n"))
        gh = FakeGh()
        gh.fields = [f for f in gh.fields if f["name"] not in ("Deploy", "Review Findings")]
        code, out, _ = run(["fields"], gh, fields=fake_gh.SPEND_FIELDS)
        self.assertEqual(out, "fields: 4 missing: Review Findings, Cache kTokens, Review kTokens, Deploy; "
                              "wf fields --create adds them\n")
        self.assertEqual(gh.writes(), [])
        code, out, _ = run(["fields", "--create"], gh, fields=fake_gh.SPEND_FIELDS)
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), ["Review Findings: created, number", "Cache kTokens: created, number",
                                            "Review kTokens: created, number",
                                            "Deploy: created, single_select (n/a, Pending, Staging, Production)"])
        bodies = [c[2] for c in gh.writes()]
        self.assertEqual([c[1] for c in gh.writes()], [f"{P}/fields"] * 4)
        self.assertEqual(bodies[3]["single_select_options"][1], {"name": "Pending", "color": "GRAY", "description": ""})
        code, out, _ = run(["fields"], gh, fields=fake_gh.SPEND_FIELDS)
        self.assertEqual(out, "fields: all 11 configured fields exist\n")

    def test_status_options_are_reported_never_rewritten(self):
        gh = FakeGh()
        status = next(f for f in gh.fields if f["name"] == "Status")
        status["options"] = status["options"][:2]
        code, out, _ = run(["fields", "--create"], gh)
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("Status: options In progress, Blocked, In review, Done missing;"), out)
        self.assertEqual(gh.writes(), [])
