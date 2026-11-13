import unittest

from tests.fake_gh import FakeGh, field_id, option, run
from wf.gh import GhError

P = "/users/acme/projectsV2/2"
ALREADY = {"message": "Content already exists in this project", "status": "422"}


def node(item_id, number, login):
    return {"fullDatabaseId": str(item_id), "project": {"number": number, "owner": {"login": login}}}


def items_of_issue(*nodes):
    return {"data": {"repository": {"issue": {"projectItems": {"nodes": list(nodes)}}}}}


class AddItemTest(unittest.TestCase):
    def test_already_on_board_resolves_the_item_with_one_graphql_query(self):
        gh = FakeGh()
        gh.fail("POST", f"{P}/items", 422, ALREADY)
        # On two boards; the other one is listed first.
        gh.graphql_data = items_of_issue(node(111, 9, "acme"), node(222, 2, "Acme"))
        code, out, err = run(["move", "web#999", "progress"], gh)
        self.assertEqual((code, out, err), (0, "web#999 → In progress\n", ""))
        graphql = [c for c in gh.calls if c[1] == "graphql"]
        self.assertEqual(len(graphql), 1)
        self.assertEqual(graphql[0][2]["variables"], {"owner": "acme", "name": "web", "number": 999})
        self.assertEqual([c for c in gh.calls if c[0] == "PATCH"], [(
            "PATCH", f"{P}/items/222",
            {"fields": [{"id": field_id(gh, "Status"), "value": option(gh, "Status", "In progress")}]},
        )])

    def test_already_on_board_with_error_details_still_takes_the_fallback(self):
        gh = FakeGh()
        gh.fail("POST", f"{P}/items", 422, {**ALREADY, "errors": [{"message": "Issue is already on this project"}]})
        gh.graphql_data = items_of_issue(node(222, 2, "acme"))
        code, out, _ = run(["move", "web#999", "progress"], gh)
        self.assertEqual((code, out), (0, "web#999 → In progress\n"))

    def test_org_owned_board(self):
        gh = FakeGh(owner_type="org")
        gh.fail("POST", "/orgs/acme/projectsV2/2/items", 422, ALREADY)
        gh.graphql_data = items_of_issue(node(444, 2, "acme"))
        code, out, _ = run(["move", "web#999", "progress"], gh, owner_type="org")
        self.assertEqual((code, out), (0, "web#999 → In progress\n"))
        self.assertEqual([c[1] for c in gh.calls if c[0] == "PATCH"], ["/orgs/acme/projectsV2/2/items/444"])

    def test_another_422_is_reported_without_graphql(self):
        gh = FakeGh()
        gh.fail("POST", f"{P}/items", 422, {"message": "Validation Failed"})
        gh.graphql_data = items_of_issue(node(222, 2, "acme"))
        code, out, err = run(["move", "web#999", "progress"], gh)
        self.assertEqual((code, out, err), (1, "", "wf: Validation Failed\n"))
        self.assertFalse([c for c in gh.calls if c[1] == "graphql"])

    def test_on_another_board_only_is_an_error(self):
        gh = FakeGh()
        gh.fail("POST", f"{P}/items", 422, ALREADY)
        gh.graphql_data = items_of_issue(node(111, 9, "acme"), node(333, 2, "someone-else"))
        code, _, err = run(["move", "web#999", "progress"], gh)
        self.assertEqual((code, err), (1, "wf: web#999 is on the board but could not be found\n"))
        self.assertFalse([c for c in gh.calls if c[0] == "PATCH"])

    def test_partial_data_with_node_errors_is_used(self):
        gh = FakeGh()
        gh.fail("POST", f"{P}/items", 422, ALREADY)
        gh.graphql_data = {**items_of_issue(None, node(222, 2, "acme")),
                           "errors": [{"message": "Resource not accessible by integration"}]}
        code, out, _ = run(["move", "web#999", "progress"], gh)
        self.assertEqual((code, out), (0, "web#999 → In progress\n"))

    def test_a_non_json_graphql_response_is_a_gh_error(self):
        gh = FakeGh()
        gh.graphql_data = "<html>bad gateway</html>"
        with self.assertRaisesRegex(GhError, "unexpected GraphQL response"):
            gh.graphql("query { viewer { login } }")

    def test_graphql_errors_are_gh_errors(self):
        gh = FakeGh()
        gh.graphql_data = {"errors": [{"message": "API rate limit already exceeded for user ID 1."}]}
        with self.assertRaisesRegex(GhError, "rate limit already exceeded"):
            gh.graphql("query { viewer { login } }")


if __name__ == "__main__":
    unittest.main()
