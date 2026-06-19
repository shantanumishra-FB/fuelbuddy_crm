# Copyright (c) 2026, Fuelbuddy and contributors
# For license information, please see license.txt

import frappe
from frappe.model.workflow import apply_workflow
from frappe.utils import add_months, flt, get_first_day, get_last_day, getdate, nowdate

# Sales Order workflow: "Approved" is the only docstatus=1 state, reached via the
# "Approve" transition. Contract SOs are created in "Pending" then approved+submitted.
SO_APPROVE_ACTION = "Approve"
CONTRACT_SO_QUEUE = "long"

# Each subsequent month's order quantity grows from the previous month's actual
# deliveries: next_qty = delivered_prev_month + delivered_prev_month * GROWTH = delivered * 1.25.
CONTRACT_QTY_GROWTH = 0.25


def on_quotation_submit(doc, method=None):
	"""Quotation submit / update-after-submit hook -> start contract SO automation
	for the current month if the Opportunity is ready (Quotation + FD submitted)."""
	if getattr(doc, "custom_opportunity_from", None):
		create_sales_order_if_ready(doc.custom_opportunity_from)


@frappe.whitelist()
def create_sales_order_if_ready(opportunity):
	"""If the Opportunity's Quotation AND Finance Dossier are both submitted, kick
	off the current month's contract Sales Order (in the background). Idempotent."""
	if not opportunity or not frappe.db.exists("Opportunity", opportunity):
		return None

	source = ready_contract_quotation(opportunity)
	if not source:
		return None

	# Run SO creation/submission in the background (as Administrator) so the
	# workflow "Approve" transition is permitted regardless of who submitted.
	frappe.enqueue(
		"fuelbuddy_crm.sales_automation.create_contract_month_so",
		queue=CONTRACT_SO_QUEUE,
		enqueue_after_commit=True,
		quotation=source,
		target_date=nowdate(),
		set_stage=True,
	)
	return source


def ready_contract_quotation(opportunity):
	"""Return the single source Quotation name when the Opportunity is ready:
	at least one submitted Quotation and at least one submitted Finance Dossier,
	with none of them still in draft. Otherwise None."""
	quotations = frappe.get_all(
		"Quotation",
		filters={"custom_opportunity_from": opportunity, "docstatus": ["!=", 2]},
		fields=["name", "docstatus"],
	)
	dossiers = frappe.get_all(
		"Finance Dossier",
		filters={"finance_dossier_from": "Opportunity", "id": opportunity, "docstatus": ["!=", 2]},
		fields=["name", "docstatus"],
	)

	if not quotations or not dossiers:
		return None
	if any(q.docstatus != 1 for q in quotations):
		return None
	if any(d.docstatus != 1 for d in dossiers):
		return None

	# The latest submitted Quotation is the single contract source for the SOs.
	return frappe.get_all(
		"Quotation",
		filters={"name": ["in", [q.name for q in quotations]]},
		order_by="creation desc",
		limit=1,
		pluck="name",
	)[0]


