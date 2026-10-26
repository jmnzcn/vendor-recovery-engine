"""Schema contracts for every input table, after parsing.

Column names follow the Amazon SP-API vendor models (flattened): purchaseOrderNumber,
itemSequenceNumber, amazonProductIdentifier, netCost, ... Invoice `id`/`date` are
exposed as invoiceNumber/invoiceDate. Remittances and deductions are not part of the
SP-API; they follow the shape of Vendor Central payment and deduction reports.

Rows that break a contract are not dropped silently: ingestion moves them to a
quarantine table with the failed check, file and row number.
"""
from __future__ import annotations

import pandera.pandas as pa

from vre.policy import DEDUCTION_CHARGEBACK, DEDUCTION_PRICE, DEDUCTION_REVERSAL, DEDUCTION_SHORTAGE

CURRENCIES = ["USD", "EUR", "GBP", "CAD", "MXN", "JPY"]

# Column kinds drive parsing: ids/text stay strings, money becomes integer cents
STRING, MONEY, DATE, INT, FLOAT = "string", "money", "date", "int", "float"

COLUMNS: dict[str, dict[str, str]] = {
    "purchase_orders": {
        "purchaseOrderNumber": STRING, "purchaseOrderDate": DATE, "shipWindowEnd": DATE,
        "itemSequenceNumber": INT, "amazonProductIdentifier": STRING, "vendorProductIdentifier": STRING,
        "itemDescription": STRING, "orderedQuantity": INT, "netCost": MONEY, "currencyCode": STRING,
    },
    "shipments": {
        "shipmentIdentifier": STRING, "purchaseOrderNumber": STRING, "shippedDate": DATE,
        "shipmentConfirmationDate": DATE, "billOfLadingNumber": STRING, "cartonCount": INT,
    },
    "proof_of_delivery": {
        "billOfLadingNumber": STRING, "carrier": STRING, "deliveredDate": DATE,
        "cartonsDelivered": INT, "podDocumentId": STRING,
    },
    "invoices": {
        "invoiceNumber": STRING, "invoiceDate": DATE, "invoiceType": STRING, "purchaseOrderNumber": STRING,
        "itemSequenceNumber": INT, "amazonProductIdentifier": STRING, "invoicedQuantity": INT,
        "netCost": MONEY, "currencyCode": STRING,
    },
    "remittances": {
        "paymentNumber": STRING, "paymentDate": DATE, "invoiceNumber": STRING, "invoiceDate": DATE,
        "invoiceAmount": MONEY, "invoiceCurrency": STRING, "amountPaid": MONEY, "paymentCurrency": STRING,
        "exchangeRate": FLOAT,
    },
    "deductions": {
        "deductionNumber": STRING, "deductionDate": DATE, "deductionType": STRING, "reasonCode": STRING,
        "referenceNumber": STRING, "amazonProductIdentifier": STRING, "quantity": INT, "amount": MONEY,
        "currencyCode": STRING,
    },
}

# Header spellings seen in real exports, mapped to the canonical name
ALIASES = {
    "po": "purchaseOrderNumber", "po#": "purchaseOrderNumber", "ponumber": "purchaseOrderNumber",
    "invoice#": "invoiceNumber", "invoiceno": "invoiceNumber", "invoiceid": "invoiceNumber",
    "asin": "amazonProductIdentifier", "sku": "vendorProductIdentifier", "bol": "billOfLadingNumber",
    "asndate": "shipmentConfirmationDate", "deductionid": "deductionNumber",
}

# Natural keys: duplicated export rows on these keys are the same record exported twice
NATURAL_KEYS = {
    "purchase_orders": ["purchaseOrderNumber", "itemSequenceNumber"],
    "shipments": ["shipmentIdentifier"],
    "proof_of_delivery": ["billOfLadingNumber"],
    "invoices": ["invoiceNumber", "itemSequenceNumber"],
    "remittances": ["paymentNumber", "invoiceNumber", "amountPaid"],
    "deductions": ["deductionNumber"],
}

