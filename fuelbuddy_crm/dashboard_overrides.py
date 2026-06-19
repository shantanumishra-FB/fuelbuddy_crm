# Copyright (c) 2026, Fuelbuddy and contributors
# For license information, please see license.txt

from frappe import _


def opportunity_dashboard(data):
	"""Augment the Opportunity form's Connections tab with the FuelBuddy documents.

	Both target doctypes link back to the Opportunity via a Dynamic Link:
	  - Finance Dossier        -> finance_dossier_from == "Opportunity", id == <opp name>
	  - Business Documentation -> reference_doctype == "Opportunity", reference_name == <opp name>

	Registered via the `override_doctype_dashboards` hook; `data` is the base
	dashboard dict produced by erpnext's opportunity_dashboard.get_data().
	"""
	data = data or {}
	data.setdefault("transactions", [])
	data.setdefault("non_standard_fieldnames", {})

	# the dynamic-link field on each target that references the Opportunity name
	data["non_standard_fieldnames"]["Finance Dossier"] = "id"
	data["non_standard_fieldnames"]["Business Documentation"] = "reference_name"

	data["transactions"].append(
		{"label": _("FuelBuddy"), "items": ["Finance Dossier", "Business Documentation"]}
	)
	return data