@frappe.whitelist()
def create_contract_month_so(quotation, target_date=None, set_stage=False):
	"""Create and submit one Sales Order for the month of `target_date` from a
	contract Quotation. delivery_date = last day of that month. Idempotent per
	(quotation, month); stops once the contract has expired."""
	# Background jobs run as the enqueuing user; the SO workflow "Approve"
	# transition needs System Manager, so act as Administrator.
	if frappe.session.user != "Administrator":
		frappe.set_user("Administrator")

	target_date = getdate(target_date or nowdate())
	month_start = get_first_day(target_date)
	month_end = get_last_day(target_date)

	qdoc = frappe.get_doc("Quotation", quotation)
	if qdoc.docstatus != 1:
		return None

	# Stop once the contract has expired (no SO for months past expiry).
	expiry = qdoc.get("custom_contract_expiry")
	if expiry and getdate(expiry) < month_start:
		return None

	# Idempotent: one Sales Order per (quotation, month).
	existing = frappe.get_all(
		"Sales Order",
		filters={
			"custom_quotation": quotation,
			"docstatus": ["!=", 2],
			"delivery_date": ["between", [month_start, month_end]],
		},
		limit=1,
		pluck="name",
	)
	if existing:
		return existing[0]

	opportunity = qdoc.custom_opportunity_from

	# The first month's SO is mapped from the Quotation. Subsequent months are
	# cloned from that first SO, because make_sales_order maps only un-ordered
	# qty and returns no items once the Quotation is fully "Ordered".
	template = frappe.get_all(
		"Sales Order",
		filters={"custom_quotation": quotation, "docstatus": ["!=", 2]},
		order_by="creation asc",
		limit=1,
		pluck="name",
	)

	if template:
		# Subsequent months: order qty is driven by the PREVIOUS month's actual
		# deliveries +25% (next_qty = delivered_prev_month * (1 + CONTRACT_QTY_GROWTH)).
		# If the previous month had no SO or zero deliveries, skip this month entirely.
		prev_so = _previous_month_so(quotation, month_start)
		if not prev_so:
			return None
		delivered = _delivered_qty_by_item(prev_so)
		if not any(qty > 0 for qty in delivered.values()):
			return None

		so = frappe.copy_doc(frappe.get_doc("Sales Order", template[0]))
		kept = []
		for row in so.items:
			# each monthly SO is a standalone order; don't re-consume the Quotation's
			# ordered qty (keep only the first SO linked back to the Quotation).
			row.prevdoc_docname = None
			row.prevdoc_doctype = None
			row.quotation_item = None
			new_qty = flt(delivered.get(row.item_code, 0)) * (1 + CONTRACT_QTY_GROWTH)
			if new_qty > 0:
				row.qty = new_qty
				kept.append(row)
		so.items = kept
		for idx, row in enumerate(so.items, start=1):
			row.idx = idx
	else:
		# Sales Order needs a Customer. For a Lead-based Quotation, auto-create the
		# Customer (copying VAT / Trade Licence from the Finance Dossier).
		if qdoc.quotation_to == "Lead":
			txn_ig = _transaction_item_group(opportunity, quotation)
			ensure_customer_from_lead(qdoc.party_name, txn_ig, opportunity)

		from erpnext.selling.doctype.quotation.quotation import make_sales_order

		so = make_sales_order(quotation)

	so.custom_quotation = quotation
	so.transaction_date = nowdate()
	so.delivery_date = month_end

	# fill custom mandatory fields the standard mapper does not set
	so.custom_deal_type = qdoc.custom_deal_type or frappe.db.get_value(
		"Opportunity", opportunity, "custom_deal_type"
	)
	if not so.get("custom_transaction_type"):
		txn = frappe.db.get_value("Customer", so.customer, "custom_transaction_type")
		if not txn and so.items:
			txn = frappe.db.get_value("Item", so.items[0].item_code, "item_group")
		so.custom_transaction_type = txn
	if not so.get("custom_source_of_creation"):
		so.custom_source_of_creation = "ERP"
	if not so.get("custom_contract_type"):
		so.custom_contract_type = "FLEET"

	for row in so.items:
		row.delivery_date = month_end

	# Create in the workflow's draft state, then approve -> submitted (docstatus 1).
	so.workflow_state = "Pending"
	so.insert(ignore_permissions=True)
	apply_workflow(so, SO_APPROVE_ACTION)

	if set_stage and opportunity:
		frappe.db.set_value("Opportunity", opportunity, "sales_stage", "Sales Order Created")
	return so.name


def generate_monthly_contract_sales_orders():
	"""Monthly scheduler: for every active contract Quotation (submitted, FD ready,
	contract not yet expired), create this month's Sales Order. Idempotent."""
	today = getdate(nowdate())
	month_start = get_first_day(today)

	quotations = frappe.get_all(
		"Quotation",
		filters={"docstatus": 1, "custom_contract_expiry": [">=", month_start]},
		pluck="name",
	)
	for q in quotations:
		opportunity = frappe.db.get_value("Quotation", q, "custom_opportunity_from")
		if not opportunity:
			continue
		# Only the chosen source Quotation of a ready Opportunity drives generation.
		if ready_contract_quotation(opportunity) != q:
			continue
		try:
			create_contract_month_so(q, today, set_stage=False)
			frappe.db.commit()
		except Exception:
			frappe.db.rollback()
			frappe.log_error(title=f"Contract SO generation failed: {q}")


