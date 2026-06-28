from sqlalchemy import Column, String, Boolean, DateTime, ForeignKey, JSON, Integer
from sqlalchemy.orm import relationship, DeclarativeBase
from datetime import datetime
import uuid


class Base(DeclarativeBase):
    pass


def new_id() -> str:
    return str(uuid.uuid4())


class Customer(Base):
    __tablename__ = "customers"

    id = Column(String, primary_key=True, default=new_id)
    name = Column(String(255), nullable=False)

    # Resend domain info — populated after calling /domains
    domain_name = Column(String(255), nullable=True)
    resend_domain_id = Column(String(255), nullable=True)
    domain_verified = Column(Boolean, default=False)

    created_at = Column(DateTime, default=datetime.utcnow)

    contacts = relationship("Contact", back_populates="customer", cascade="all, delete")
    segments = relationship("Segment", back_populates="customer", cascade="all, delete")
    campaigns = relationship("Campaign", back_populates="customer", cascade="all, delete")

    def __repr__(self):
        return f"<Customer id={self.id} name={self.name} domain={self.domain_name}>"


class Contact(Base):
    __tablename__ = "contacts"

    id = Column(String, primary_key=True, default=new_id)
    customer_id = Column(String, ForeignKey("customers.id"), nullable=False)

    email = Column(String(255), nullable=False)
    first_name = Column(String(100), nullable=True)
    last_name = Column(String(100), nullable=True)
    tags = Column(JSON, default=list)        # e.g. ["newsletter", "premium"]
    meta = Column(JSON, default=dict)        # any extra fields you need
    is_subscribed = Column(Boolean, default=True)

    created_at = Column(DateTime, default=datetime.utcnow)

    customer = relationship("Customer", back_populates="contacts")

    def __repr__(self):
        return f"<Contact email={self.email} customer={self.customer_id}>"


class Segment(Base):
    __tablename__ = "segments"

    id = Column(String, primary_key=True, default=new_id)
    customer_id = Column(String, ForeignKey("customers.id"), nullable=False)
    name = Column(String(255), nullable=False)

    created_at = Column(DateTime, default=datetime.utcnow)

    customer = relationship("Customer", back_populates="segments")
    # Many-to-many with Contact via contact_segments
    contacts = relationship(
        "Contact",
        secondary="contact_segments",
        backref="segments"
    )

    def __repr__(self):
        return f"<Segment id={self.id} name={self.name}>"


class ContactSegment(Base):
    __tablename__ = "contact_segments"

    contact_id = Column(String, ForeignKey("contacts.id", ondelete="CASCADE"), primary_key=True)
    segment_id = Column(String, ForeignKey("segments.id", ondelete="CASCADE"), primary_key=True)
    added_at = Column(DateTime, default=datetime.utcnow)


class Campaign(Base):
    """
    Records every bulk email send. Created automatically when you call
    POST /customers/{id}/send. Cannot be edited — it's a historical log.
    Can be deleted from our DB (does not affect already-sent emails).
    """
    __tablename__ = "campaigns"

    id = Column(String, primary_key=True, default=new_id)
    customer_id = Column(String, ForeignKey("customers.id", ondelete="CASCADE"), nullable=False)

    subject = Column(String(500), nullable=False)
    sent_to_count = Column(Integer, default=0)      # number of recipients
    from_address = Column(String(255))               # e.g. hello@acme.com
    targeting = Column(JSON, default=dict)           # e.g. {"tag": "premium"} or {"segment_id": "..."}
    status = Column(String(50), default="sending")   # sending | sent | partial | failed

    sent_at = Column(DateTime, default=datetime.utcnow)

    customer = relationship("Customer", back_populates="campaigns")
    recipients = relationship("CampaignRecipient", back_populates="campaign", cascade="all, delete")

    def __repr__(self):
        return f"<Campaign id={self.id} subject={self.subject[:30]} sent_to={self.sent_to_count}>"


class CampaignRecipient(Base):
    """One row per contact per campaign. Populated when the batch call returns email IDs,
    then updated by the webhook handler as delivery events arrive."""
    __tablename__ = "campaign_recipients"

    id = Column(String, primary_key=True, default=new_id)
    campaign_id = Column(String, ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False)
    contact_id = Column(String, ForeignKey("contacts.id", ondelete="SET NULL"), nullable=True)
    resend_email_id = Column(String(255), nullable=True, index=True)
    status = Column(String(50), default="queued")  # queued | sent | delivered | bounced | complained
    updated_at = Column(DateTime, default=datetime.utcnow)

    campaign = relationship("Campaign", back_populates="recipients")
    contact = relationship("Contact")
