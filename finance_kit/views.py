import csv
from decimal import Decimal, InvalidOperation

from django.apps import apps
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.utils import timezone
from django.views.decorators.http import require_POST

from .models import AssetDepreciation, AuditReport, BusinessDocument, DonationReceipt, FinanceSettings, LedgerEntry, MembershipFee
from .services import (
    cancel_ledger_entry,
    convert_business_doc,
    generate_due_recurring_invoices,
    generate_membership_fees,
    get_account_balances,
    get_allowance_monitor,
    get_audit_findings,
    get_domain_options,
    get_sphere_summary,
    import_bank_csv,
    next_doc_number,
    seed_finance_categories,
)


def _has_finance_access(user, write: bool = False, audit_write: bool = False) -> bool:
    if not user or not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    gnames = set(user.groups.values_list("name", flat=True))
    if gnames & {"Leitung / Vorstand", "Vorstand", "Buchhaltung / Kassierer"}:
        return True
    if "Kassenprüfer" in gnames:
        return True if (not write or audit_write) else False
    if apps.is_installed("domain_kit"):
        try:
            from domain_kit.services import get_module_access
            acc = get_module_access(user, "accounts")
            return acc == "write" if write else acc in ("read", "write")
        except Exception:
            pass
    return False


def _dec(raw, default=Decimal("0.00")) -> Decimal:
    try:
        return Decimal(str(raw or "").strip().replace(",", "."))
    except (InvalidOperation, ValueError):
        return default


def _render_pdf_or_html(template_name: str, ctx: dict, filename: str) -> HttpResponse:
    html = render_to_string(template_name, ctx)
    try:
        from io import BytesIO
        from xhtml2pdf import pisa
        buf = BytesIO()
        status = pisa.CreatePDF(html, dest=buf, encoding="utf-8")
        if not status.err:
            resp = HttpResponse(buf.getvalue(), content_type="application/pdf")
            resp["Content-Disposition"] = f'inline; filename="{filename}"'
            return resp
    except Exception:
        pass
    return HttpResponse(html)


@login_required
def finance_dashboard_view(request):
    if not _has_finance_access(request.user):
        return HttpResponseForbidden("Kein Zugriff auf Finanzen.")
    seed_finance_categories()
    year = timezone.localdate().year
    spheres = get_sphere_summary(year=year)
    total_income = sum((s["income"] for s in spheres), Decimal("0.00"))
    total_expense = sum((s["expense"] for s in spheres), Decimal("0.00"))
    open_invoices = BusinessDocument.objects.filter(doc_type="invoice", status__in=["draft", "sent"]).count()
    open_fees = MembershipFee.objects.filter(year=year, status="open").count()
    allowances = get_allowance_monitor(year=year)
    account_balances = get_account_balances(year=year)
    return render(request, "finance_kit/dashboard.html", {
        "year": year,
        "spheres": spheres,
        "total_income": total_income,
        "total_expense": total_expense,
        "net_balance": total_income - total_expense,
        "open_invoices": open_invoices,
        "open_fees": open_fees,
        "allowances": allowances,
        "account_balances": account_balances,
        "recent_entries": LedgerEntry.objects.all()[:6],
        "active_tab": "dashboard",
    })


@login_required
def annual_report_pdf_view(request):
    if not _has_finance_access(request.user):
        return HttpResponseForbidden("Kein Zugriff.")
    year = int(request.GET.get("year") or timezone.localdate().year)
    spheres = get_sphere_summary(year=year)
    total_income = sum((s["income"] for s in spheres), Decimal("0.00"))
    total_expense = sum((s["expense"] for s in spheres), Decimal("0.00"))
    return _render_pdf_or_html(
        "finance_kit/annual_report_pdf.html",
        {
            "year": year,
            "cfg": FinanceSettings.get_solo(),
            "spheres": spheres,
            "total_income": total_income,
            "total_expense": total_expense,
            "net_balance": total_income - total_expense,
            "account_balances": get_account_balances(year=year),
            "allowances": get_allowance_monitor(year=year),
            "donations_total": DonationReceipt.objects.filter(date_received__year=year).count(),
            "entries_count": LedgerEntry.objects.filter(date__year=year, is_cancelled=False).count(),
            "issued_date": timezone.localdate(),
        },
        f"jahresabschluss-{year}.pdf",
    )


