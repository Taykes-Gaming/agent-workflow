import os
import tempfile
import unittest
from unittest import mock

from tests.fake_gh import FakeGh, run
from wf.gh import GhError, WfError, _parse, common_dir

P = "/users/acme/projectsV2/2"
RATE = "API rate limit exceeded for user ID 1. If you reach out to GitHub Support for help, please include the request ID."


class ErrorTest(unittest.TestCase):
    def test_common_dir_outside_a_checkout_is_a_wf_error(self):
        with tempfile.TemporaryDirectory() as outside, mock.patch.dict(os.environ, {"GIT_CEILING_DIRECTORIES": outside}):
            with self.assertRaises(WfError):
                common_dir(cwd=outside)

    def test_rate_limit_reaches_the_user_as_githubs_message(self):
        gh = FakeGh()
        gh.fail("GET", f"{P}/fields", 403, {"message": RATE, "documentation_url": "https://docs.github.com/rest"})
        code, out, err = run(["move", "web#157", "progress"], gh)
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertEqual(err, f"wf: {RATE}\n")
        self.assertEqual(gh.writes(), [])

    def test_success_prints_one_line(self):
        code, out, err = run(["move", "web#157", "progress"], FakeGh())
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(len(out.splitlines()), 1)

    def test_unknown_option_lists_the_valid_ones(self):
        code, _, err = run(["merged", "web#157", "--model", "gpt", "--findings", "0", "--rounds", "0",
                            "--ktokens", "1"], FakeGh())
        self.assertEqual(code, 1)
        self.assertEqual(err, "wf: no option named 'gpt' on field 'Model'; valid: opus, sonnet, haiku, fable\n")

    def test_read_commands_fail_on_a_field_the_board_lacks(self):
        for cmd in (["board"], ["report", "sprint-3"], ["deploy-report"]):
            gh = FakeGh()
            next(f for f in gh.fields if f["name"] == "Status")["name"] = "State"
            code, out, err = run(cmd, gh)
            self.assertEqual((code, out), (1, ""), cmd)
            self.assertTrue(err.startswith("wf: board has no field named 'Status'; fields: Title, "), err)

    def test_validation_errors_carry_the_detail(self):
        gh = FakeGh()
        gh.fail("PATCH", f"{P}/items/5000", 422,
                {"message": "Validation Failed", "errors": [{"message": "value is not a valid option"}]})
        code, _, err = run(["move", "web#157", "ready"], gh)
        self.assertEqual((code, err), (1, "wf: Validation Failed: value is not a valid option\n"))

    def test_parse_reads_status_headers_and_body(self):
        raw = ('HTTP/2.0 403 Forbidden\r\nContent-Type: application/json\r\n'
               'Link: <https://api.github.com/x?after=Y>; rel="next"\r\n\r\n{"message": "nope"}')
        status, headers, data = _parse(raw, "gh: nope (HTTP 403)")
        self.assertEqual((status, headers["link"], data), (403, '<https://api.github.com/x?after=Y>; rel="next"',
                                                           {"message": "nope"}))
        self.assertEqual(_parse("HTTP/2.0 204 No Content\n\n", "")[::2], (204, None))

    def test_no_http_response_raises_with_gh_stderr(self):
        with self.assertRaisesRegex(GhError, "not logged in"):
            _parse("", "To get started with GitHub CLI, please run: gh auth login\nnot logged in\n")


if __name__ == "__main__":
    unittest.main()
