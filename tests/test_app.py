import io
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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

    def test_ai_provider_missing_returns_clear_error(self):
        self.register(self.client, "one@example.test")
        response = self.client.post("/api/summary", json={"document_id":"missing","language":"en"})
        self.assertEqual(response.status_code, 404)
        response = self.client.post("/api/chat", json={"message":"Explain my report"})
        self.assertEqual(response.status_code, 404)

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