@login_required
def ledger_view(request):
    if not _has_finance_access(request.user):
        return HttpResponseForbidden("Kein Zugriff auf Finanzen.")
    can_write = _has_finance_access(request.user, write=True)
    seed_finance_categories()
    opts = get_domain_options()
    person_map = {p["ref"]: p["name"] for p in opts["persons"]}
    cat_map = {str(c["id"]): c for c in opts["categories"]}

    if request.method == "POST":
        if not can_write:
            return HttpResponseForbidden("Schreibzugriff verweigert.")
        action = request.POST.get("action") or "create"
        if action == "storno":
            entry = get_object_or_404(LedgerEntry, pk=request.POST.get("pk"))
            cancel_ledger_entry(entry, user=request.user)
            messages.success(request, "Storno-Gegenbuchung erzeugt.")
            return redirect("finance_kit:ledger")
        if action == "delete":
            LedgerEntry.objects.filter(pk=request.POST.get("pk")).delete()
            messages.success(request, "Buchung gelöscht.")
            return redirect("finance_kit:ledger")

        pref = (request.POST.get("person_ref") or "").strip()
        cat_info = cat_map.get((request.POST.get("category_id") or "").strip(), {})
        sphere = cat_info.get("sphere") or (
            request.POST.get("sphere") if request.POST.get("sphere") in dict(LedgerEntry.SPHERE_CHOICES) else "ideell"
        )
        cat_name = (cat_info.get("name") or request.POST.get("category_name") or "").strip()[:80]
        rec_no = (request.POST.get("receipt_number") or "").strip()[:60] or next_doc_number("BEL", LedgerEntry, "receipt_number")
        entry = LedgerEntry.objects.create(
            date=request.POST.get("date") or timezone.localdate(),
            entry_type=request.POST.get("entry_type") if request.POST.get("entry_type") in ("income", "expense") else (cat_info.get("default_type") or "income"),
            sphere=sphere,
            amount=_dec(request.POST.get("amount")),
            tax_rate=_dec(request.POST.get("tax_rate")),
            description=(request.POST.get("description") or cat_name or "Buchung").strip()[:220],
            category_name=cat_name,
            receipt_number=rec_no,
            receipt_file=request.FILES.get("receipt_file"),
            account_ref=(request.POST.get("account_ref") or "").strip()[:64],
            person_ref=pref[:64],
            person_name=(request.POST.get("person_name") or person_map.get(pref, "")).strip()[:140],
            tax_exempt_paragraph=(request.POST.get("tax_exempt_paragraph") or "").strip()[:16],
            created_by=request.user,
        )
        if entry.receipt_file and apps.is_installed("domain_kit"):
            try:
                from domain_kit.models import Document
                Document.objects.create(
                    title=f"{entry.receipt_number}: {entry.description}"[:160],
                    topic="finance",
                    file=entry.receipt_file,
                    original_filename=getattr(entry.receipt_file, "name", "")[:180],
                    amount=entry.amount,
                    uploaded_by=request.user,
                )
            except Exception:
                pass
        messages.success(request, f"Buchung {entry.receipt_number} gespeichert.")
        return redirect("finance_kit:ledger")

    sphere_filter = (request.GET.get("sphere") or "").strip()
    entries = LedgerEntry.objects.all()
    if sphere_filter:
        entries = entries.filter(sphere=sphere_filter)
    return render(request, "finance_kit/ledger.html", {
        "entries": entries,
        "sphere_filter": sphere_filter,
        "sphere_choices": LedgerEntry.SPHERE_CHOICES,
        "tax_exempt_choices": LedgerEntry.TAX_EXEMPT_CHOICES,
        "domain_opts": opts,
        "can_write": can_write,
        "active_tab": "ledger",
    })


@login_required
def ledger_csv_view(request):
    if not _has_finance_access(request.user):
        return HttpResponseForbidden("Kein Zugriff.")
    resp = HttpResponse(content_type="text/csv; charset=utf-8")
    resp["Content-Disposition"] = 'attachment; filename="buchungsjournal.csv"'
    resp.write("\ufeff")
    w = csv.writer(resp, delimiter=";")
    w.writerow(["Datum", "Typ", "Sphäre", "Betrag (€)", "Beschreibung", "Person", "Beleg", "Pauschale"])
    for e in LedgerEntry.objects.all():
        w.writerow([
            e.date.isoformat(),
            e.get_entry_type_display(),
            e.get_sphere_display(),
            f"{e.amount:.2f}",
            e.description,
            e.person_name,
            e.receipt_number,
            e.get_tax_exempt_paragraph_display(),
        ])
    return resp


