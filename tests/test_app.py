import importlib
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

from fastapi.testclient import TestClient


class MFGDataSharingAppTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.data_path = os.path.join(self.tempdir.name, "data.json")
        os.environ["MFG_USERNAME"] = "demo-user"
        os.environ["MFG_PASSWORD"] = "demo-pass"
        os.environ["MFG_DATA_PATH"] = self.data_path
        sys.modules.pop("app", None)
        self.app_module = importlib.import_module("app")
        self.client = TestClient(self.app_module.app)

    def tearDown(self):
        sys.modules.pop("app", None)
        self.tempdir.cleanup()

    def read_data_file(self):
        with open(self.data_path, "r", encoding="utf-8") as fh:
            return json.load(fh)

    def login(self, username="demo-user", password="demo-pass", follow_redirects=True):
        return self.client.post(
            "/login",
            data={"username": username, "password": password},
            follow_redirects=follow_redirects,
        )

    def test_root_redirects_to_login_when_not_authenticated(self):
        response = self.client.get("/", follow_redirects=False)

        self.assertEqual(response.status_code, 307)
        self.assertEqual(response.headers["location"], "/login")

    def test_api_data_requires_login_session(self):
        response = self.client.get("/api/data")

        self.assertEqual(response.status_code, 401)

    def test_upload_redirects_to_login_when_not_authenticated(self):
        response = self.client.post(
            "/upload",
            files={"file": ("data.csv", b"Batch,Parameter,D0\n500L_GMP,VCD,1.23\n", "text/csv")},
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 303)
        self.assertIn("/login", response.headers["location"])

    def test_login_page_renders_form(self):
        response = self.client.get("/login")

        self.assertEqual(response.status_code, 200)
        self.assertIn("MFG Data Sharing", response.text)
        self.assertIn('name="username"', response.text)
        self.assertIn('name="password"', response.text)

    def test_login_rejects_bad_password(self):
        response = self.login(password="bad-pass", follow_redirects=False)

        self.assertEqual(response.status_code, 401)

    def test_login_sets_session_cookie_and_allows_access(self):
        response = self.login(follow_redirects=False)

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/")
        self.assertIn("mfg_session", response.cookies)

    def test_get_api_data_returns_v2_shape_after_login(self):
        self.login()
        response = self.client.get("/api/data")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("50L", payload)
        self.assertIn("2L", payload)
        self.assertIn("satellite", payload)
        self.assertIn("500L", payload)
        self.assertIn("updated_at", payload)
        self.assertNotIn("update_log", payload)
        self.assertEqual(len(payload["50L"]["vcd"]), 18)

    def test_data_file_initializes_to_v2_structure(self):
        payload = self.app_module.load_data()

        self.assertTrue(os.path.exists(self.data_path))
        self.assertIn("updated_at", payload)
        self.assertNotIn("update_log", payload)
        self.assertEqual(sorted(payload.keys()), ["2L", "500L", "50L", "satellite", "updated_at"])
        self.assertIsNone(payload["updated_at"])
        self.assertEqual(payload["500L"]["glucose"], [None] * 18)

    def test_upload_merges_non_empty_values_after_login(self):
        self.login()
        response = self.client.post(
            "/upload",
            files={
                "file": (
                    "data.csv",
                    (
                        "Batch,Parameter,D0,D1,D5\n"
                        "500L_GMP,VCD,0.987,2.10,\n"
                        "500L_GMP,Gluc,,,4.44\n"
                    ).encode("utf-8"),
                    "text/csv",
                )
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 303)
        self.assertTrue(response.headers["location"].startswith("/?uploaded="))

        stored = self.read_data_file()
        self.assertEqual(stored["500L"]["vcd"][1], 2.10)
        self.assertEqual(stored["500L"]["glucose"][5], 4.44)
        self.assertIn("updated_at", stored)
        self.assertNotIn("update_log", stored)

    def test_upload_does_not_modify_50l_reference(self):
        self.login()
        response = self.client.post(
            "/upload",
            files={
                "file": (
                    "data.csv",
                    "Batch,Parameter,D0,D1\n50L_Pilot,VCD,9.99,9.99\n".encode("utf-8"),
                    "text/csv",
                )
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 303)
        stored = self.read_data_file()
        self.assertIsNone(stored["50L"]["vcd"][0])
        self.assertIsNone(stored["50L"]["vcd"][1])

    def test_logout_redirects_to_login(self):
        self.login()

        response = self.client.get("/logout", follow_redirects=False)

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/login")

    def test_index_page_shows_pm_note_and_hides_dashboard_summary(self):
        self.login()

        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn('class="section-caption">For PM Operation</div>', response.text)
        self.assertNotIn("Dashboard Summary", response.text)
        self.assertIn(">GMP MFG Data Sharing</h1>", response.text)
        self.assertNotIn("Minimal dashboard for shared login, CSV upload, and chart review.", response.text)
        self.assertLess(response.text.index('id="chartGrid"'), response.text.index('class="panel upload-card"'))

    def test_health_endpoint_is_public_and_reports_ok(self):
        response = self.client.get("/health")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "ok")
        self.assertTrue(payload["data_ok"])
        self.assertIn("updated_at", payload)

    def test_export_requires_login(self):
        response = self.client.get("/api/export")

        self.assertEqual(response.status_code, 401)

    def test_export_returns_csv_that_round_trips_through_upload(self):
        self.login()
        self.client.post(
            "/upload",
            files={
                "file": (
                    "data.csv",
                    "Batch,Parameter,D0,D1\n500L_GMP,VCD,0.987,2.10\n".encode("utf-8"),
                    "text/csv",
                )
            },
            follow_redirects=False,
        )

        response = self.client.get("/api/export")

        self.assertEqual(response.status_code, 200)
        self.assertIn("text/csv", response.headers["content-type"])
        self.assertIn("attachment", response.headers["content-disposition"])
        self.assertIn("500L_GMP,VCD", response.text)
        self.assertIn("0.987", response.text)

    def test_upload_rejects_out_of_range_values(self):
        self.login()
        response = self.client.post(
            "/upload",
            files={
                "file": (
                    "data.csv",
                    (
                        "Batch,Parameter,D0,D1\n"
                        "500L_GMP,VIA,150,99.1\n"  # 150% viability is impossible
                    ).encode("utf-8"),
                    "text/csv",
                )
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 303)
        self.assertIn("rejected=1", response.headers["location"])
        stored = self.read_data_file()
        self.assertIsNone(stored["500L"]["viability"][0])
        self.assertEqual(stored["500L"]["viability"][1], 99.1)

    def test_session_survives_process_restart_with_stable_secret(self):
        os.environ["MFG_SECRET_KEY"] = "stable-test-secret"
        sys.modules.pop("app", None)
        module = importlib.import_module("app")
        client = TestClient(module.app)
        login = client.post(
            "/login",
            data={"username": "demo-user", "password": "demo-pass"},
            follow_redirects=False,
        )
        token = login.cookies["mfg_session"]

        # Simulate a restart: reload the module so in-memory state is discarded.
        sys.modules.pop("app", None)
        restarted = importlib.import_module("app")
        restarted_client = TestClient(restarted.app)
        restarted_client.cookies.set("mfg_session", token)

        response = restarted_client.get("/api/data")

        self.assertEqual(response.status_code, 200)
        os.environ.pop("MFG_SECRET_KEY", None)

    def test_write_data_falls_back_when_atomic_replace_hits_busy_device(self):
        data = self.app_module.build_default_data()
        data["500L"]["glucose"][5] = 6.66

        real_replace = os.replace

        def flaky_replace(src, dst):
            if str(dst) == self.data_path:
                raise OSError(16, "Device or resource busy")
            return real_replace(src, dst)

        with mock.patch.object(self.app_module.os, "replace", side_effect=flaky_replace):
            self.app_module.write_data(data)

        stored = self.read_data_file()
        self.assertEqual(stored["500L"]["glucose"][5], 6.66)
        self.assertIn("updated_at", stored)
        self.assertNotIn("update_log", stored)


if __name__ == "__main__":
    unittest.main()
