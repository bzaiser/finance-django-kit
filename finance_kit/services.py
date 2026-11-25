from datetime import date
from decimal import Decimal

from django.apps import apps
from django.db.models import Sum
from django.utils import timezone

from .models import BusinessDocument, DonationReceipt, LedgerEntry

LIMIT_3_26 = Decimal("3000.00")
LIMIT_3_26A = Decimal("840.00")


def get_sphere_summary(year: int | None = None) -> list[dict]:
    qs = LedgerEntry.objects.all()
    if year:
        qs = qs.filter(date__year=year)
    rows = []
    for key, label in LedgerEntry.SPHERE_CHOICES:
        sub = qs.filter(sphere=key)
        inc = sub.filter(entry_type="income").aggregate(s=Sum("amount"))["s"] or Decimal("0.00")
        exp = sub.filter(entry_type="expense").aggregate(s=Sum("amount"))["s"] or Decimal("0.00")
        rows.append({
            "key": key,
            "label": label,
            "income": inc,
            "expense": exp,
            "balance": inc - exp,
        })
    return rows


def get_allowance_monitor(year: int | None = None) -> list[dict]:
    """Sum § 3 Nr. 26 (3.000 €) and § 3 Nr. 26a (840 €) payouts per person & year."""
    target_year = year or timezone.localdate().year
    qs = (
        LedgerEntry.objects.filter(
            date__year=target_year,
            entry_type="expense",
            tax_exempt_paragraph__in=["3_26", "3_26a"],
        )
        .values("person_ref", "person_name", "tax_exempt_paragraph")
        .annotate(total=Sum("amount"))
        .order_by("-total")
    )
    rows = []
    for r in qs:
        limit = LIMIT_3_26 if r["tax_exempt_paragraph"] == "3_26" else LIMIT_3_26A
        total = r["total"] or Decimal("0.00")
        pct = min(999, int((total / limit) * 100)) if limit > 0 else 0
        rows.append({
            "person_ref": r["person_ref"],
            "person_name": r["person_name"] or r["person_ref"] or "Unbekannt",
            "paragraph": "§ 3 Nr. 26 EStG" if r["tax_exempt_paragraph"] == "3_26" else "§ 3 Nr. 26a EStG",
            "total": total,
            "limit": limit,
            "remaining": max(Decimal("0.00"), limit - total),
            "pct": pct,
            "status": "danger" if total >= limit else ("warning" if pct >= 80 else "ok"),
        })
    return rows


def next_doc_number(prefix: str, model_cls, field_name: str = "doc_number") -> str:
    year = timezone.localdate().year
    base = f"{prefix}-{year}-"
    existing = model_cls.objects.filter(**{f"{field_name}__startswith": base}).count() + 1
    cand = f"{base}{existing:04d}"
    while model_cls.objects.filter(**{field_name: cand}).exists():
        existing += 1
        cand = f"{base}{existing:04d}"
    return cand


def convert_business_doc(source: BusinessDocument, target_type: str) -> BusinessDocument:
    prefix_map = {"quote": "ANG", "order": "AB", "invoice": "RE", "credit_note": "GS"}
    prefix = prefix_map.get(target_type, "DOC")
    today = timezone.localdate()
    return BusinessDocument.objects.create(
        doc_type=target_type,
        doc_number=next_doc_number(prefix, BusinessDocument),
        status="draft",
        recipient_ref=source.recipient_ref,
        recipient_name=source.recipient_name,
        recipient_address=source.recipient_address,
        subject=source.subject,
        issue_date=today,
        due_date=date.fromordinal(today.toordinal() + 14) if target_type == "invoice" else None,
        lines_json=list(source.lines_json or []),
        total_net=source.total_net,
        tax_rate=source.tax_rate,
        total_gross=source.total_gross,
        notes=source.notes,
        source_doc=source,
    )


def _add_months(d: date, months: int) -> date:
    m = d.month - 1 + months
    y = d.year + m // 12
    m = m % 12 + 1
    day = min(d.day, [31, 29 if y % 4 == 0 and (y % 100 != 0 or y % 400 == 0) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1])
    return date(y, m, day)


def generate_due_recurring_invoices(as_of: date | None = None) -> list[BusinessDocument]:
    today = as_of or timezone.localdate()
    due_qs = BusinessDocument.objects.filter(
        is_recurring=True,
        next_run_date__lte=today,
    ).exclude(status="cancelled")
    created = []
    for tpl in due_qs:
        inv = convert_business_doc(tpl, "invoice")
        created.append(inv)
        tpl.next_run_date = _add_months(tpl.next_run_date, max(1, int(tpl.recurrence_months or 1)))
        tpl.save(update_fields=["next_run_date"])
    return created


def get_domain_options() -> dict:
    persons, accounts, inventories = [], [], []
    if apps.is_installed("domain_kit"):
        try:
            from domain_kit.models import AssetItem, Person
            for p in Person.objects.prefetch_related("addresses").all():
                addr = p.addresses.first()
                addr_str = f"{addr.street}, {addr.postal_code} {addr.city}".strip(", ") if addr else ""
                persons.append({"ref": f"person:{p.id}", "name": p.full_name, "address": addr_str})
            for a in AssetItem.objects.filter(asset_type="account"):
                accounts.append({"ref": f"account:{a.id}", "name": a.title})
            for a in AssetItem.objects.filter(asset_type="inventory"):
                inventories.append({"ref": f"inventory:{a.id}", "name": a.title, "cost": str(a.quantity)})
        except Exception:
            pass
    return {"persons": persons, "accounts": accounts, "inventories": inventories}