@login_required
def donations_view(request):
    if not _has_finance_access(request.user):
        return HttpResponseForbidden("Kein Zugriff.")
    can_write = _has_finance_access(request.user, write=True)
    opts = get_domain_options()
    person_map = {p["ref"]: p for p in opts["persons"]}

    if request.method == "POST":
        if not can_write:
            return HttpResponseForbidden("Schreibzugriff verweigert.")
        if request.POST.get("action") == "delete":
            DonationReceipt.objects.filter(pk=request.POST.get("pk")).delete()
            return redirect("finance_kit:donations")
        dref = (request.POST.get("donor_ref") or "").strip()
        pinfo = person_map.get(dref, {})
        amt = _dec(request.POST.get("amount"))
        d_date = request.POST.get("date_received") or timezone.localdate()
        dname = (request.POST.get("donor_name") or pinfo.get("name") or "Spender").strip()[:160]
        daddr = (request.POST.get("donor_address") or pinfo.get("address") or "").strip()
        entry = LedgerEntry.objects.create(
            date=d_date,
            entry_type="income",
            sphere="ideell",
            amount=amt,
            description=f"Spende: {dname}",
            person_ref=dref[:64],
            person_name=dname[:140],
            created_by=request.user,
        )
        DonationReceipt.objects.create(
            receipt_number=next_doc_number("SP", DonationReceipt, "receipt_number"),
            donor_ref=dref[:64],
            donor_name=dname,
            donor_address=daddr,
            amount=amt,
            donation_type=request.POST.get("donation_type") if request.POST.get("donation_type") in ("cash", "in_kind") else "cash",
            date_received=d_date,
            purpose_note=(request.POST.get("purpose_note") or "Förderung von Kunst und Kultur").strip()[:220],
            waiver_of_reimbursement=bool(request.POST.get("waiver_of_reimbursement")),
            ledger_entry=entry,
        )
        messages.success(request, "Zuwendungsbestätigung erstellt und im ideellen Bereich verbucht.")
        return redirect("finance_kit:donations")

    return render(request, "finance_kit/donations.html", {
        "receipts": DonationReceipt.objects.all(),
        "domain_opts": opts,
        "can_write": can_write,
        "active_tab": "donations",
    })


@login_required
def donation_pdf_view(request, pk):
    if not _has_finance_access(request.user):
        return HttpResponseForbidden("Kein Zugriff.")
    rec = get_object_or_404(DonationReceipt, pk=pk)
    cfg = FinanceSettings.get_solo()
    return _render_pdf_or_html(
        "finance_kit/donation_pdf.html",
        {"receipt": rec, "cfg": cfg},
        f"spendenquittung-{rec.receipt_number}.pdf",
    )