_req = {"nullable": False}
_nonneg = pa.Check.ge(0)

SCHEMAS = {
    "purchase_orders": pa.DataFrameSchema({
        "purchaseOrderNumber": pa.Column(str, pa.Check.str_length(min_value=1), **_req),
        "purchaseOrderDate": pa.Column("datetime64[ns]", **_req),
        "shipWindowEnd": pa.Column("datetime64[ns]", **_req),
        "itemSequenceNumber": pa.Column("Int64", pa.Check.ge(1), **_req),
        "amazonProductIdentifier": pa.Column(str, pa.Check.str_matches(r"^B0[A-Z0-9]{8}$"), **_req),
        "orderedQuantity": pa.Column("Int64", pa.Check.gt(0), **_req),
        "netCost": pa.Column("Int64", _nonneg, **_req),
        "currencyCode": pa.Column(str, pa.Check.isin(CURRENCIES), **_req),
    }, strict=False),
    "shipments": pa.DataFrameSchema({
        "shipmentIdentifier": pa.Column(str, pa.Check.str_length(min_value=1), **_req),
        "purchaseOrderNumber": pa.Column(str, pa.Check.str_length(min_value=1), **_req),
        "shippedDate": pa.Column("datetime64[ns]", **_req),
        "shipmentConfirmationDate": pa.Column("datetime64[ns]", **_req),
        "billOfLadingNumber": pa.Column(str, **_req),
        "cartonCount": pa.Column("Int64", pa.Check.gt(0), **_req),
    }, strict=False),
    "proof_of_delivery": pa.DataFrameSchema({
        "billOfLadingNumber": pa.Column(str, pa.Check.str_length(min_value=1), **_req),
        "deliveredDate": pa.Column("datetime64[ns]", **_req),
        "cartonsDelivered": pa.Column("Int64", _nonneg, **_req),
    }, strict=False),
    "invoices": pa.DataFrameSchema({
        "invoiceNumber": pa.Column(str, pa.Check.str_length(min_value=1), **_req),
        "invoiceDate": pa.Column("datetime64[ns]", **_req),
        "purchaseOrderNumber": pa.Column(str, pa.Check.str_length(min_value=1), **_req),
        "amazonProductIdentifier": pa.Column(str, **_req),
        "invoicedQuantity": pa.Column("Int64", _nonneg, **_req),
        "netCost": pa.Column("Int64", _nonneg, **_req),
        "currencyCode": pa.Column(str, pa.Check.isin(CURRENCIES), **_req),
    }, strict=False),
    "remittances": pa.DataFrameSchema({
        "paymentNumber": pa.Column(str, pa.Check.str_length(min_value=1), **_req),
        "paymentDate": pa.Column("datetime64[ns]", **_req),
        "invoiceNumber": pa.Column(str, pa.Check.str_length(min_value=1), **_req),
        "invoiceAmount": pa.Column("Int64", **_req),
        "invoiceCurrency": pa.Column(str, pa.Check.isin(CURRENCIES), **_req),
        "amountPaid": pa.Column("Int64", **_req),
        "exchangeRate": pa.Column(float, pa.Check.gt(0), **_req),
    }, strict=False),
    "deductions": pa.DataFrameSchema({
        "deductionNumber": pa.Column(str, pa.Check.str_length(min_value=3), **_req),
        "deductionDate": pa.Column("datetime64[ns]", **_req),
        "deductionType": pa.Column(str, pa.Check.isin(
            [DEDUCTION_SHORTAGE, DEDUCTION_PRICE, DEDUCTION_CHARGEBACK, DEDUCTION_REVERSAL]), **_req),
        "referenceNumber": pa.Column(str, pa.Check.str_length(min_value=1), **_req),
        "amount": pa.Column("Int64", pa.Check.gt(0), **_req),
        "currencyCode": pa.Column(str, pa.Check.isin(CURRENCIES), **_req),
    }, strict=False),
}
