import io
import httpx
import importlib.util
import openai
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import app as medilife


class MediLifeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.db_path = root / "test.sqlite3"
        self.uploads = root / "uploads"
        self.data_patch = patch.object(medilife, "DATA", self.db_path)
        self.upload_patch = patch.object(medilife, "UPLOADS", self.uploads)
        self.data_patch.start(); self.upload_patch.start()
        medilife.app.config.update(TESTING=True, SECRET_KEY="test-only")
        self.client = medilife.app.test_client()

    def tearDown(self):
        self.data_patch.stop(); self.upload_patch.stop(); self.temp.cleanup()

    def register(self, client, email):
        return client.post("/api/auth/register", json={"email": email, "password": "a-long-test-password"})

    def test_health_and_auth_validation(self):
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["database"], "connected")
        self.assertEqual(self.client.post("/api/auth/register", json={"email":"bad","password":"short"}).status_code, 400)

    def test_health_reports_openai_key_presence_without_exposing_value(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "   "}):
            response = self.client.get("/health")
            self.assertFalse(response.json["ai_configured"])
            self.assertNotIn("OPENAI_API_KEY", response.get_data(as_text=True))
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key-not-a-secret"}):
            response = self.client.get("/health")
            self.assertTrue(response.json["ai_configured"])
            self.assertNotIn("test-key-not-a-secret", response.get_data(as_text=True))

    def test_temporary_demo_mode_blocks_account_writes_and_reports_release(self):
        with patch.dict(os.environ,{"MEDILIFE_DEMO_MODE":"1","RENDER_GIT_COMMIT":"test-commit"}):
            health = self.client.get("/health")
            self.assertTrue(health.json["demo_mode"])
            self.assertEqual(health.json["release"], "test-commit")
            response = self.client.post("/api/auth/register", json={"email":"demo@example.test", "password":"a-long-test-password"})
            self.assertEqual(response.status_code, 503)
            self.assertIn("synthetic sample data only", response.json["error"])
        with patch.dict(os.environ,{"RENDER":"true"},clear=True):
            self.assertTrue(self.client.get("/health").json["demo_mode"])

    def test_render_requires_mounted_storage_for_real_user_writes(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); db_path=root/"medilife.sqlite3"; uploads=root/"uploads"
            with patch.dict(os.environ,{"RENDER":"true","MEDILIFE_DEMO_MODE":"0","PERSISTENT_DATA_DIR":str(root)},clear=True):
                with patch.object(medilife,"DATA",db_path), patch.object(medilife,"UPLOADS",uploads), patch("app.os.path.ismount",return_value=True):
                    self.assertTrue(medilife.durable_storage_ready())
                    self.assertFalse(medilife.demo_only_mode())
                    outside=root.parent/"unmounted-uploads"
                    with patch.object(medilife,"UPLOADS",outside):
                        self.assertFalse(medilife.durable_storage_ready())
                        self.assertTrue(medilife.demo_only_mode())

    def test_ai_credentials_quota_and_timeout_have_distinct_errors(self):
        request=httpx.Request("POST","https://api.openai.com/v1/chat/completions")
        auth_error=openai.AuthenticationError("rejected",response=httpx.Response(401,request=request),body={})
        quota_error=openai.RateLimitError("quota",response=httpx.Response(429,request=request),body={"error":{"code":"insufficient_quota"}})
        timeout_error=openai.APITimeoutError(request=request)
        with medilife.app.test_request_context(), patch.dict(os.environ,{"OPENAI_API_KEY":"test-key-not-a-secret"}):
            for error,status,expected in [(auth_error,502,"rejected"),(quota_error,503,"quota"),(timeout_error,504,"too long")]:
                client=Mock(); client.chat.completions.create.side_effect=error
                with patch("openai.OpenAI",return_value=client):
                    response=medilife.ai_call("summary","synthetic.pdf","synthetic source")
                self.assertEqual(response[1],status)
                self.assertIn(expected,response[0].get_json()["error"])
                self.assertNotIn("test-key-not-a-secret",response[0].get_data(as_text=True))

    def test_auth_and_per_user_isolation(self):
        self.assertEqual(self.register(self.client, "one@example.test").status_code, 201)
        self.assertEqual(self.client.get("/api/documents").status_code, 200)
        other = medilife.app.test_client()
        self.assertEqual(self.register(other, "two@example.test").status_code, 201)
        self.assertEqual(other.get("/api/documents").json["documents"], [])

    def test_upload_validation_duplicate_and_real_ocr_failure_state(self):
        self.register(self.client, "one@example.test")
        invalid = self.client.post("/api/documents", data={"file":(io.BytesIO(b"nope"),"bad.exe")}, content_type="multipart/form-data")
        self.assertEqual(invalid.status_code, 415)
        from PIL import Image
        image = io.BytesIO(); Image.new("RGB", (30, 30), "white").save(image, "PNG"); image_bytes=image.getvalue(); image.seek(0)
        with patch.object(medilife, "extract_document", return_value=("", "OCR unavailable")):
            response = self.client.post("/api/documents", data={"file":(image,"sample.png")}, content_type="multipart/form-data")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json["document"]["status"], "failed")
        self.assertEqual(response.json["document"]["error"], "OCR unavailable")
        duplicate = self.client.post("/api/documents", data={"file":(io.BytesIO(image_bytes),"sample.png")}, content_type="multipart/form-data")
        self.assertEqual(duplicate.status_code, 409)

    def test_private_file_and_unknown_document_access(self):
        self.register(self.client, "one@example.test")
        self.assertEqual(self.client.get("/api/documents/not-owned/file").status_code, 404)
        outsider = medilife.app.test_client()
        self.assertEqual(outsider.get("/api/documents/not-owned/file").status_code, 401)

    def test_document_deletion_is_owner_scoped_and_removes_private_file(self):
        self.register(self.client, "owner@example.test")
        image=io.BytesIO(); from PIL import Image; Image.new("RGB",(30,30),"white").save(image,"PNG"); image.seek(0)
        with patch.object(medilife,"extract_document",return_value=("synthetic report text","")):
            created=self.client.post("/api/documents",data={"file":(image,"synthetic.png")},content_type="multipart/form-data")
        self.assertEqual(created.status_code,201)
        doc_id=created.json["document"]["id"]
        stored=next(self.uploads.iterdir())
        other=medilife.app.test_client(); self.register(other,"other@example.test")
        self.assertEqual(other.delete(f"/api/documents/{doc_id}").status_code,404)
        self.assertTrue(stored.exists())
        self.assertEqual(self.client.delete(f"/api/documents/{doc_id}").status_code,200)
        self.assertFalse(stored.exists())
        self.assertEqual(self.client.get(f"/api/documents/{doc_id}").status_code,404)

    def test_ai_provider_missing_returns_clear_error(self):
        self.register(self.client, "one@example.test")
        response = self.client.post("/api/summary", json={"document_id":"missing","language":"en"})
        self.assertEqual(response.status_code, 404)
        response = self.client.post("/api/chat", json={"message":"Explain my report"})
        self.assertEqual(response.status_code, 404)

    def test_ai_provider_errors_are_not_reported_as_missing_configuration(self):
        with medilife.app.test_request_context():
            with patch.dict(os.environ, {"OPENAI_API_KEY": "   "}):
                response = medilife.ai_call("summary", "report.pdf", "source text")
                self.assertEqual(response[1], 503)
                self.assertIn("not configured", response[0].get_json()["error"])
            with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key-not-a-secret"}):
                with patch("openai.OpenAI", Mock(side_effect=RuntimeError("provider unavailable"))):
                    response = medilife.ai_call("summary", "report.pdf", "source text")
            self.assertEqual(response[1], 502)
            self.assertNotIn("not configured", response[0].get_json()["error"])

    def test_fhir_export_is_authenticated_and_owned(self):
        self.assertEqual(self.client.get("/api/fhir").status_code, 401)
        self.register(self.client, "one@example.test")
        response = self.client.get("/api/fhir")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["entry"][0]["resource"]["resourceType"], "Patient")

    @unittest.skipUnless(importlib.util.find_spec("fitz"), "PyMuPDF is optional in the base runtime")
    def test_real_digital_pdf_extraction_persistence_timeline_and_no_ai_key(self):
        import pymupdf as fitz
        self.register(self.client, "pdf@example.test")
        pdf = fitz.open()
        page = pdf.new_page()
        page.insert_text((72, 72), "Synthetic CBC Report\nHemoglobin: 11.2 g/dL (ref 12.0-15.5) LOW\nWBC: 6.8 x10^3/uL (ref 4.0-10.0)")
        content = pdf.tobytes(); pdf.close()
        response = self.client.post("/api/documents", data={"category":"lab","document_date":"2026-10-03","file":(io.BytesIO(content),"synthetic-cbc.pdf")}, content_type="multipart/form-data")
        self.assertEqual(response.status_code, 201, response.json)
        doc_id = response.json["document"]["id"]
        self.assertEqual(response.json["document"]["status"], "needs_review")
        self.assertIn("Hemoglobin", response.json["document"]["source_text"])
        labs = self.client.get("/api/observations").json["observations"]
        self.assertTrue(any(x["name"].strip()=="Hemoglobin" and x["value"]=="11.2" and x["flagged"]=="LOW" for x in labs), labs)
        self.assertTrue(any(x["document_id"]==doc_id for x in self.client.get("/api/timeline").json["events"]))
        file_response=self.client.get(f"/api/documents/{doc_id}/file")
        self.assertEqual(file_response.status_code,200)
        file_response.close()
        with patch.dict(os.environ,{"OPENAI_API_KEY":""}):
            summary=self.client.post("/api/summary",json={"document_id":doc_id,"language":"en"})
        self.assertEqual(summary.status_code,503)
        self.assertIn("not configured",summary.json["error"])


if __name__ == "__main__":
    unittest.main()