@login_required
def documents_view(request):
    if not _has_finance_access(request.user):
        return HttpResponseForbidden("Kein Zugriff.")
    can_write = _has_finance_access(request.user, write=True)
    opts = get_domain_options()
    person_map = {p["ref"]: p for p in opts["persons"]}

    if request.method == "POST":
        if not can_write:
            return HttpResponseForbidden("Schreibzugriff verweigert.")
        action = request.POST.get("action") or "create"
        if action == "delete":
            BusinessDocument.objects.filter(pk=request.POST.get("pk")).delete()
            return redirect("finance_kit:documents")
        if action == "mark_paid":
            doc = get_object_or_404(BusinessDocument, pk=request.POST.get("pk"))
            doc.status = "paid"
            doc.save(update_fields=["status"])
            messages.success(request, f"{doc.doc_number} als bezahlt markiert.")
            return redirect("finance_kit:documents")

        dtype = request.POST.get("doc_type") if request.POST.get("doc_type") in dict(BusinessDocument.DOC_TYPE_CHOICES) else "quote"
        prefix_map = {"quote": "ANG", "order": "AB", "invoice": "RE", "credit_note": "GS"}
        rref = (request.POST.get("recipient_ref") or "").strip()
        pinfo = person_map.get(rref, {})
        net = _dec(request.POST.get("total_net"))
        tax_r = _dec(request.POST.get("tax_rate"))
        gross = (net * (Decimal("1.00") + tax_r / Decimal("100.00"))).quantize(Decimal("0.01"))
        is_rec = bool(request.POST.get("is_recurring"))
        rec_months = max(1, int(request.POST.get("recurrence_months") or 12))
        issue_d = request.POST.get("issue_date") or timezone.localdate()
        BusinessDocument.objects.create(
            doc_type=dtype,
            doc_number=next_doc_number(prefix_map[dtype], BusinessDocument),
            status="draft",
            recipient_ref=rref[:64],
            recipient_name=(request.POST.get("recipient_name") or pinfo.get("name") or "Empfänger").strip()[:160],
            recipient_address=(request.POST.get("recipient_address") or pinfo.get("address") or "").strip(),
            subject=(request.POST.get("subject") or "Leistung").strip()[:200],
            issue_date=issue_d,
            due_date=request.POST.get("due_date") or None,
            is_recurring=is_rec,
            recurrence_months=rec_months,
            next_run_date=(request.POST.get("next_run_date") or issue_d) if is_rec else None,
            total_net=net,
            tax_rate=tax_r,
            total_gross=gross,
            notes=(request.POST.get("notes") or "").strip(),
        )
        messages.success(request, "Beleg angelegt.")
        return redirect("finance_kit:documents")

    return render(request, "finance_kit/documents.html", {
        "docs": BusinessDocument.objects.all(),
        "doc_type_choices": BusinessDocument.DOC_TYPE_CHOICES,
        "domain_opts": opts,
        "cfg": FinanceSettings.get_solo(),
        "can_write": can_write,
        "active_tab": "documents",
    })


@login_required
@require_POST
def document_convert_view(request, pk):
    if not _has_finance_access(request.user, write=True):
        return HttpResponseForbidden("Kein Zugriff.")
    src = get_object_or_404(BusinessDocument, pk=pk)
    target = request.POST.get("target_type") or "invoice"
    if target not in dict(BusinessDocument.DOC_TYPE_CHOICES):
        target = "invoice"
    new_doc = convert_business_doc(src, target)
    messages.success(request, f"{src.doc_number} → {new_doc.doc_number} ({new_doc.get_doc_type_display()}) erstellt.")
    return redirect("finance_kit:documents")


@login_required
@require_POST
def run_recurring_view(request):
    if not _has_finance_access(request.user, write=True):
        return HttpResponseForbidden("Kein Zugriff.")
    created = generate_due_recurring_invoices()
    messages.success(request, f"{len(created)} fällige Abo-Rechnung(en) erzeugt.")
    return redirect("finance_kit:documents")


@login_required
def document_pdf_view(request, pk):
    if not _has_finance_access(request.user):
        return HttpResponseForbidden("Kein Zugriff.")
    doc = get_object_or_404(BusinessDocument, pk=pk)
    cfg = FinanceSettings.get_solo()
    return _render_pdf_or_html(
        "finance_kit/document_pdf.html",
        {"doc": doc, "cfg": cfg},
        f"{doc.doc_number}.pdf",
    )


@login_required
def afa_view(request):
    if not _has_finance_access(request.user):
        return HttpResponseForbidden("Kein Zugriff.")
    can_write = _has_finance_access(request.user, write=True)
    opts = get_domain_options()
    inv_map = {i["ref"]: i for i in opts["inventories"]}

    if request.method == "POST":
        if not can_write:
            return HttpResponseForbidden("Schreibzugriff verweigert.")
        if request.POST.get("action") == "delete":
            AssetDepreciation.objects.filter(pk=request.POST.get("pk")).delete()
            return redirect("finance_kit:afa")
        aref = (request.POST.get("asset_ref") or "").strip()
        ainfo = inv_map.get(aref, {})
        AssetDepreciation.objects.create(
            asset_ref=aref[:64],
            asset_title=(request.POST.get("asset_title") or ainfo.get("name") or "Anlagegut").strip()[:160],
            acquisition_date=request.POST.get("acquisition_date") or timezone.localdate(),
            acquisition_cost=_dec(request.POST.get("acquisition_cost") or ainfo.get("cost")),
            useful_life_years=max(1, int(request.POST.get("useful_life_years") or 5)),
            residual_value=_dec(request.POST.get("residual_value"), Decimal("1.00")),
            sphere=request.POST.get("sphere") if request.POST.get("sphere") in dict(LedgerEntry.SPHERE_CHOICES) else "zweckbetrieb",
            notes=(request.POST.get("notes") or "").strip()[:220],
        )
        messages.success(request, "Anlagegut im AfA-Verzeichnis gespeichert.")
        return redirect("finance_kit:afa")

    return render(request, "finance_kit/afa.html", {
        "assets": AssetDepreciation.objects.all(),
        "sphere_choices": LedgerEntry.SPHERE_CHOICES,
        "domain_opts": opts,
        "current_year": timezone.localdate().year,
        "can_write": can_write,
        "active_tab": "afa",
    })


