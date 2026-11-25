import os
import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "tests.settings")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import django
from django.conf import settings

if not settings.configured:
    settings.configure(
        DEBUG=True,
        SECRET_KEY="test-finance-kit",
        INSTALLED_APPS=[
            "django.contrib.admin",
            "django.contrib.auth",
            "django.contrib.contenttypes",
            "django.contrib.sessions",
            "django.contrib.messages",
            "django.contrib.staticfiles",
            "design_studio",
            "finance_kit",
        ],
        MIDDLEWARE=[
            "django.middleware.security.SecurityMiddleware",
            "django.contrib.sessions.middleware.SessionMiddleware",
            "django.middleware.common.CommonMiddleware",
            "django.middleware.csrf.CsrfViewMiddleware",
            "django.contrib.auth.middleware.AuthenticationMiddleware",
            "django.contrib.messages.middleware.MessageMiddleware",
        ],
        ROOT_URLCONF="tests.urls",
        DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}},
        TEMPLATES=[{
            "BACKEND": "django.template.backends.django.DjangoTemplates",
            "APP_DIRS": True,
            "OPTIONS": {
                "context_processors": [
                    "django.template.context_processors.request",
                    "django.contrib.auth.context_processors.auth",
                    "django.contrib.messages.context_processors.messages",
                    "design_studio.context_processors.theme_context",
                ]
            },
        }],
        STATIC_URL="/static/",
        LANGUAGE_CODE="de",
        TIME_ZONE="Europe/Berlin",
        USE_TZ=True,
    )
    django.setup()

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.urls import include, path

urlpatterns = [
    path("design-studio/", include("design_studio.urls")),
    path("finance/", include("finance_kit.urls")),
]
sys.modules["tests.urls"] = sys.modules[__name__]


class FinanceKitTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.admin = User.objects.create_user(username="finadmin", password="pw", is_superuser=True, is_staff=True)
        self.client.force_login(self.admin)

    def test_role_isolation_for_auditor_and_non_finance_staff(self):
        from django.contrib.auth.models import Group
        User = get_user_model()
        staff_no_fin = User.objects.create_user(username="staff_other", password="pw", is_staff=True)
        self.client.force_login(staff_no_fin)
        self.assertEqual(self.client.get("/finance/").status_code, 403)

        auditor = User.objects.create_user(username="auditor", password="pw", is_staff=False)
        aud_grp, _ = Group.objects.get_or_create(name="Kassenprüfer")
        auditor.groups.add(aud_grp)
        self.client.force_login(auditor)
        self.assertEqual(self.client.get("/finance/").status_code, 200)
        self.assertEqual(self.client.post("/finance/ledger/", {"amount": "10.00", "description": "Verboten"}).status_code, 403)
        self.assertEqual(self.client.post("/finance/audit/", {"auditor_1": "Prüfer", "result": "ok"}).status_code, 302)

    def test_spheres_allowance_monitor_and_donations(self):
        self.client.post("/finance/ledger/", {
            "entry_type": "expense",
            "sphere": "zweckbetrieb",
            "amount": "2500.00",
            "description": "Honorar Kursleitung",
            "person_name": "Anna Dozentin",
            "tax_exempt_paragraph": "3_26",
        })
        dash = self.client.get("/finance/")
        self.assertEqual(dash.status_code, 200)
        self.assertContains(dash, "Anna Dozentin")
        self.assertContains(dash, "2500")

        self.client.post("/finance/donations/", {
            "donor_name": "Max Spender",
            "amount": "150.00",
            "donation_type": "cash",
        })
        from finance_kit.models import DonationReceipt
        rec = DonationReceipt.objects.get(donor_name="Max Spender")
        pdf = self.client.get(f"/finance/donations/{rec.id}/pdf/")
        self.assertEqual(pdf.status_code, 200)

    def test_quote_convert_recurring_and_afa(self):
        from finance_kit.models import AssetDepreciation, BusinessDocument
        self.client.post("/finance/documents/", {
            "doc_type": "quote",
            "recipient_name": "Firma ABC",
            "subject": "Ausstellungssponsoring",
            "total_net": "500.00",
            "tax_rate": "19.00",
            "is_recurring": "on",
            "recurrence_months": "12",
            "next_run_date": date.today().isoformat(),
        })
        quote = BusinessDocument.objects.get(doc_type="quote")
        self.client.post(f"/finance/documents/{quote.id}/convert/", {"target_type": "invoice"})
        self.assertTrue(BusinessDocument.objects.filter(doc_type="invoice", source_doc=quote).exists())

        self.client.post("/finance/documents/run-recurring/")
        self.assertGreaterEqual(BusinessDocument.objects.filter(doc_type="invoice").count(), 2)

        self.client.post("/finance/afa/", {
            "asset_title": "Beamer",
            "acquisition_cost": "1001.00",
            "useful_life_years": "5",
        })
        afa = AssetDepreciation.objects.get(asset_title="Beamer")
        self.assertEqual(afa.annual_afa, Decimal("200.00"))
        self.client.post(f"/finance/afa/{afa.id}/book/")
        afa.refresh_from_db()
        self.assertEqual(afa.last_booked_year, date.today().year)

    def test_storno_membership_fee_and_annual_report_pdf(self):
        from finance_kit.models import LedgerEntry, MembershipFee
        self.client.post("/finance/ledger/", {
            "entry_type": "income",
            "sphere": "ideell",
            "amount": "100.00",
            "description": "Testbuchung",
        })
        entry = LedgerEntry.objects.get(description="Testbuchung")
        self.assertTrue(entry.receipt_number.startswith("BEL-"))
        self.client.post("/finance/ledger/", {"action": "storno", "pk": str(entry.id)})
        entry.refresh_from_db()
        self.assertTrue(entry.is_cancelled)
        self.assertTrue(LedgerEntry.objects.filter(storno_of=entry, amount=Decimal("-100.00")).exists())

        fee = MembershipFee.objects.create(year=date.today().year, person_ref="person:1", person_name="Erika Mitglied", amount=Decimal("60.00"))
        self.client.post("/finance/fees/", {"action": "mark_paid", "year": str(date.today().year), "pk": str(fee.id)})
        fee.refresh_from_db()
        self.assertEqual(fee.status, "paid")
        self.assertIsNotNone(fee.ledger_entry)

        rep = self.client.get(f"/finance/report/pdf/?year={date.today().year}")
        self.assertEqual(rep.status_code, 200)

    def test_bank_csv_import_and_audit_report(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        from finance_kit.models import AuditReport, LedgerEntry
        csv_bytes = "Datum;Verwendungszweck;Betrag\n15.11.2026;Beitrag Huber;60,00\n16.11.2026;Miete Atelier;-450,00\n".encode("utf-8")
        f = SimpleUploadedFile("bank.csv", csv_bytes, content_type="text/csv")
        self.client.post("/finance/ledger/import-csv/", {"csv_file": f})
        self.assertEqual(LedgerEntry.objects.filter(source_ref__startswith="bankcsv:").count(), 2)

        f2 = SimpleUploadedFile("bank.csv", csv_bytes, content_type="text/csv")
        self.client.post("/finance/ledger/import-csv/", {"csv_file": f2})
        self.assertEqual(LedgerEntry.objects.filter(source_ref__startswith="bankcsv:").count(), 2)

        self.client.post("/finance/audit/", {
            "year": str(date.today().year),
            "auditor_1": "Max Prüfer",
            "auditor_2": "Erika Prüferin",
            "result": "ok",
            "findings": "Alles sauber.",
        })
        ar = AuditReport.objects.get(auditor_1="Max Prüfer")
        pdf = self.client.get(f"/finance/audit/{ar.id}/pdf/")
        self.assertEqual(pdf.status_code, 200)


if __name__ == "__main__":
    from django.test.runner import DiscoverRunner
    call_command("migrate", verbosity=0)
    runner = DiscoverRunner(verbosity=2)
    sys.exit(bool(runner.run_tests(["tests.test_finance_kit"])))
