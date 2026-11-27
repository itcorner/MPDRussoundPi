"""Whether a release contains an installed development build (mpdrussoundpi.common.sourcehistory)."""

import unittest

from mpdrussoundpi.common import sourcehistory

RELEASE, INSTALLED = "a" * 40, "b" * 40


class ContainsTests(unittest.TestCase):
    def test_the_compare_status_decides(self):
        asked = []
        for status, expected in (("ahead", True), ("identical", True), ("behind", False), ("diverged", False)):
            with self.subTest(status=status):
                def fetch(url, status=status):
                    asked.append(url)
                    return {"status": status}
                self.assertIs(sourcehistory.contains(RELEASE, INSTALLED, fetch=fetch), expected)
        self.assertEqual(asked[0], sourcehistory.API + f"/compare/{INSTALLED}...{RELEASE}")
        self.assertTrue(sourcehistory.API.startswith("https://api.github.com/repos/itcorner/MPDRussoundPi"))

    def test_equal_commits_need_no_request_and_bad_input_or_answers_conclude_nothing(self):
        self.assertTrue(sourcehistory.contains(RELEASE, RELEASE, fetch=lambda url: self.fail("no request")))
        for release, installed in (("abc", INSTALLED), (RELEASE, "B" * 40), (RELEASE, "")):
            with self.subTest(release=release, installed=installed), self.assertRaises(sourcehistory.HistoryError):
                sourcehistory.contains(release, installed, fetch=lambda url: {"status": "ahead"})
        for answer in ({}, {"status": "unknown"}):
            with self.subTest(answer=answer), self.assertRaises(sourcehistory.HistoryError):
                sourcehistory.contains(RELEASE, INSTALLED, fetch=lambda url, answer=answer: answer)


if __name__ == "__main__":
    unittest.main()
