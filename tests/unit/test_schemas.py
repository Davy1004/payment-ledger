import uuid

import pytest
from pydantic import ValidationError

from app.schemas.ledger import AccountCreate, MoneyRequest, TransferCreate
from app.services.ledger import fingerprint


def test_currency_is_uppercased_and_validated():
    assert AccountCreate(owner_name="A", currency="inr").currency == "INR"
    with pytest.raises(ValidationError):
        AccountCreate(owner_name="A", currency="12$")
    with pytest.raises(ValidationError):
        AccountCreate(owner_name="A", currency="RUPEE")


@pytest.mark.parametrize("amount", [0, -5, 10**12 + 1])
def test_amount_must_be_positive_and_bounded(amount):
    with pytest.raises(ValidationError):
        MoneyRequest(amount=amount)


def test_transfer_to_same_account_rejected():
    same = uuid.uuid4()
    with pytest.raises(ValidationError):
        TransferCreate(from_account_id=same, to_account_id=same, amount=10)


def test_fingerprint_is_stable_and_sensitive():
    a, b = uuid.uuid4(), uuid.uuid4()
    assert fingerprint("TRANSFER", a, b, 100, None) == fingerprint("TRANSFER", a, b, 100, None)
    assert fingerprint("TRANSFER", a, b, 100, None) != fingerprint("TRANSFER", a, b, 101, None)