@login_required
@require_POST
def afa_book_view(request, pk):
    if not _has_finance_access(request.user, write=True):
        return HttpResponseForbidden("Kein Zugriff.")
    item = get_object_or_404(AssetDepreciation, pk=pk)
    year = timezone.localdate().year
    LedgerEntry.objects.create(
        date=timezone.localdate(),
        entry_type="expense",
        sphere=item.sphere,
        amount=item.annual_afa,
        description=f"AfA {year}: {item.asset_title}",
        category_name="Abschreibung (AfA)",
        created_by=request.user,
    )
    item.last_booked_year = year
    item.save(update_fields=["last_booked_year"])
    messages.success(request, f"AfA {year} ({item.annual_afa} €) für „{item.asset_title}“ verbucht.")
    return redirect("finance_kit:afa")


@login_required
def finance_settings_view(request):
    if not _has_finance_access(request.user, write=True):
        return HttpResponseForbidden("Kein Zugriff.")
    cfg = FinanceSettings.get_solo()
    if request.method == "POST":
        cfg.org_name = (request.POST.get("org_name") or cfg.org_name).strip()[:160]
        cfg.org_address = (request.POST.get("org_address") or "").strip()
        cfg.tax_number = (request.POST.get("tax_number") or "").strip()[:80]
        cfg.tax_office = (request.POST.get("tax_office") or "").strip()[:120]
        cfg.exemption_date = request.POST.get("exemption_date") or None
        cfg.small_business_rule = bool(request.POST.get("small_business_rule"))
        cfg.default_vat_rate = _dec(request.POST.get("default_vat_rate"), Decimal("19.00"))
        cfg.save()
        messages.success(request, "Finanz-Einstellungen gespeichert.")
        return redirect("finance_kit:settings")
    return render(request, "finance_kit/settings.html", {"cfg": cfg, "active_tab": "settings"})


