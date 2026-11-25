from django.urls import path

from . import views

app_name = "finance_kit"

urlpatterns = [
    path("", views.finance_dashboard_view, name="dashboard"),
    path("report/pdf/", views.annual_report_pdf_view, name="annual_report_pdf"),
    path("ledger/", views.ledger_view, name="ledger"),
    path("ledger/csv/", views.ledger_csv_view, name="ledger_csv"),
    path("ledger/import-csv/", views.ledger_import_csv_view, name="ledger_import_csv"),
    path("fees/", views.fees_view, name="fees"),
    path("audit/", views.audit_view, name="audit"),
    path("audit/<int:pk>/pdf/", views.audit_report_pdf_view, name="audit_report_pdf"),
    path("donations/", views.donations_view, name="donations"),
    path("donations/<int:pk>/pdf/", views.donation_pdf_view, name="donation_pdf"),
    path("documents/", views.documents_view, name="documents"),
    path("documents/<int:pk>/convert/", views.document_convert_view, name="document_convert"),
    path("documents/<int:pk>/pdf/", views.document_pdf_view, name="document_pdf"),
    path("documents/run-recurring/", views.run_recurring_view, name="run_recurring"),
    path("afa/", views.afa_view, name="afa"),
    path("afa/<int:pk>/book/", views.afa_book_view, name="afa_book"),
    path("settings/", views.finance_settings_view, name="settings"),
]
