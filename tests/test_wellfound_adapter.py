"""Offline fixture tests for the Wellfound Apify adapter.

These tests do not call Apify.
"""

from __future__ import annotations

import unittest

from core.wellfound_apify import (
    WELLFOUND_ACTOR_ID,
    normalize_wellfound_row,
    wellfound_run_input,
)

# Trimmed johnvc/wellfound-jobs-api row. No profile or contact fields.
JOHNVC_ROW = {
    "title": "Software Engineer",
    "primaryRoleTitle": "Software Engineer",
    "url": "https://wellfound.com/jobs/example",
    "description": "Build product at a startup.",
    "postedAt": "2026-10-01T00:00:00Z",
    "remote": True,
    "locationNames": ["Remote"],
    "salaryMin": 120000,
    "salaryMax": 160000,
    "compensationRaw": "$120k-$160k",
    "company": {
        "name": "Example Startup",
        "slug": "example-startup",
        "url": "https://wellfound.com/company/example-startup",
    },
}

# Flat row shape the previous actor used.
FLAT_ROW = {
    "jobTitle": "Backend Engineer",
    "companyName": "Flat Co",
    "location": "Dublin",
    "jobUrl": "https://wellfound.com/jobs/flat",
    "jobDescription": "Ship APIs.",
    "postedAt": "2026-09-15",
}


class WellfoundAdapterTests(unittest.TestCase):
    def test_actor_id_is_the_live_wellfound_actor(self) -> None:
        self.assertEqual(WELLFOUND_ACTOR_ID, "johnvc~wellfound-jobs-api")
        self.assertNotIn("crawlerbros", WELLFOUND_ACTOR_ID)

    def test_run_input_maps_remote_role_and_cap(self) -> None:
        payload = wellfound_run_input(
            role="software-engineer",
            location="Remote",
            max_items=50,
        )
        self.assertEqual(payload["roles"], ["software-engineer"])
        self.assertEqual(payload["locations"], ["remote"])
        self.assertTrue(payload["remoteOnly"])
        self.assertEqual(payload["maxItems"], 50)
        self.assertTrue(payload["includeDescription"])

    def test_nested_company_and_location_names(self) -> None:
        out = normalize_wellfound_row(JOHNVC_ROW)
        self.assertEqual(out["title"], "Software Engineer")
        self.assertEqual(out["company"], "Example Startup")
        self.assertEqual(out["location"], "Remote")
        self.assertEqual(out["url"], "https://wellfound.com/jobs/example")
        self.assertEqual(out["description"], "Build product at a startup.")
        self.assertEqual(out["posted_at"], "2026-10-01T00:00:00Z")
        self.assertEqual(out["salary"], "$120k-$160k")

    def test_flat_row_still_maps(self) -> None:
        out = normalize_wellfound_row(FLAT_ROW)
        self.assertEqual(out["title"], "Backend Engineer")
        self.assertEqual(out["company"], "Flat Co")
        self.assertEqual(out["location"], "Dublin")
        self.assertEqual(out["url"], "https://wellfound.com/jobs/flat")
        self.assertEqual(out["description"], "Ship APIs.")
        self.assertEqual(out["posted_at"], "2026-09-15")

    def test_nested_company_is_not_stringified(self) -> None:
        out = normalize_wellfound_row(JOHNVC_ROW)
        self.assertNotIn("{", out["company"])
        self.assertIsInstance(out["company"], str)


if __name__ == "__main__":
    unittest.main()
