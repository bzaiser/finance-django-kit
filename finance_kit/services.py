from datetime import date
from decimal import Decimal, InvalidOperation

from django.apps import apps
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from .models import BusinessDocument, DonationReceipt, LedgerEntry, MembershipFee

LIMIT_3_26 = Decimal("3000.00")
LIMIT_3_26A = Decimal("840.00")

DEFAULT_FINANCE_CATEGORIES = [
    ("Mitgliedsbeiträge", "ideell", "income"),
    ("Spenden & Zuwendungen", "ideell", "income"),
    ("Zuschüsse & Fördermittel", "ideell", "income"),
    ("Kurs- & Teilnahmegebühren", "zweckbetrieb", "income"),
    ("Ausstellungs- & Eintrittsgelder", "zweckbetrieb", "income"),
    ("Dozenten-Honorar", "zweckbetrieb", "expense"),
    ("Katalog- & Werkverkauf", "wirtschaftlich", "income"),
    ("Bewirtung / Catering", "wirtschaftlich", "expense"),
    ("Raummiete & Nebenkosten", "ideell", "expense"),
    ("Material, Büro & Porto", "ideell", "expense"),
    ("Zinsen & Kapitalerträge", "vermoegen", "income"),
]


def seed_finance_categories() -> int:
    if not apps.is_installed("domain_kit"):
        return 0
    try:
        from domain_kit.models import Category
        if Category.objects.filter(purpose="account").exists():
            return 0
        created = 0
        for idx, (name, sphere_code, default_type) in enumerate(DEFAULT_FINANCE_CATEGORIES, start=1):
            _, was_created = Category.objects.get_or_create(
                name=name,
                purpose="account",
                defaults={"code": sphere_code, "value": default_type, "position": idx * 10},
            )
            if was_created:
                created += 1
        return created
    except Exception:
        return 0


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
            is_cancelled=False,
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


def cancel_ledger_entry(entry: LedgerEntry, user=None) -> LedgerEntry:
    """Create a GoBD-compliant counter-booking (Storno) and mark the original cancelled."""
    if entry.is_cancelled:
        return entry
    with transaction.atomic():
        entry.is_cancelled = True
        entry.save(update_fields=["is_cancelled"])
        storno = LedgerEntry.objects.create(
            date=timezone.localdate(),
            entry_type=entry.entry_type,
            sphere=entry.sphere,
            amount=-abs(entry.amount) if entry.amount > 0 else abs(entry.amount),
            tax_rate=entry.tax_rate,
            description=f"Storno zu {entry.receipt_number or entry.id}: {entry.description}"[:220],
            category_name=entry.category_name,
            receipt_number=next_doc_number("ST", LedgerEntry, "receipt_number"),
            account_ref=entry.account_ref,
            person_ref=entry.person_ref,
            person_name=entry.person_name,
            tax_exempt_paragraph=entry.tax_exempt_paragraph,
            is_cancelled=True,
            storno_of=entry,
            created_by=user,
        )
    return storno


def get_account_balances(year: int | None = None) -> list[dict]:
    if not apps.is_installed("domain_kit"):
        return []
    try:
        from domain_kit.models import AssetItem
        rows = []
        for acc in AssetItem.objects.filter(asset_type="account").order_by("title"):
            ref = f"account:{acc.id}"
            qs = LedgerEntry.objects.filter(account_ref=ref)
            if year:
                qs = qs.filter(date__year=year)
            inc = qs.filter(entry_type="income").aggregate(s=Sum("amount"))["s"] or Decimal("0.00")
            exp = qs.filter(entry_type="expense").aggregate(s=Sum("amount"))["s"] or Decimal("0.00")
            opening = acc.quantity or Decimal("0.00")
            rows.append({
                "ref": ref,
                "name": acc.title,
                "identifier": acc.identifier,
                "opening": opening,
                "income": inc,
                "expense": exp,
                "balance": opening + inc - exp,
            })
        return rows
    except Exception:
        return []


def generate_membership_fees(year: int, default_amount: Decimal = Decimal("60.00")) -> int:
    if not apps.is_installed("domain_kit"):
        return 0
    from domain_kit.models import Person
    created = 0
    for p in Person.objects.select_related("category").all():
        fee_amount = default_amount
        tariff = p.category.name if p.category else "Standard"
        if p.category and p.category.value:
            try:
                fee_amount = Decimal(str(p.category.value).strip().replace(",", "."))
            except (InvalidOperation, ValueError):
                pass
        _, was_created = MembershipFee.objects.get_or_create(
            year=year,
            person_ref=f"person:{p.id}",
            defaults={
                "person_name": p.full_name,
                "tariff_label": tariff,
                "amount": fee_amount,
            },
        )
        if was_created:
            created += 1
    return created


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
    persons, accounts, inventories, categories = [], [], [], []
    if apps.is_installed("domain_kit"):
        try:
            from domain_kit.models import AssetItem, Category, Person
            for p in Person.objects.select_related("category").prefetch_related("addresses").all():
                addr = p.addresses.first()
                addr_str = f"{addr.street}, {addr.postal_code} {addr.city}".strip(", ") if addr else ""
                persons.append({"ref": f"person:{p.id}", "name": p.full_name, "address": addr_str})
            for a in AssetItem.objects.filter(asset_type="account"):
                accounts.append({"ref": f"account:{a.id}", "name": a.title})
            for a in AssetItem.objects.filter(asset_type="inventory"):
                inventories.append({"ref": f"inventory:{a.id}", "name": a.title, "cost": str(a.quantity)})
            for c in Category.for_purpose("account", "general"):
                categories.append({
                    "id": c.id,
                    "name": c.name,
                    "sphere": c.code if c.code in dict(LedgerEntry.SPHERE_CHOICES) else "ideell",
                    "default_type": c.value if c.value in ("income", "expense") else "",
                })
        except Exception:
            pass
    return {"persons": persons, "accounts": accounts, "inventories": inventories, "categories": categories}
