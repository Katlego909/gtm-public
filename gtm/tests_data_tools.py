"""Deterministic data-file analysis (gtm/data_tools.py) on real CSV/XLSX
uploads: the numbers agents propose must come from rows, not estimates."""

import io
import shutil
import tempfile

import openpyxl
from django.core.files.base import ContentFile
from django.test import TestCase, override_settings

from gtm.data_tools import DataError, build_data_tools, compute, profile, to_number
from gtm.models import AssessmentSession, CategoryDocument, DeliveryDocument
from gtm.models_workspace import Workspace

DEALS_CSV = (
    "Deal,Stage,Amount,Close Date,Owner\n"
    "Acme,Closed Won,\"$12,000\",2026-07-03,Sam\n"
    "Beta,Closed Lost,8000,2026-07-10,Sam\n"
    "Cora,Closed Won,20000,2026-08-15,Priya\n"
    "Dune,Negotiation,5000,,Priya\n"
    "Echo,Closed Lost,3000,2026-09-01,Sam\n"
)

MEDIA_ROOT = tempfile.mkdtemp()


@override_settings(MEDIA_ROOT=MEDIA_ROOT, STORAGES={
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
})
class DataToolsTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA_ROOT, ignore_errors=True)

    def setUp(self):
        self.workspace = Workspace.objects.create(name="Data Co")
        self.session = AssessmentSession.objects.create(owner_client_id="data-client", workspace=self.workspace)
        self.csv = DeliveryDocument.objects.create(
            session=self.session, original_filename="deals.csv", file_type="csv",
            file=ContentFile(DEALS_CSV.encode(), name="deals.csv"),
        )

    def _xlsx_doc(self):
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.append([])  # blank first row: header detection skips it
        sheet.append(["Ticket", "Hours to first reply"])
        for ticket, hours in [("T1", 2), ("T2", 6), ("T3", 4), ("T4", None)]:
            sheet.append([ticket, hours])
        buffer = io.BytesIO()
        workbook.save(buffer)
        return CategoryDocument.objects.create(
            session=self.session, category="delivery", original_filename="tickets.xlsx", file_type="xlsx",
            file=ContentFile(buffer.getvalue(), name="tickets.xlsx"),
        )

    def test_win_rate_ratio(self):
        result = compute(
            self.csv, "ratio",
            filters=[{"column": "stage", "op": "eq", "value": "Closed Won"}],
            denominator_filters=[{"column": "Stage", "op": "contains", "value": "closed"}],
        )
        self.assertEqual(result["value"], 50.0)
        self.assertEqual(result["denominator_rows"], 4)

    def test_average_won_deal_size_parses_currency(self):
        result = compute(self.csv, "mean", "Amount", filters=[{"column": "Stage", "op": "eq", "value": "Closed Won"}])
        self.assertEqual(result["value"], 16000.0)

    def test_date_filters(self):
        result = compute(self.csv, "count", filters=[
            {"column": "Close Date", "op": "gte", "value": "2026-07-01"},
            {"column": "Close Date", "op": "lt", "value": "2026-08-01"},
        ])
        self.assertEqual(result["value"], 2.0)

    def test_xlsx_median_skips_blank_cells(self):
        result = compute(self._xlsx_doc(), "median", "Hours to first reply")
        self.assertEqual(result["value"], 4.0)

    def test_unknown_column_lists_real_columns(self):
        with self.assertRaisesMessage(DataError, "Columns are: Deal, Stage, Amount, Close Date, Owner"):
            compute(self.csv, "sum", "Revenue")

    def test_unknown_operation_and_filter_op_are_rejected(self):
        with self.assertRaises(DataError):
            compute(self.csv, "eval")
        with self.assertRaises(DataError):
            compute(self.csv, "count", filters=[{"column": "Stage", "op": "regex", "value": ".*"}])

    def test_profile_shows_columns_and_values(self):
        text = profile(self.csv)
        self.assertIn("5 rows", text)
        self.assertIn("Stage (text", text)
        self.assertIn("Closed Won", text)
        self.assertIn("Amount (number", text)

    def test_tools_are_scoped_to_the_workspace(self):
        other = AssessmentSession.objects.create(owner_client_id="other", workspace=Workspace.objects.create(name="X"))
        tools = {t.__name__: t for t in build_data_tools(workspace=other.workspace)}
        self.assertIn("No CSV or Excel", tools["list_data_files"]())
        self.assertIn("No CSV or Excel file", tools["profile_data_file"](str(self.csv.pk)))

        tools = {t.__name__: t for t in build_data_tools(workspace=self.workspace)}
        self.assertIn("deals.csv", tools["list_data_files"]())
        self.assertTrue(tools["compute_metric_from_file"](str(self.csv.pk), "count").startswith("5 "))

    def test_to_number(self):
        self.assertEqual(to_number("$1,200.50"), 1200.5)
        self.assertEqual(to_number("45%"), 45.0)
        self.assertEqual(to_number("(300)"), -300.0)
        self.assertIsNone(to_number("n/a"))