def _previous_month_so(quotation, month_start):
	"""Return this contract's submitted Sales Order for the month immediately before
	`month_start` (delivery_date in that month), or None if there isn't one."""
	prev = getdate(add_months(month_start, -1))
	rows = frappe.get_all(
		"Sales Order",
		filters={
			"custom_quotation": quotation,
			"docstatus": 1,
			"delivery_date": ["between", [get_first_day(prev), get_last_day(prev)]],
		},
		order_by="delivery_date desc",
		limit=1,
		pluck="name",
	)
	return rows[0] if rows else None


def _delivered_qty_by_item(so_name):
	"""Total delivered quantity per item_code on a Sales Order (net of returns, since
	ERPNext reduces delivered_qty for sales returns). {item_code: delivered_qty}."""
	out = {}
	for it in frappe.get_all(
		"Sales Order Item", filters={"parent": so_name}, fields=["item_code", "delivered_qty"]
	):
		out[it.item_code] = out.get(it.item_code, 0) + flt(it.delivered_qty)
	return out


def _transaction_item_group(opportunity, quotation):
	"""Resolve the transaction Item Group from the Opportunity product or the
	first quotation item (used to seed Customer.custom_transaction_type)."""
	txn_ig = None
	if opportunity:
		opp_product = frappe.db.get_value("Opportunity", opportunity, "custom_product")
		if opp_product:
			txn_ig = frappe.db.get_value("Item", opp_product, "item_group")
	if not txn_ig:
		first_item = frappe.get_all("Quotation Item", {"parent": quotation}, ["item_code"], limit=1)
		if first_item:
			txn_ig = frappe.db.get_value("Item", first_item[0].item_code, "item_group")
	return txn_ig


def ensure_customer_from_lead(lead_name, transaction_item_group=None, opportunity=None):
	"""Create a Customer for a Lead (if none exists) so a Sales Order can be raised.
	make_sales_order resolves the customer from a Quotation via Customer.lead_name.
	Copies VAT / Trade Licence from the Opportunity's Finance Dossier when available."""
	if not lead_name or not frappe.db.exists("Lead", lead_name):
		return None
	existing = frappe.db.get_value("Customer", {"lead_name": lead_name}, "name")
	if existing:
		return existing
	lead = frappe.get_doc("Lead", lead_name)
	cust = frappe.new_doc("Customer")
	cust.customer_name = lead.company_name or lead.lead_name or lead_name
	cust.customer_type = lead.get("custom_customer_type") or "Company"
	cust.lead_name = lead_name
	group = frappe.db.get_value("Customer Group", {"is_group": 0}, "name")
	if group:
		cust.customer_group = group
	territory = frappe.db.get_value("Territory", {"is_group": 0}, "name")
	if territory:
		cust.territory = territory
	if transaction_item_group:
		cust.custom_transaction_type = transaction_item_group

	# Task 1: carry VAT / Trade Licence from the (submitted) Finance Dossier.
	_apply_finance_dossier_details(cust, opportunity)

	cust.insert(ignore_permissions=True)
	return cust.name


def _apply_finance_dossier_details(cust, opportunity):
	"""Copy VAT Number -> custom_vat_certificate and Trade Licence Number ->
	custom_trade_license from the Opportunity's Finance Dossier onto the Customer."""
	if not opportunity:
		return
	fd = frappe.get_all(
		"Finance Dossier",
		filters={"finance_dossier_from": "Opportunity", "id": opportunity, "docstatus": 1},
		fields=["vat_number", "trade_licence_number"],
		order_by="modified desc",
		limit=1,
	)
	if not fd:
		return
	if fd[0].vat_number:
		cust.custom_vat_certificate = fd[0].vat_number
	if fd[0].trade_licence_number:
		cust.custom_trade_license = fd[0].trade_licence_number