@login_required
def fees_view(request):
    if not _has_finance_access(request.user):
        return HttpResponseForbidden("Kein Zugriff.")
    can_write = _has_finance_access(request.user, write=True)
    year = int(request.GET.get("year") or request.POST.get("year") or timezone.localdate().year)

    if request.method == "POST":
        if not can_write:
            return HttpResponseForbidden("Schreibzugriff verweigert.")
        action = request.POST.get("action") or ""
        if action == "generate":
            cnt = generate_membership_fees(year, _dec(request.POST.get("default_amount"), Decimal("60.00")))
            messages.success(request, f"{cnt} Beitragssatz/Beitragssätze für {year} erzeugt.")
        elif action == "mark_paid":
            fee = get_object_or_404(MembershipFee, pk=request.POST.get("pk"))
            if fee.status != "paid":
                today = timezone.localdate()
                entry = LedgerEntry.objects.create(
                    date=today,
                    entry_type="income",
                    sphere="ideell",
                    amount=fee.amount,
                    description=f"Mitgliedsbeitrag {fee.year}: {fee.person_name}",
                    category_name="Mitgliedsbeiträge",
                    receipt_number=next_doc_number("BEL", LedgerEntry, "receipt_number"),
                    account_ref=(request.POST.get("account_ref") or "").strip()[:64],
                    person_ref=fee.person_ref,
                    person_name=fee.person_name[:140],
                    source_ref=f"fee:{fee.year}:{fee.person_ref}",
                    created_by=request.user,
                )
                fee.status = "paid"
                fee.paid_date = today
                fee.ledger_entry = entry
                fee.save(update_fields=["status", "paid_date", "ledger_entry"])
                messages.success(request, f"Beitrag für {fee.person_name} als bezahlt verbucht ({entry.receipt_number}).")
        elif action == "send_reminders":
            sent = 0
            if apps.is_installed("domain_kit"):
                from django.conf import settings as dj_settings
                from django.core.mail import send_mail
                from domain_kit.models import Person
                cfg = FinanceSettings.get_solo()
                open_fees = MembershipFee.objects.filter(year=year, status="open")
                for f in open_fees:
                    if not f.person_ref.startswith("person:"):
                        continue
                    pid = int(f.person_ref.split(":", 1)[1])
                    p = Person.objects.filter(pk=pid).first()
                    if p and p.email and "@" in p.email:
                        try:
                            send_mail(
                                f"Erinnerung Mitgliedsbeitrag {year} ({cfg.org_name})",
                                f"Hallo {f.person_name},\n\nbitte überweisen Sie Ihren offenen Mitgliedsbeitrag für {year} in Höhe von {f.amount:.2f} €.\n\nVielen Dank!\n{cfg.org_name}",
                                getattr(dj_settings, "DEFAULT_FROM_EMAIL", "noreply@example.com"),
                                [p.email],
                                fail_silently=True,
                            )
                            sent += 1
                        except Exception:
                            pass
            messages.success(request, f"{sent} Zahlungserinnerung(en) versendet.")
        return redirect(f"{request.path}?year={year}")

    fees = MembershipFee.objects.filter(year=year)
    return render(request, "finance_kit/fees.html", {
        "year": year,
        "fees": fees,
        "open_count": fees.filter(status="open").count(),
        "paid_count": fees.filter(status="paid").count(),
        "domain_opts": get_domain_options(),
        "can_write": can_write,
        "active_tab": "fees",
    })


@login_required
@require_POST
def ledger_import_csv_view(request):
    if not _has_finance_access(request.user, write=True):
        return HttpResponseForbidden("Kein Zugriff.")
    f = request.FILES.get("csv_file")
    if not f:
        messages.error(request, "Bitte eine CSV-Datei auswählen.")
        return redirect("finance_kit:ledger")
    text = f.read().decode("utf-8-sig", errors="replace")
    created, skipped = import_bank_csv(text, account_ref=(request.POST.get("account_ref") or "").strip(), user=request.user)
    messages.success(request, f"Bank-CSV importiert: {created} neu, {skipped} übersprungen/Duplikate.")
    return redirect("finance_kit:ledger")


@login_required
def audit_view(request):
    if not _has_finance_access(request.user):
        return HttpResponseForbidden("Kein Zugriff.")
    can_write = _has_finance_access(request.user, write=True, audit_write=True)
    year = int(request.GET.get("year") or request.POST.get("year") or timezone.localdate().year)
    if request.method == "POST":
        if not can_write:
            return HttpResponseForbidden("Schreibzugriff verweigert.")
        AuditReport.objects.create(
            year=year,
            auditor_1=(request.POST.get("auditor_1") or "Kassenprüfer 1").strip()[:120],
            auditor_2=(request.POST.get("auditor_2") or "").strip()[:120],
            audit_date=request.POST.get("audit_date") or timezone.localdate(),
            result=request.POST.get("result") if request.POST.get("result") in ("ok", "notes") else "ok",
            findings=(request.POST.get("findings") or "").strip(),
        )
        messages.success(request, f"Kassenprüfungsbericht {year} gespeichert.")
        return redirect(f"{request.path}?year={year}")
    return render(request, "finance_kit/audit.html", {
        "year": year,
        "findings": get_audit_findings(year),
        "reports": AuditReport.objects.filter(year=year),
        "can_write": can_write,
        "active_tab": "audit",
    })


@login_required
def audit_report_pdf_view(request, pk):
    if not _has_finance_access(request.user):
        return HttpResponseForbidden("Kein Zugriff.")
    rep = get_object_or_404(AuditReport, pk=pk)
    return _render_pdf_or_html(
        "finance_kit/audit_report_pdf.html",
        {
            "report": rep,
            "cfg": FinanceSettings.get_solo(),
            "findings": get_audit_findings(rep.year),
            "spheres": get_sphere_summary(year=rep.year),
        },
        f"kassenpruefung-{rep.year}.pdf",
    )
