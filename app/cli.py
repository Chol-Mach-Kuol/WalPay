"""Operational CLI. Cron examples (see README):
  */1  * * * *  flask --app app sms-worker
  0    2 * * *  flask --app app expire-vouchers
  0    9 * * 1  flask --app app export-payouts > payouts.csv
  (after paying) flask --app app confirm-payouts <batch_id> <reference>
"""
import click
from flask import Blueprint

from .extensions import db
from .services.jobs import confirm_payout_batch, create_payout_batch, expire_due_vouchers
from .services.sms import process_outbox
from .services.reports import sample_callbacks as _sample_callbacks

bp = Blueprint("ops", __name__, cli_group=None)


@bp.cli.command("init-db")
def init_db():
    """Create all tables (dev convenience; use migrations in prod)."""
    db.create_all()
    click.echo("Tables created.")


@bp.cli.command("sms-worker")
def sms_worker():
    """Process the SMS outbox once (run every minute via cron)."""
    stats = process_outbox()
    click.echo(f"sms-worker: {stats}")


@bp.cli.command("expire-vouchers")
def expire_vouchers():
    """Expire due vouchers and queue refund notifications."""
    count = expire_due_vouchers()
    click.echo(f"expired {count} voucher(s)")


@bp.cli.command("export-payouts")
def export_payouts():
    """Create a payout batch and print the CSV for manual payment."""
    batch, csv_text = create_payout_batch()
    if batch is None:
        click.echo("Nothing to pay out.")
        return
    click.echo(csv_text, nl=False)


@bp.cli.command("confirm-payouts")
@click.argument("batch_id", type=int)
@click.argument("reference")
def confirm_payouts(batch_id: int, reference: str):
    """Confirm a paid batch with the payment reference (e.g. m-Gurush txn id)."""
    paid = confirm_payout_batch(batch_id, reference)
    click.echo(f"batch {batch_id}: {paid} voucher(s) marked paid_out")


@bp.cli.command("create-admin")
@click.argument("email")
@click.password_option()
def create_admin(email: str, password: str):
    """Create an admin user and print the TOTP provisioning URI once."""
    from .models import AdminUser
    from .web.admin_auth import generate_totp_secret, provisioning_uri
    from .web.security import hash_password

    secret = generate_totp_secret()
    db.session.add(AdminUser(email=email.lower(), password_hash=hash_password(password),
                             totp_secret=secret))
    db.session.commit()
    click.echo("Scan this in your authenticator app (shown ONCE):")
    click.echo(provisioning_uri(secret, email))


@bp.cli.command("sample-callbacks")
def sample_callbacks_cmd():
    """Queue a random sample of redemptions for anti-fraud patient callbacks."""
    click.echo(f"queued {_sample_callbacks()} callback(s)")
