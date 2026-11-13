import unittest

from tests.fake_gh import FakeGh, field_id, option, run

P = "/users/acme/projectsV2/2"


class MoveTest(unittest.TestCase):
    def test_move_progress_sets_status_and_drops_backlog_label(self):
        gh = FakeGh()
        code, out, err = run(["move", "web#157", "progress"], gh)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, "web#157 → In progress\n")
        patches = [c for c in gh.calls if c[0] == "PATCH"]
        self.assertEqual(patches, [(
            "PATCH", f"{P}/items/5000",
            {"fields": [{"id": field_id(gh, "Status"), "value": option(gh, "Status", "In progress")}]},
        )])
        self.assertEqual(gh.writes(), patches + [
            ("DELETE", "/repos/acme/web/issues/157/labels/backlog", None),
        ])

    def test_backlog_label_is_matched_exactly_not_as_a_prefix(self):
        gh = FakeGh()
        item = next(i for i in gh.items if i["content"]["number"] == 157 and i["content"]["repository_url"].endswith("/web"))
        labels = item["content"]["labels"]
        labels.append({"id": 7999, "name": "backlog-grooming"})
        code, _, _ = run(["move", "web#157", "ready"], gh)
        self.assertEqual(code, 0)
        deletes = [c[1] for c in gh.writes() if c[0] == "DELETE"]
        self.assertEqual(deletes, ["/repos/acme/web/issues/157/labels/backlog"])

    def test_a_label_already_gone_does_not_abort_after_the_field_write(self):
        gh = FakeGh()
        gh.fail("DELETE", "/repos/acme/web/issues/157/labels/backlog", 404,
                {"message": "Label does not exist"})
        code, out, err = run(["move", "web#157", "ready"], gh)
        self.assertEqual((code, out, err), (0, "web#157 → Ready\n", ""))
        self.assertEqual([c[:2] for c in gh.writes()], [
            ("PATCH", "/users/acme/projectsV2/2/items/5000"),
            ("DELETE", "/repos/acme/web/issues/157/labels/backlog"),
        ])

    def test_other_label_errors_still_fail(self):
        for status, message in ((403, "Resource not accessible by integration"), (404, "Not Found")):
            gh = FakeGh()
            gh.fail("DELETE", "/repos/acme/web/issues/157/labels/backlog", status, {"message": message})
            code, _, err = run(["move", "web#157", "ready"], gh)
            self.assertEqual((code, err), (1, f"wf: {message}\n"), status)

    def test_unknown_verb_is_a_usage_error_and_makes_no_call(self):
        gh = FakeGh()
        code, out, err = run(["move", "web#157", "sideways"], gh)
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("wf: unknown status 'sideways'", err)
        self.assertEqual(gh.calls, [])

    def test_bad_ref_is_a_usage_error_and_makes_no_call(self):
        for ref in ("web 157", "web#", "#157", "nope#1", "web#157,mobile#12"):
            gh = FakeGh()
            code, _, err = run(["move", ref, "ready"], gh)
            self.assertEqual(code, 2, ref)
            self.assertEqual(gh.calls, [], ref)

    def test_several_refs_print_one_line_each(self):
        gh = FakeGh()
        code, out, _ = run(["move", "web#157", "mobile#12", "acme/core#3", "backlog"], gh)
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), ["web#157 → Backlog", "mobile#12 → Backlog", "core#3 → Backlog"])
        # web#157 already carries `backlog`; the other two gain it.
        self.assertEqual([c for c in gh.writes() if "/labels" in c[1]], [
            ("POST", "/repos/acme/mobile/issues/12/labels", {"labels": ["backlog"]}),
            ("POST", "/repos/acme/core/issues/3/labels", {"labels": ["backlog"]}),
        ])
        # Fields read once per run, items listed once per run.
        self.assertEqual(sum(1 for c in gh.calls if c[1] == f"{P}/fields"), 1)
        self.assertEqual(sum(1 for c in gh.calls if c[1].startswith(f"{P}/items?")), 1)

    def test_missing_item_is_added_by_issue_id_then_patched(self):
        gh = FakeGh()
        code, out, _ = run(["move", "web#999", "ready"], gh)
        self.assertEqual((code, out), (0, "web#999 → Ready\n"))
        self.assertEqual(gh.writes()[:2], [
            ("POST", f"{P}/items", {"type": "Issue", "id": 8999}),
            ("PATCH", f"{P}/items/9901",
             {"fields": [{"id": field_id(gh, "Status"), "value": option(gh, "Status", "Ready")}]}),
        ])

    def test_ready_and_progress_refuse_an_issue_without_a_milestone_before_any_write(self):
        for verb in ("ready", "progress"):
            gh = FakeGh()
            next(i for i in gh.items if i["content"]["number"] == 12)["content"]["milestone"] = None
            code, out, err = run(["move", "web#157", "mobile#12", verb], gh)
            self.assertEqual((code, out), (1, ""), verb)
            self.assertIn("wf: no milestone on mobile#12;", err)
            self.assertEqual(gh.writes(), [], verb)

    def test_backlog_and_review_need_no_milestone(self):
        for verb in ("backlog", "review"):
            gh = FakeGh()
            gh.milestone = None
            self.assertEqual(run(["move", "web#157", verb], gh)[0], 0, verb)
            self.assertFalse(any(c[1] == "/repos/acme/web/issues/157" for c in gh.calls), verb)

    def test_json_and_quiet(self):
        code, out, _ = run(["move", "web#157", "ready", "--json"], FakeGh())
        self.assertEqual((code, out), (0, '{"ref": "web#157", "status": "Ready"}\n'))
        code, out, err = run(["move", "web#157", "ready", "--quiet"], FakeGh())
        self.assertEqual((code, out, err), (0, "", ""))


if __name__ == "__main__":
    unittest.main()
