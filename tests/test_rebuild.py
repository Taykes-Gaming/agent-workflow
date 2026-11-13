import unittest

from tests.fake_gh import FakeGh, field_id, option, run

P = "/users/acme/projectsV2/2"


class RebuildTest(unittest.TestCase):
    def test_dry_run_lists_only_changes(self):
        gh = FakeGh()
        code, out, err = run(["rebuild", "--dry-run"], gh)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out.splitlines(), [
            "would set mobile#12 Status → Ready",   # In review is lost by a rebuild
            "would set mobile#20 Status → Ready",   # no labels: Size and Deploy untouched
        ])
        self.assertEqual(gh.writes(), [])

    def test_rebuild_writes_the_changed_targets(self):
        gh = FakeGh()
        code, out, _ = run(["rebuild"], gh)
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), ["mobile#12 Status → Ready", "mobile#20 Status → Ready"])
        self.assertEqual(gh.writes(), [
            ("PATCH", f"{P}/items/5001", {"fields": [{"id": field_id(gh, "Status"), "value": option(gh, "Status", "Ready")}]}),
            ("PATCH", f"{P}/items/5004", {"fields": [{"id": field_id(gh, "Status"), "value": option(gh, "Status", "Ready")}]}),
        ])

    def test_rebuild_skips_items_already_correct(self):
        gh = FakeGh()
        gh.items = [i for i in gh.items if i["id"] in (5000, 5002, 5003)]  # Status, Size and Deploy match
        code, out, _ = run(["rebuild"], gh)
        self.assertEqual((code, out, gh.writes()), (0, "", []))

    def test_rebuild_sends_one_patch_per_changed_item(self):
        gh = FakeGh()
        gh.items = [i for i in gh.items if i["id"] == 5000]
        for f in gh.items[0]["fields"]:
            if f["name"] == "Size":
                f["value"] = None
        gh.items[0]["fields"][0]["value"] = None  # Status
        code, out, _ = run(["rebuild"], gh)
        self.assertEqual(out.splitlines(), ["web#157 Status → Backlog", "web#157 Size → M"])
        self.assertEqual(gh.writes(), [("PATCH", f"{P}/items/5000", {"fields": [
            {"id": field_id(gh, "Status"), "value": option(gh, "Status", "Backlog")},
            {"id": field_id(gh, "Size"), "value": option(gh, "Size", "M")}]})])

    def test_without_deploy_configured_deploy_is_untouched(self):
        code, out, _ = run(["rebuild", "--dry-run"], FakeGh(), deploy=False)
        self.assertEqual(code, 0)
        self.assertNotIn("Deploy", out)
        self.assertEqual(len(out.splitlines()), 2)


if __name__ == "__main__":
    unittest.main()
