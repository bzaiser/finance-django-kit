from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils import timezone


class FinanceSettings(models.Model):
    org_name = models.CharField(max_length=160, default="Kunstverein")
    org_address = models.TextField(blank=True, default="")
    tax_number = models.CharField(max_length=80, blank=True, default="")
    tax_office = models.CharField(max_length=120, blank=True, default="")
    exemption_date = models.DateField(null=True, blank=True)
    small_business_rule = models.BooleanField(default=True)
    default_vat_rate = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("19.00"))
    updated_at = models.DateTimeField(auto_now=True)

    @classmethod
    def get_solo(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def __str__(self):
        return self.org_name


class LedgerEntry(models.Model):
    ENTRY_TYPE_CHOICES = [
        ("income", "Einnahme"),
        ("expense", "Ausgabe"),
    ]
    SPHERE_CHOICES = [
        ("ideell", "Ideeller Bereich"),
        ("vermoegen", "Vermögensverwaltung"),
        ("zweckbetrieb", "Zweckbetrieb"),
        ("wirtschaftlich", "Wirtschaftlicher Geschäftsbetrieb"),
    ]
    TAX_EXEMPT_CHOICES = [
        ("", "Standard / Keine Pauschale"),
        ("3_26", "§ 3 Nr. 26 EStG (Übungsleiterpauschale 3.000 €/J.)"),
        ("3_26a", "§ 3 Nr. 26a EStG (Ehrenamtspauschale 840 €/J.)"),
    ]

    date = models.DateField(default=timezone.localdate, db_index=True)
    entry_type = models.CharField(max_length=16, choices=ENTRY_TYPE_CHOICES, default="income", db_index=True)
    sphere = models.CharField(max_length=24, choices=SPHERE_CHOICES, default="ideell", db_index=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    tax_rate = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0.00"))
    description = models.CharField(max_length=220)
    category_name = models.CharField(max_length=80, blank=True, default="")
    receipt_number = models.CharField(max_length=60, blank=True, default="")
    receipt_file = models.FileField(upload_to="finance_kit/receipts/%Y/%m/", blank=True, null=True)
    account_ref = models.CharField(max_length=64, blank=True, default="", db_index=True)
    person_ref = models.CharField(max_length=64, blank=True, default="", db_index=True)
    person_name = models.CharField(max_length=140, blank=True, default="")
    tax_exempt_paragraph = models.CharField(max_length=16, choices=TAX_EXEMPT_CHOICES, blank=True, default="")
    is_cancelled = models.BooleanField(default=False, db_index=True)
    storno_of = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="storno_entries",
    )
    source_ref = models.CharField(max_length=80, blank=True, default="", db_index=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="finance_kit_entries",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date", "-id"]

    def __str__(self):
        return f"{self.date} · {self.description} ({self.amount} €)"


class DonationReceipt(models.Model):
    DONATION_TYPE_CHOICES = [
        ("cash", "Geldzuwendung"),
        ("in_kind", "Sachzuwendung"),
    ]

    receipt_number = models.CharField(max_length=40, unique=True)
    donor_ref = models.CharField(max_length=64, blank=True, default="", db_index=True)
    donor_name = models.CharField(max_length=160)
    donor_address = models.TextField(blank=True, default="")
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    donation_type = models.CharField(max_length=16, choices=DONATION_TYPE_CHOICES, default="cash")
    date_received = models.DateField(default=timezone.localdate)
    purpose_note = models.CharField(max_length=220, blank=True, default="Förderung von Kunst und Kultur")
    waiver_of_reimbursement = models.BooleanField(default=False)
    ledger_entry = models.ForeignKey(
        LedgerEntry,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="donation_receipts",
    )
    issued_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date_received", "-id"]

    def __str__(self):
        return f"{self.receipt_number} · {self.donor_name} ({self.amount} €)"


class BusinessDocument(models.Model):
    DOC_TYPE_CHOICES = [
        ("quote", "Angebot"),
        ("order", "Auftragsbestätigung"),
        ("invoice", "Rechnung"),
        ("credit_note", "Gutschrift"),
    ]
    STATUS_CHOICES = [
        ("draft", "Entwurf"),
        ("sent", "Versendet"),
        ("paid", "Bezahlt"),
        ("cancelled", "Storniert"),
    ]

    doc_type = models.CharField(max_length=20, choices=DOC_TYPE_CHOICES, default="quote", db_index=True)
    doc_number = models.CharField(max_length=40, unique=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="draft", db_index=True)
    recipient_ref = models.CharField(max_length=64, blank=True, default="", db_index=True)
    recipient_name = models.CharField(max_length=160)
    recipient_address = models.TextField(blank=True, default="")
    subject = models.CharField(max_length=200)
    issue_date = models.DateField(default=timezone.localdate)
    due_date = models.DateField(null=True, blank=True)
    is_recurring = models.BooleanField(default=False, db_index=True)
    recurrence_months = models.PositiveSmallIntegerField(default=12)
    next_run_date = models.DateField(null=True, blank=True)
    lines_json = models.JSONField(default=list, blank=True)
    total_net = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    tax_rate = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0.00"))
    total_gross = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    notes = models.TextField(blank=True, default="")
    source_doc = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="derived_docs",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-issue_date", "-id"]

    def __str__(self):
        return f"{self.doc_number} ({self.get_doc_type_display()})"


class AssetDepreciation(models.Model):
    asset_ref = models.CharField(max_length=64, blank=True, default="", db_index=True)
    asset_title = models.CharField(max_length=160)
    acquisition_date = models.DateField(default=timezone.localdate)
    acquisition_cost = models.DecimalField(max_digits=12, decimal_places=2)
    useful_life_years = models.PositiveSmallIntegerField(default=5)
    residual_value = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("1.00"))
    sphere = models.CharField(max_length=24, choices=LedgerEntry.SPHERE_CHOICES, default="zweckbetrieb")
    last_booked_year = models.PositiveIntegerField(null=True, blank=True)
    notes = models.CharField(max_length=220, blank=True, default="")

    class Meta:
        ordering = ["-acquisition_date", "asset_title"]

    @property
    def annual_afa(self) -> Decimal:
        years = max(1, int(self.useful_life_years or 1))
        base = max(Decimal("0.00"), (self.acquisition_cost or Decimal("0.00")) - (self.residual_value or Decimal("0.00")))
        return (base / Decimal(years)).quantize(Decimal("0.01"))

    def __str__(self):
        return f"{self.asset_title} ({self.annual_afa} €/J.)"


class MembershipFee(models.Model):
    STATUS_CHOICES = [
        ("open", "Offen"),
        ("paid", "Bezahlt"),
    ]

    year = models.PositiveIntegerField(db_index=True)
    person_ref = models.CharField(max_length=64, db_index=True)
    person_name = models.CharField(max_length=160)
    tariff_label = models.CharField(max_length=80, blank=True, default="")
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default="open", db_index=True)
    paid_date = models.DateField(null=True, blank=True)
    ledger_entry = models.ForeignKey(
        LedgerEntry,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="membership_fees",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["person_name", "id"]
        unique_together = [("year", "person_ref")]

    def __str__(self):
        return f"{self.year} · {self.person_name} ({self.amount} €)"


class AuditReport(models.Model):
    RESULT_CHOICES = [
        ("ok", "Kasse geprüft und für richtig befunden"),
        ("notes", "Mit Beanstandungen / Hinweisen"),
    ]

    year = models.PositiveIntegerField(db_index=True)
    auditor_1 = models.CharField(max_length=120)
    auditor_2 = models.CharField(max_length=120, blank=True, default="")
    audit_date = models.DateField(default=timezone.localdate)
    result = models.CharField(max_length=16, choices=RESULT_CHOICES, default="ok")
    findings = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-year", "-audit_date", "-id"]

    def __str__(self):
        return f"Kassenprüfung {self.year} ({self.audit_date:%d.%m.%Y})"
