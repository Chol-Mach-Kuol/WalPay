import os

import pytest

os.environ["PSP_WEBHOOK_SECRET"] = "test-webhook-secret"
# Set BEFORE create_app so no code path can ever touch a file-backed dev DB.
os.environ["DATABASE_URL"] = "sqlite:///:memory:"

from app import create_app
from app.extensions import db as _db
from app.models import Provider, Sender, ServiceBundle


@pytest.fixture()
def app():
    app = create_app()
    app.config.update(
        SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
        TESTING=True,
        VOUCHER_CODE_PEPPER="test-pepper",
    )
    with app.app_context():
        _db.create_all()
        yield app
        _db.session.remove()
        _db.drop_all()


@pytest.fixture()
def db(app):
    return _db


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def seed(db):
    """A verified provider with one bundle, and one sender."""
    sender = Sender(email="chol@example.com", full_name="Test Sender", country_code="RW")
    provider = Provider(name="Juba Care Clinic", city="Juba", phone="+211900000001", is_verified=True)
    db.session.add_all([sender, provider])
    db.session.flush()
    bundle = ServiceBundle(provider_id=provider.id, title="Malaria test + treatment", price_cents=1500)
    db.session.add(bundle)
    db.session.commit()
    return {"sender": sender, "provider": provider, "bundle": bundle}
